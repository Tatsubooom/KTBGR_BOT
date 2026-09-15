"""環境変数 / コマンドライン引数から設定を読み込む。"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEVICES = ("auto", "cuda", "cpu")


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    token: str
    guild_id: int | None

    # 文字起こし
    device: str
    model: str
    compute_type: str
    language: str
    beam_size: int

    # 音声区切り (リアルタイム性に関わるパラメータ)
    silence_ms: int
    partial_interval_ms: int
    max_segment_sec: float
    min_speech_ms: int
    energy_threshold: float

    # キーワード検出
    keywords_files: list[Path]
    match_threshold: float
    cooldown_sec: float
    combo_window_sec: float  # VC でこの秒数以内に続けて検出したらコンボ継続 (0で無効)

    # テキストチャット
    text_chat: bool
    text_channel_ids: set[int]  # 空なら全チャンネル

    # 出力
    post_transcripts: bool
    response_channel_id: int | None


def load_settings(argv: list[str] | None = None) -> Settings:
    load_dotenv()

    parser = argparse.ArgumentParser(description="VC文字起こし & キーワード反応BOT")
    parser.add_argument(
        "--device",
        choices=DEVICES,
        default=os.getenv("WHISPER_DEVICE", "auto"),
        help="文字起こしに使うデバイス (auto: GPUがあればGPU、なければCPU)",
    )
    parser.add_argument("--model", default=os.getenv("WHISPER_MODEL", "small"))
    parser.add_argument("--compute-type", default=os.getenv("WHISPER_COMPUTE_TYPE", "default"))
    parser.add_argument(
        "--keywords",
        default=os.getenv("KEYWORDS_FILE", "keywords.json,data/inmu_goroku.json,data/hikamani_goroku.json"),
        help="キーワードファイル (カンマ区切りで複数指定可)",
    )
    args = parser.parse_args(argv)

    token = os.getenv("DISCORD_TOKEN", "")
    if not token:
        raise SystemExit("DISCORD_TOKEN が設定されていません (.env を確認してください)")

    guild_id = os.getenv("GUILD_ID")
    response_channel_id = os.getenv("RESPONSE_CHANNEL_ID")

    return Settings(
        token=token,
        guild_id=int(guild_id) if guild_id else None,
        device=args.device,
        model=args.model,
        compute_type=args.compute_type,
        language=os.getenv("WHISPER_LANGUAGE", "ja"),
        beam_size=_env_int("WHISPER_BEAM_SIZE", 1),
        silence_ms=_env_int("SILENCE_MS", 450),
        partial_interval_ms=_env_int("PARTIAL_INTERVAL_MS", 1000),
        max_segment_sec=_env_float("MAX_SEGMENT_SEC", 8.0),
        min_speech_ms=_env_int("MIN_SPEECH_MS", 250),
        energy_threshold=_env_float("ENERGY_THRESHOLD", 0.008),
        keywords_files=[Path(p.strip()) for p in args.keywords.split(",") if p.strip()],
        match_threshold=_env_float("MATCH_THRESHOLD", 70.0),
        cooldown_sec=_env_float("COOLDOWN_SEC", 5.0),
        combo_window_sec=_env_float("COMBO_WINDOW_SEC", 8.0),
        text_chat=_env_bool("TEXT_CHAT", True),
        text_channel_ids={int(c) for c in os.getenv("TEXT_CHANNEL_IDS", "").split(",") if c.strip()},
        post_transcripts=_env_bool("POST_TRANSCRIPTS", False),
        response_channel_id=int(response_channel_id) if response_channel_id else None,
    )
