"""faster-whisper による文字起こし (GPU / CPU 切り替え対応)。"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# Whisper がよく出す無音時の幻聴テキスト
_HALLUCINATIONS = {
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございました。",
    "ありがとうございました。",
    "おやすみなさい。",
    "チャンネル登録よろしくお願いします",
}


def _add_cuda_dll_dirs() -> None:
    """pip で入れた nvidia-cublas / nvidia-cudnn の DLL を Windows で見つけられるようにする。"""
    if sys.platform != "win32":
        return
    for site in map(Path, sys.path):
        nvidia = site / "nvidia"
        if not nvidia.is_dir():
            continue
        for bin_dir in nvidia.glob("*/bin"):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


def resolve_device(device: str) -> str:
    import ctranslate2

    if device == "auto":
        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    if device == "cuda" and ctranslate2.get_cuda_device_count() == 0:
        raise RuntimeError("GPU (CUDA) が見つかりません。--device cpu を指定するか、CUDA環境を確認してください。")
    return device


class Transcriber:
    def __init__(self, model: str, device: str, compute_type: str, language: str, beam_size: int):
        self.model_name = model
        self.language = language
        self.beam_size = beam_size
        self.hotwords: str | None = None
        self._lock = threading.Lock()
        self._model = None
        self.device = ""
        self.compute_type = ""
        self.load(device, compute_type)

    def load(self, device: str, compute_type: str = "default") -> None:
        """モデルを (再) ロードする。実行中の文字起こしが終わるまで待ってから切り替える。"""
        _add_cuda_dll_dirs()
        from faster_whisper import WhisperModel

        resolved = resolve_device(device)
        if compute_type == "default":
            compute_type = "float16" if resolved == "cuda" else "int8"

        # faster-whisper の既定は 4 スレッド。small を CPU で動かすと 1 回 2.6 秒ほどかかり実時間に追いつかないので増やす
        # (16 スレッドの環境で 8 にすると 1 回 1.8 秒ほど。それ以上はほぼ変わらない)
        cpu_threads = min(8, os.cpu_count() or 4) if resolved == "cpu" else 0
        log.info(
            "Whisperモデル読み込み中: model=%s device=%s compute_type=%s cpu_threads=%s",
            self.model_name, resolved, compute_type, cpu_threads or "-",
        )
        model = WhisperModel(self.model_name, device=resolved, compute_type=compute_type, cpu_threads=cpu_threads)
        # 初回推論は遅いのでウォームアップしておく
        list(model.transcribe(np.zeros(16000, dtype=np.float32), language=self.language, beam_size=1)[0])

        with self._lock:
            self._model = model
            self.device = resolved
            self.compute_type = compute_type
        log.info("Whisperモデル読み込み完了 (%s)", resolved)

    def transcribe(self, audio: np.ndarray) -> str:
        """16kHz mono float32 の音声を文字起こしする。"""
        with self._lock:
            segments, _ = self._model.transcribe(
                audio,
                language=self.language,
                beam_size=self.beam_size,
                condition_on_previous_text=False,
                without_timestamps=True,
                vad_filter=False,  # 区切りは受信側で済ませているので不要 (遅延削減)
                no_speech_threshold=0.6,
                hotwords=self.hotwords,
            )
            texts = [s.text for s in segments if s.no_speech_prob < 0.8]
        text = "".join(texts).strip()
        return "" if text in _HALLUCINATIONS else text
