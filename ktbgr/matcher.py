"""キーワードの曖昧一致検出。

Whisper の出力は同じ発話でも「漢字/ひらがな/カタカナ」表記や細かい音の聞き間違いで揺れるため、
以下の3つの表記に正規化した上で、それぞれ類似度 (編集距離ベース) を計算し最大値をスコアとする。

- surface : NFKC正規化 + 小文字化 + カタカナ→ひらがな + 記号除去
- reading : 漢字を読み (ひらがな) に変換したもの
- romaji  : ヘボン式ローマ字 (音の近さを拾う)
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pykakasi
from rapidfuzz.distance import Levenshtein

_kakasi = pykakasi.kakasi()
_STRIP_RE = re.compile(r"[^0-9a-zぁ-ゖー一-龯々〆ヵヶ]")
_ROMAJI_STRIP_RE = re.compile(r"[^0-9a-z]")

# これより短い表記は曖昧一致すると誤爆しやすいので完全一致 (部分文字列) のみ許可する
_MIN_FUZZY_LEN = {"surface": 3, "reading": 3, "romaji": 5}


def _kata_to_hira(text: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


@dataclass(frozen=True)
class Forms:
    surface: str
    reading: str
    romaji: str

    def items(self):
        return (("surface", self.surface), ("reading", self.reading), ("romaji", self.romaji))


def to_forms(text: str) -> Forms:
    base = _kata_to_hira(unicodedata.normalize("NFKC", text).lower())
    surface = _STRIP_RE.sub("", base)
    converted = _kakasi.convert(base)
    reading = _STRIP_RE.sub("", "".join(part["hira"] for part in converted))
    romaji = _ROMAJI_STRIP_RE.sub("", "".join(part["hepburn"] for part in converted).lower())
    return Forms(surface, reading, romaji)


def window_similarity(needle: str, haystack: str, min_fuzzy_len: int) -> float:
    """haystack 内で needle に最も近い部分文字列との類似度 (0-100) を返す。"""
    if not needle or not haystack:
        return 0.0
    if needle in haystack:
        return 100.0
    if len(needle) < min_fuzzy_len:
        return 0.0

    n = len(needle)
    best = 0.0
    # 1文字の挿入/脱落も拾えるよう、窓幅を n-1 〜 n+1 で走査する
    for width in (n - 1, n, n + 1):
        if width <= 0 or width > len(haystack):
            continue
        for start in range(len(haystack) - width + 1):
            score = Levenshtein.normalized_similarity(needle, haystack[start : start + width]) * 100
            if score > best:
                best = score
                if best == 100.0:
                    return best
    if len(haystack) < n - 1:
        best = max(best, Levenshtein.normalized_similarity(needle, haystack) * 100)
    return best


@dataclass
class Keyword:
    name: str
    aliases: list[str] = field(default_factory=list)
    threshold: float | None = None
    reply: str | None = None
    sound: str | None = None
    forms: list[Forms] = field(default_factory=list, repr=False)

    def __post_init__(self):
        self.forms = [to_forms(v) for v in [self.name, *self.aliases]]


@dataclass
class Match:
    keyword: Keyword
    score: float
    variant: str  # どの表記で一致したか


class KeywordMatcher:
    def __init__(self, keywords: list[Keyword], default_threshold: float):
        self.keywords = keywords
        self.default_threshold = default_threshold

    @classmethod
    def from_file(cls, path: Path, default_threshold: float) -> "KeywordMatcher":
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data["keywords"] if isinstance(data, dict) else data
        keywords = [
            Keyword(e) if isinstance(e, str) else Keyword(
                name=e["name"],
                aliases=e.get("aliases", []),
                threshold=e.get("threshold"),
                reply=e.get("reply"),
                sound=e.get("sound"),
            )
            for e in entries
        ]
        return cls(keywords, default_threshold)

    def find(self, text: str) -> list[Match]:
        text_forms = to_forms(text)
        matches: list[Match] = []
        for keyword in self.keywords:
            threshold = keyword.threshold if keyword.threshold is not None else self.default_threshold
            best: Match | None = None
            for kw_forms in keyword.forms:
                for (variant, needle), (_, haystack) in zip(kw_forms.items(), text_forms.items()):
                    score = window_similarity(needle, haystack, _MIN_FUZZY_LEN[variant])
                    if best is None or score > best.score:
                        best = Match(keyword, score, variant)
            if best is not None and best.score >= threshold:
                matches.append(best)
        return matches
