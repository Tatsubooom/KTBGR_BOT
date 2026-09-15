"""キーワードの曖昧一致検出。

Whisper の出力は同じ発話でも「漢字/ひらがな/カタカナ」表記や細かい音の聞き間違いで揺れるため、
以下の3つの表記に正規化した上で、それぞれ類似度 (編集距離ベース) を計算し最大値をスコアとする。

- surface : NFKC正規化 + 小文字化 + カタカナ→ひらがな + 記号除去
- reading : 漢字を読み (ひらがな) に変換したもの
- romaji  : ヘボン式ローマ字 (音の近さを拾う)

いずれも長音 (ー/～)・小書き文字 (ぁ→あ)・3文字以上の同じ文字の連続 (ああああ→ああ) を揃えてから比較する。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pykakasi
from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

log = logging.getLogger(__name__)

_kakasi = pykakasi.kakasi()
_STRIP_RE = re.compile(r"[^0-9a-zぁ-ゖ一-龯々〆]")
_ROMAJI_STRIP_RE = re.compile(r"[^0-9a-z]")
_REPEAT_RE = re.compile(r"(.)\1{2,}")
_SMALL_KANA = str.maketrans("ぁぃぅぇぉゃゅょゎゕゖ", "あいうえおやゆよわかけ")

# これより短い表記は曖昧一致すると誤爆しやすいので完全一致 (部分文字列) のみ許可する
_MIN_FUZZY_LEN = {"surface": 3, "reading": 3, "romaji": 5}
# 短いローマ字は完全一致でも音の区切りをまたいで誤爆する (「おつ」otsu が「バイト疲れた」baiTOTSUkareta に一致など) ので比較しない
_MIN_EXACT_LEN = {"surface": 0, "reading": 0, "romaji": 5}

# 曖昧一致に必要な最低スコア。MATCH_THRESHOLD を下げても、これより低い一致は採用しない。
# ローマ字や短いキーワードは、長文 (歌詞の貼り付けなど) のどこかに似た並びが偶然見つかりやすいため。
_ROMAJI_MIN_SCORE = 85
_SHORT_NEEDLE_LEN = 7
_SHORT_NEEDLE_MIN_SCORE = 85


def _fuzzy_floor(variant: str, needle: str) -> float:
    if variant == "romaji":
        return _ROMAJI_MIN_SCORE
    if len(needle) < _SHORT_NEEDLE_LEN:
        return _SHORT_NEEDLE_MIN_SCORE
    return 0.0


def _kata_to_hira(text: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


def _clean(text: str, strip_re: re.Pattern) -> str:
    return _REPEAT_RE.sub(r"\1\1", strip_re.sub("", text.translate(_SMALL_KANA)))


@dataclass(frozen=True)
class Forms:
    surface: str
    reading: str
    romaji: str

    def items(self):
        return (("surface", self.surface), ("reading", self.reading), ("romaji", self.romaji))


def to_forms(text: str) -> Forms:
    base = _kata_to_hira(unicodedata.normalize("NFKC", text).lower())
    converted = _kakasi.convert(base)
    return Forms(
        surface=_clean(base, _STRIP_RE),
        reading=_clean("".join(part["hira"] for part in converted), _STRIP_RE),
        romaji=_clean("".join(part["hepburn"] for part in converted).lower(), _ROMAJI_STRIP_RE),
    )


def window_similarity(
    needle: str, haystack: str, min_fuzzy_len: int, cutoff: float = 0, min_exact_len: int = 0
) -> tuple[float, float, float]:
    """haystack 内で needle に最も近い部分文字列との類似度 (0-100) と、その位置を返す。

    位置は表記 (surface/reading/romaji) ごとに文字数が違うので、haystack 全体に対する割合 (0.0-1.0) で表す。
    """
    if not needle or not haystack:
        return 0.0, 0.0, 0.0
    if len(needle) < min_exact_len:
        return 0.0, 0.0, 0.0
    n = len(haystack)
    index = haystack.find(needle)
    if index >= 0:
        return 100.0, index / n, (index + len(needle)) / n
    if len(needle) < min_fuzzy_len or cutoff >= 100:
        return 0.0, 0.0, 0.0
    if n < len(needle):
        # 発話がキーワードより短い場合は全体同士で比較 (発話がキーワードの一部なだけで反応しないように)
        return Levenshtein.normalized_similarity(needle, haystack) * 100, 0.0, 1.0
    aligned = fuzz.partial_ratio_alignment(needle, haystack, score_cutoff=cutoff)
    if aligned is None:
        return 0.0, 0.0, 0.0
    return aligned.score, aligned.dest_start / n, aligned.dest_end / n


@dataclass
class Keyword:
    name: str
    aliases: list[str] = field(default_factory=list)
    threshold: float | None = None
    reply: str | None = None
    label: str | None = None  # コンボ表示での1行表記 (未指定なら name)
    sound: str | None = None
    enabled: bool = True
    hotword: bool = True  # Whisper に認識のヒントとして渡すか
    forms: list[Forms] = field(default_factory=list, repr=False)

    def __post_init__(self):
        self.forms = [to_forms(v) for v in [self.name, *self.aliases]]


@dataclass
class Match:
    keyword: Keyword
    score: float
    variant: str  # どの表記で一致したか
    start: float = 0.0  # 発言内の一致位置 (割合)
    end: float = 0.0

    def overlaps(self, other: "Match", ratio: float = 0.3) -> bool:
        # 位置は表記ごとの割合なので境界は多少ずれる。別々の語録が並んでいるだけならほぼ重ならないので低めにしている
        """一致箇所が短い方の長さの ratio 以上重なっているか。"""
        shorter = min(self.end - self.start, other.end - other.start)
        overlap = min(self.end, other.end) - max(self.start, other.start)
        return shorter > 0 and overlap >= shorter * ratio


def _load_keywords(path: Path) -> list[Keyword]:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data["keywords"] if isinstance(data, dict) else data
    return [
        Keyword(e) if isinstance(e, str) else Keyword(
            name=e["name"],
            aliases=e.get("aliases", []),
            threshold=e.get("threshold"),
            reply=e.get("reply"),
            label=e.get("label"),
            sound=e.get("sound"),
            enabled=e.get("enabled", True),
            hotword=e.get("hotword", True),
        )
        for e in entries
    ]


class KeywordMatcher:
    def __init__(self, keywords: list[Keyword], default_threshold: float):
        self.all_keywords = keywords
        self.keywords = [k for k in keywords if k.enabled]
        self.default_threshold = default_threshold

    @classmethod
    def from_file(cls, path: Path, default_threshold: float) -> "KeywordMatcher":
        return cls.from_files([path], default_threshold)

    @classmethod
    def from_files(cls, paths: list[Path], default_threshold: float) -> "KeywordMatcher":
        keywords: list[Keyword] = []
        for path in paths:
            loaded = _load_keywords(path)
            log.info("キーワード読み込み: %s (%d件)", path, len(loaded))
            keywords += loaded
        return cls(keywords, default_threshold)

    def find(self, text: str, allow_overlap: bool = False) -> list[Match]:
        """一致したキーワードをスコアの高い順に返す。

        allow_overlap=False の場合、発言内の同じ箇所に複数のキーワードが一致したら
        スコアの高いもの (同点なら長いもの) だけを残す。ゆるい一致で同じ箇所が何重にもカウントされるのを防ぐ。
        """
        text_forms = to_forms(text)
        matches: list[Match] = []
        for keyword in self.keywords:
            threshold = keyword.threshold if keyword.threshold is not None else self.default_threshold
            best: Match | None = None
            for kw_forms in keyword.forms:
                for (variant, needle), (_, haystack) in zip(kw_forms.items(), text_forms.items()):
                    cutoff = max(threshold, _fuzzy_floor(variant, needle))
                    score, start, end = window_similarity(
                        needle, haystack, _MIN_FUZZY_LEN[variant], cutoff, _MIN_EXACT_LEN[variant]
                    )
                    if score < cutoff:
                        continue
                    if best is None or score > best.score:
                        best = Match(keyword, score, variant, start, end)
                if best is not None and best.score >= 100:
                    break
            if best is not None and best.score >= threshold:
                matches.append(best)
        # 同点なら長いキーワードを優先 (より具体的な語録なので)
        matches.sort(key=lambda m: (m.score, len(m.keyword.name)), reverse=True)
        if allow_overlap:
            return matches
        selected: list[Match] = []
        for match in matches:
            if not any(match.overlaps(s) for s in selected):
                selected.append(match)
        return selected
