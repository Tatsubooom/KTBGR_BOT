"""キーワードの曖昧一致検出。

Whisper の出力は同じ発話でも「漢字/ひらがな/カタカナ」表記や細かい音の聞き間違いで揺れるため、
以下の3つの表記に正規化して比較する。

- surface : NFKC正規化 + 小文字化 + カタカナ→ひらがな + 記号除去
- reading : 漢字を読み (ひらがな) に変換したもの
- romaji  : ヘボン式ローマ字 (音の近さを拾う)

いずれも長音 (ー/～)・小書き文字 (ぁ→あ)・3文字以上の同じ文字の連続 (ああああ→ああ) を揃えてから比較する。

一致の判定は2通りで、スコアの高い方を採用する。
1. 聞き間違い: キーワードとほぼ同じ並びがある (編集距離ベース、類似度 85 以上)
2. こじつけ: キーワードと発言に「内容のある」共通部分がある。漢字 2 文字以上の熟語を含むか、6 文字以上続く共通部分。
   「しかった」「できたら」のようなひらがなの語尾だけの共通部分は、関係ない一致になりやすいので根拠にしない。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import fugashi
import pykakasi
from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

log = logging.getLogger(__name__)

_kakasi = pykakasi.kakasi()
_tagger = fugashi.Tagger()
_STRIP_RE = re.compile(r"[^0-9a-zぁ-ゖ一-龯々〆]")
_ROMAJI_STRIP_RE = re.compile(r"[^0-9a-z]")
_REPEAT_RE = re.compile(r"(.)\1{2,}")
# 小書き文字は大きい文字に揃える
_SMALL_KANA = str.maketrans("ぁぃぅぇぉゃゅょゎゕゖ", "あいうえおやゆよわかけ")
# 読みの表記では促音「っ」も消す。Whisper は「いいっすよ」「いいすよ」のように促音を落としたり足したりする。
# 表記 (surface) には残す。消すと「えっち」が「えち」になり「声小さい (こえちいさい)」に一致するなど、短いキーワードが誤爆する
_SMALL_KANA_NO_SOKUON = str.maketrans("ぁぃぅぇぉゃゅょゎゕゖ", "あいうえおやゆよわかけ", "っ")

# これより短い表記は曖昧一致すると誤爆しやすいので完全一致 (部分文字列) のみ許可する
_MIN_FUZZY_LEN = {"surface": 3, "reading": 3, "reading_mecab": 3, "romaji": 5}
# 完全一致に必要な長さ。
# - 読み: 促音を消した分短くなるので 3 文字未満は比較しない (短いキーワードは表記の完全一致で拾う)
# - ローマ字: 音の区切りをまたいで誤爆する (「おつ」otsu が「バイト疲れた」baiTOTSUkareta に一致など)
_MIN_EXACT_LEN = {"surface": 0, "reading": 3, "reading_mecab": 3, "romaji": 5}

# 聞き間違い判定 (編集距離) に必要な最低スコア。MATCH_THRESHOLD を下げても、これより低い一致は採用しない。
# 編集距離は文字がバラバラに似ているだけでもスコアが出るので、長文 (歌詞の貼り付けなど) で関係ない一致が出やすい。
_EDIT_MIN_SCORE = 85

# こじつけ判定
_SEED_LEN = 3  # 候補を探す手がかりにする共通部分の長さ (漢字2文字の熟語は 2)
_CONTENT_KANA_LEN = 6  # 熟語を含まない共通部分はこの長さから「内容のある」共通部分とみなす
_COVERAGE_BLOCK_LEN = 3  # キーワードを占める割合は、この長さ以上の共通部分で数える
_JUKUGO_RE = re.compile(r"[一-龯々〆]{2}")  # 漢字2文字以上の並び (「全然」「試合」など)。「日も」のような1文字+送り仮名は含めない


def _kata_to_hira(text: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


def _clean(text: str, strip_re: re.Pattern, table: dict = _SMALL_KANA) -> str:
    return _REPEAT_RE.sub(r"\1\1", strip_re.sub("", text.translate(table)))


@dataclass(frozen=True)
class Forms:
    surface: str
    reading: str  # pykakasi の読み
    reading_mecab: str  # MeCab (UniDic) の読み
    romaji: str

    def items(self):
        return (
            ("surface", self.surface),
            ("reading", self.reading),
            ("reading_mecab", self.reading_mecab),
            ("romaji", self.romaji),
        )


def _mecab_reading(text: str) -> str:
    """MeCab の読み。辞書にない語は表記のまま使う。

    pykakasi と MeCab は読み間違える語が違う (pykakasi: 「今日は」→こんにちは、「眠落ち」→みんおち /
    MeCab: 「四天王」→よんてんのう、「皆」→かい) ので、両方の読みで照合して取りこぼしを減らす。
    """
    parts = []
    for word in _tagger(text):
        kana = getattr(word.feature, "kana", None)
        parts.append(kana if kana and kana != "*" else word.surface)
    return _kata_to_hira("".join(parts))


def to_forms(text: str) -> Forms:
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text))
    base = _kata_to_hira(normalized.lower())
    # pykakasi は改行の直前の語を二重に出力する (「メシア\n喘ぎ」→「めしあめしああえぎ」) ので空白にしてから変換する
    converted = _kakasi.convert(base)
    return Forms(
        surface=_clean(base, _STRIP_RE),
        reading=_clean("".join(part["hira"] for part in converted), _STRIP_RE, _SMALL_KANA_NO_SOKUON),
        reading_mecab=_clean(_mecab_reading(normalized).lower(), _STRIP_RE, _SMALL_KANA_NO_SOKUON),
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


def _seed_positions(text: str) -> dict[str, list[int]]:
    """こじつけ判定の手がかり (3文字の並び、漢字を含む2文字の並び) の出現位置。"""
    positions: dict[str, list[int]] = {}
    for size in (2, _SEED_LEN):
        for i in range(len(text) - size + 1):
            chunk = text[i : i + size]
            if size == _SEED_LEN or _JUKUGO_RE.fullmatch(chunk):
                positions.setdefault(chunk, []).append(i)
    return positions


def _is_content(chunk: str) -> bool:
    return bool(_JUKUGO_RE.search(chunk)) or len(chunk) >= _CONTENT_KANA_LEN


def shared_chunk_similarity(needle: str, haystack: str, positions: dict[str, list[int]]) -> tuple[float, float, float]:
    """キーワードと発言の共通部分によるこじつけ判定。スコア (0-99) と位置 (割合) を返す。

    - 内容のある共通部分があれば 70 + 30 × (キーワードを占める割合)
    - なければ 100 × (キーワードを占める割合)。語尾だけが共通する程度では 70 に届かない
    """
    n = len(needle)
    starts: set[int] = set()
    for size in (2, _SEED_LEN):
        for i in range(n - size + 1):
            for p in positions.get(needle[i : i + size], ()):
                starts.add(max(0, p - i))
    best = (0.0, 0.0, 0.0)
    seen: set[tuple[int, int]] = set()
    for start in starts:
        a, b = max(0, start - n // 2), min(len(haystack), start + n + n // 2)
        if (a, b) in seen:
            continue
        seen.add((a, b))
        window = haystack[a:b]
        blocks = [
            bl for bl in SequenceMatcher(None, needle, window, autojunk=False).get_matching_blocks() if bl.size >= 2
        ]
        if not blocks:
            continue
        coverage = sum(bl.size for bl in blocks if bl.size >= _COVERAGE_BLOCK_LEN) / n
        score = coverage * 100
        if any(_is_content(needle[bl.a : bl.a + bl.size]) for bl in blocks):
            score = max(score, 70 + 30 * coverage)
        score = min(score, 99.0)  # 完全一致 (100) と区別する
        if score > best[0]:
            first = a + min(bl.b for bl in blocks)
            last = a + max(bl.b + bl.size for bl in blocks)
            best = (score, first / len(haystack), last / len(haystack))
    return best


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
        seeds = {v: _seed_positions(getattr(text_forms, v)) for v in ("surface", "reading", "reading_mecab")}
        matches: list[Match] = []
        for keyword in self.keywords:
            threshold = keyword.threshold if keyword.threshold is not None else self.default_threshold
            best: Match | None = None
            for kw_forms in keyword.forms:
                for (variant, needle), (_, haystack) in zip(kw_forms.items(), text_forms.items()):
                    # 聞き間違い判定 (完全一致を含む)。85 未満は採用しない
                    edit_cutoff = max(threshold, _EDIT_MIN_SCORE)
                    score, start, end = window_similarity(
                        needle, haystack, _MIN_FUZZY_LEN[variant], edit_cutoff, _MIN_EXACT_LEN[variant]
                    )
                    candidates = [(score, start, end)] if score >= edit_cutoff else []
                    # こじつけ判定。キーワードのしきい値で判定する (しきい値 100 のキーワードは完全一致のみ)
                    if score < 100 and threshold < 100 and variant in seeds and len(needle) >= _SEED_LEN:
                        chunk = shared_chunk_similarity(needle, haystack, seeds[variant])
                        if chunk[0] >= threshold:
                            candidates.append(chunk)
                    for score, start, end in candidates:
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
