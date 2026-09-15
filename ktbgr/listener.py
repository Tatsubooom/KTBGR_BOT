"""VC音声の受信 → 発話区切り → 文字起こし → キーワード検出 のパイプライン。

リアルタイム性のための工夫:
- 話者ごとにバッファし、短い無音 (SILENCE_MS) で即座に発話を確定させる
- 話している途中でも PARTIAL_INTERVAL_MS ごとに、直近 PARTIAL_WINDOW_SEC 秒だけを文字起こしし、キーワードを早期検出する
- 文字起こし待ちが詰まったら古い途中経過は捨て、常に最新の音声だけを処理する
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import numpy as np
from discord.ext import voice_recv

from .config import Settings
from .matcher import KeywordMatcher, Match, to_forms
from .transcriber import Transcriber

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME_MS = 20
PREROLL_FRAMES = 10  # 発話開始前の 200ms も含めて頭切れを防ぐ

# utterance_key: (ユーザーID, 発話ID)。同じ発話の途中経過と確定結果を結びつけるのに使う
UtteranceKey = tuple[int, int]
# (user, 発言, 一致, 発話キー, 照合に使った直前の発言)
DetectCallback = Callable[[object, str, list[Match], UtteranceKey, list[str]], Awaitable[None]]
TranscriptCallback = Callable[[object, str, UtteranceKey], Awaitable[None]]


def pcm_to_16k_mono(pcm: bytes) -> np.ndarray:
    """Discordの 48kHz / 16bit / stereo PCM を 16kHz mono float32 に変換する。"""
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    mono = samples.reshape(-1, 2).mean(axis=1)
    usable = len(mono) - len(mono) % 3
    return mono[:usable].reshape(-1, 3).mean(axis=1)  # 3サンプル平均で簡易ローパス + 間引き


@dataclass
class UserStream:
    user: object
    frames: list[np.ndarray] = field(default_factory=list)
    preroll: deque = field(default_factory=lambda: deque(maxlen=PREROLL_FRAMES))
    voiced_ms: int = 0
    speaking: bool = False
    last_voice: float = 0.0
    last_partial: float = 0.0
    utterance_id: int = 0

    def audio(self) -> np.ndarray:
        return np.concatenate(self.frames)

    def duration_sec(self) -> float:
        return len(self.frames) * FRAME_MS / 1000

    def reset(self) -> None:
        self.frames = []
        self.preroll.clear()
        self.voiced_ms = 0
        self.speaking = False
        self.utterance_id += 1


@dataclass
class Job:
    user: object
    utterance_id: int
    audio: np.ndarray
    final: bool


class SpeechSink(voice_recv.AudioSink):
    def __init__(
        self,
        settings: Settings,
        transcriber: Transcriber,
        get_matcher: Callable[[], KeywordMatcher],
        loop: asyncio.AbstractEventLoop,
        on_detect: DetectCallback,
        on_transcript: TranscriptCallback | None = None,
    ):
        super().__init__()
        self.settings = settings
        self.transcriber = transcriber
        self.get_matcher = get_matcher
        self.loop = loop
        self.on_detect = on_detect
        self.on_transcript = on_transcript

        self._streams: dict[int, UserStream] = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._finals: deque[Job] = deque()
        self._partials: dict[int, Job] = {}  # ユーザーごとに最新の途中経過だけ保持
        self._triggered: dict[tuple[int, int], set[str]] = {}
        self._last_fired: dict[tuple[int, str], float] = {}
        self._recent_finals: dict[int, deque[tuple[float, str, UtteranceKey]]] = {}  # ユーザーごとの直前の確定発言
        self._skip = object()  # _next_job が「処理不要のジョブだった」ことを示す印
        self._busy = False  # 文字起こし中か
        self._job_sec = 1.0  # 1回の文字起こしにかかる時間 (移動平均)
        self._running = True

        self._monitor = threading.Thread(target=self._monitor_loop, name="stt-monitor", daemon=True)
        self._worker = threading.Thread(target=self._worker_loop, name="stt-worker", daemon=True)
        self._monitor.start()
        self._worker.start()

    # ---- voice_recv.AudioSink ----
    def wants_opus(self) -> bool:
        return False

    def write(self, user, data: voice_recv.VoiceData):
        if user is None or getattr(user, "bot", False) or not data.pcm:
            return
        frame = pcm_to_16k_mono(data.pcm)
        voiced = float(np.sqrt(np.mean(frame**2))) >= self.settings.energy_threshold
        now = time.monotonic()

        with self._lock:
            stream = self._streams.get(user.id)
            if stream is None:
                stream = self._streams[user.id] = UserStream(user)
            stream.user = user

            if voiced:
                if not stream.speaking:
                    stream.speaking = True
                    stream.frames.extend(stream.preroll)
                    stream.preroll.clear()
                    stream.last_partial = now
                stream.voiced_ms += FRAME_MS
                stream.last_voice = now

            if stream.speaking:
                stream.frames.append(frame)
            else:
                stream.preroll.append(frame)

    def cleanup(self):
        with self._cond:
            self._running = False
            self._cond.notify_all()

    # ---- 発話区切り ----
    def _monitor_loop(self) -> None:
        s = self.settings
        while self._running:
            time.sleep(0.05)
            now = time.monotonic()
            with self._cond:
                for uid, stream in self._streams.items():
                    if not stream.speaking:
                        continue
                    enough = stream.voiced_ms >= s.min_speech_ms

                    if now - stream.last_voice >= s.silence_ms / 1000:
                        # 無音が続いた (またはパケットが途絶えた) → 発話確定
                        if enough:
                            self._finals.append(Job(stream.user, stream.utterance_id, stream.audio(), True))
                            self._partials.pop(uid, None)
                        stream.reset()
                        self._cond.notify()
                    elif stream.duration_sec() >= s.max_segment_sec:
                        # 長すぎる発話は区切って確定させる (同一発話扱いで二重反応を防ぐ)
                        self._finals.append(Job(stream.user, stream.utterance_id, stream.audio(), True))
                        self._partials.pop(uid, None)
                        stream.frames = stream.frames[-(1000 // FRAME_MS):]  # 1秒分を重ねて単語の分断を防ぐ
                        stream.last_partial = now
                        self._cond.notify()
                    elif (
                        s.partial_interval_ms > 0
                        and enough
                        # 途中経過は、前回から (間隔か、1回の文字起こし時間の1.5倍の長い方) 経ってから。
                        # 文字起こしが追いつかない環境で途中経過を詰め込むと、確定の文字起こしが待たされて遅れる
                        and now - stream.last_partial >= max(s.partial_interval_ms / 1000, self._job_sec * 1.5)
                        and not self._busy
                        and not self._finals
                    ):
                        # 途中経過は直近 PARTIAL_WINDOW_SEC 秒だけを文字起こしする。
                        # 発話全体を毎回やり直すと、話が長くなるほど処理が重くなり遅延が溜まる
                        audio = stream.audio()[-int(s.partial_window_sec * SAMPLE_RATE):]
                        self._partials[uid] = Job(stream.user, stream.utterance_id, audio, False)
                        stream.last_partial = now
                        self._cond.notify()

    # ---- 文字起こし ----
    def _next_job(self) -> Job | None:
        with self._cond:
            while self._running and not self._finals and not self._partials:
                self._cond.wait()
            if not self._running:
                return None
            if self._finals:
                self._busy = True
                return self._finals.popleft()
            uid, job = self._partials.popitem()
            stream = self._streams.get(uid)
            if stream is None or stream.utterance_id != job.utterance_id:
                # 待っている間に発話が確定済み → 確定の文字起こしで検知するので途中経過は捨てる
                return self._skip
            self._busy = True
            return job

    def _worker_loop(self) -> None:
        while (job := self._next_job()) is not None:
            if job is self._skip:
                continue
            try:
                self._process(job)
            except Exception:
                log.exception("文字起こし処理でエラーが発生しました")
            finally:
                with self._lock:
                    self._busy = False

    def _process(self, job: Job) -> None:
        started = time.monotonic()
        text = self.transcriber.transcribe(job.audio)
        elapsed = time.monotonic() - started
        self._job_sec = self._job_sec * 0.7 + elapsed * 0.3
        log.debug(
            "[%s] %s (%.1fs音声 / 処理%.2fs) %s",
            "確定" if job.final else "途中", job.user, len(job.audio) / SAMPLE_RATE, elapsed, text,
        )
        if not text:
            self._finish(job)
            return

        key = (job.user.id, job.utterance_id)
        if job.final:
            log.info("%s: %s", job.user, text)
            if self.on_transcript:
                asyncio.run_coroutine_threadsafe(self.on_transcript(job.user, text, key), self.loop)

        # 直前 CONTEXT_SEC 秒以内に確定した同じ人の発言とつなげて照合する。
        # 区切りを細かくすると「ガチャン！」「ゴン！」のように1つの語録が複数の発話に分かれるため
        now = time.monotonic()
        with self._lock:
            recent = self._recent_finals.setdefault(job.user.id, deque())
            while recent and now - recent[0][0] > self.settings.context_sec:
                recent.popleft()
            context = [t for _, t, k in recent if k != key]

        # 照合は数十ms かかるので、音声受信 (write) を止めないようロックの外で行う
        combined = " ".join([*context, text])
        found = self.get_matcher().find(combined)
        if context:
            # 前の発言だけで完結する一致は、そのとき反応済みなので除く
            found = [m for m in found if m.end > self._context_boundaries(context, combined, m.variant)[-1]]
        if job.final:
            with self._lock:
                recent.append((now, text, key))

        groups: dict[tuple[str, ...], list[Match]] = {}
        with self._lock:
            fired = self._triggered.setdefault(key, set())
            for match in found:
                name = match.keyword.name
                last = self._last_fired.get((job.user.id, name), 0.0)
                # 同じ語録が続けて一致している間はクールダウンを延長する。
                # 1つの語録が複数の発話に分かれ、前半と後半がそれぞれ一致して二重に数えるのを防ぐ
                self._last_fired[(job.user.id, name)] = now
                if name in fired or now - last < self.settings.cooldown_sec:
                    continue
                fired.add(name)
                # 付記するのは、一致した部分がまたがっている直前の発言だけ
                boundaries = self._context_boundaries(context, combined, match.variant) if context else []
                used = tuple(c for c, end in zip(context, boundaries) if end > match.start)
                groups.setdefault(used, []).append(match)

        for used, matches in groups.items():
            log.info(
                "検出: %s <- %s「%s」%s",
                [m.keyword.name for m in matches], job.user, " ".join((*used, text)),
                [(round(m.score), m.variant) for m in matches],
            )
            asyncio.run_coroutine_threadsafe(self.on_detect(job.user, text, matches, key, list(used)), self.loop)

        self._finish(job)

    @staticmethod
    @functools.lru_cache(maxsize=256)
    def _form_length(text: str, variant: str) -> int:
        return len(getattr(to_forms(text), variant))

    def _context_boundaries(self, context: list[str], combined: str, variant: str) -> list[float]:
        """直前の各発言が、つなげた文のどこまでを占めるか (割合)。

        一致位置は表記 (surface / reading など) ごとの文字数に対する割合なので、同じ表記の文字数で計算する。
        """
        total = self._form_length(combined, variant) or 1
        return [self._form_length(" ".join(context[: i + 1]), variant) / total for i in range(len(context))]

    def _finish(self, job: Job) -> None:
        if job.final:
            with self._lock:
                # 確定済みの発話の検出履歴は不要 (長い発話の分割時は同じIDが続くので残す)
                current = self._streams.get(job.user.id)
                if current is None or current.utterance_id != job.utterance_id:
                    self._triggered.pop((job.user.id, job.utterance_id), None)
