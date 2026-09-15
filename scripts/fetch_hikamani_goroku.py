"""ピクシブ百科事典「ヒカマニ語録」の表から語録を収集し、キーワードファイルを生成する。

使い方:
    python scripts/fetch_hikamani_goroku.py                  # data/hikamani_goroku.json を生成
    python scripts/fetch_hikamani_goroku.py --limit 250 --refresh

- HIKAKIN / SEIKIN / Masuo / 効果音 / コメント の表から収集し、知名度 (★の数) の高い順に --limit 件を採用
- 自動字幕の表は、事件・犯罪を連想させる誤字幕が大半なので対象外
- 性暴力・殺人などを含む語録は除外
- 表の「略称」「別名、表記揺れ」列と、改行で併記された表記を別表記 (aliases) にする (括弧内は注釈として除く)
- しきい値や無効化のルールは fetch_inmu_goroku.py と共通
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_inmu_goroku import (  # noqa: E402
    DISABLE_BELOW,
    EXACT_BELOW,
    FUZZY_SHORT_BELOW,
    FUZZY_SHORT_THRESHOLD,
    ROOT,
    USER_AGENT,
    reading_key,
    spoken,
)

SOURCE_URL = "https://dic.pixiv.net/a/%E3%83%92%E3%82%AB%E3%83%9E%E3%83%8B%E8%AA%9E%E9%8C%B2"
CACHE = ROOT / ".cache" / "hikamani_goroku_raw.html"

SKIP_SECTIONS = {"自動字幕(HIKAKIN)", "自動字幕(SEIKIN)", "〇〇キン"}
SPEAKERS = {"HIKAKIN": "HIKAKIN", "SEIKIN": "SEIKIN", "Masuo": "Masuo", "コメント": "コメント"}
# 事件・性暴力などを連想させる語録は、会話に反応させる用途に向かないので除外
EXCLUDE_WORDS = (
    "レイプ", "殺", "コロス", "ころす", "刺し", "テロ", "通り魔", "暴力団", "大麻", "薬物", "監禁", "遺体", "死体", "死ね",
)
# 別名列に書かれた説明文 (「ヒカマニ界隈における「草」」「設Xキンの場合」など) は表記ではないので除外
_ALIAS_NOTE_RE = re.compile(r"「|」|における|の場合|のこと|参照")
ASCII_DISABLE_BELOW = 4  # 「RED」など短い英字は英文の一部に一致しやすい
UNRATED_STARS = 3  # 知名度の列がない表 (アナル発言・効果音) の扱い
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


class _TableParser(HTMLParser):
    """見出し (h2-h4) の階層と、その下の表を集める。"""

    def __init__(self):
        super().__init__()
        self.heading: list[str] = []
        self.tables: list[dict] = []
        self._h = None
        self._htext = ""
        self._table = None
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag in ("h2", "h3", "h4"):
            self._h, self._htext = int(tag[1]), ""
        elif tag == "table":
            self._table = {"section": list(self.heading), "rows": []}
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = ""
        elif tag == "br" and self._cell is not None:
            self._cell += "\n"

    def handle_endtag(self, tag):
        if tag in ("h2", "h3", "h4") and self._h:
            self.heading = self.heading[: self._h - 2] + [self._htext.strip()]
            self._h = None
        elif tag in ("td", "th") and self._cell is not None:
            self._row.append(self._cell.strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._table["rows"].append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data):
        if self._h:
            self._htext += data
        if self._cell is not None:
            self._cell += data


def fetch_html(refresh: bool) -> tuple[str, str]:
    if CACHE.exists() and not refresh:
        print(f"キャッシュを使用: {CACHE}", file=sys.stderr)
        fetched_at = datetime.fromtimestamp(CACHE.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        return CACHE.read_text(encoding="utf-8"), fetched_at
    req = urllib.request.Request(SOURCE_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as res:
        html = res.read().decode("utf-8")
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(html, encoding="utf-8")
    return html, datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", _EMOJI_RE.sub("", text)).strip()


def variants_of(cell: str, split_commas: bool = False) -> list[str]:
    """セル内で改行 (別名列は読点も) で併記された表記を分ける。括弧内は注釈として除く。"""
    parts = re.split(r"[\n、,]" if split_commas else r"\n", cell)
    return [spoken(clean(p)) for p in parts if clean(p)]


def collect_rows(html: str) -> list[dict]:
    parser = _TableParser()
    parser.feed(html)
    rows = []
    for table in parser.tables:
        section = table["section"]
        if len(section) < 2 or section[0] != "語録一覧" or section[-1] in SKIP_SECTIONS:
            continue
        header, *body = table["rows"]
        for order, cells in enumerate(body):
            row = dict(zip(header, cells))
            quote = row.get("語録", "")
            if not clean(quote) or any(w in quote for w in EXCLUDE_WORDS):
                continue
            stars = row.get("知名度", "").count("★") or UNRATED_STARS
            alias_cells = [row.get(k, "") for k in header if "略称" in k or "表記揺れ" in k]
            rows.append({
                "quote": quote,
                "section": section,
                "speaker": SPEAKERS.get(section[1]),
                "video": row.get("元動画", ""),
                "stars": stars,
                "alias_cells": alias_cells,
                "order": (len(rows), order),
            })
    return rows


def build_entry(row: dict) -> dict | None:
    candidates = variants_of(row["quote"])
    if not candidates:
        return None
    for cell in row["alias_cells"]:
        candidates += [v for v in variants_of(cell, split_commas=True) if not _ALIAS_NOTE_RE.search(v)]

    name = candidates[0]
    seen = {reading_key(name)}
    aliases = []
    for c in candidates[1:]:
        key = reading_key(c)
        if len(key) >= DISABLE_BELOW and not key.isdigit() and key not in seen:
            seen.add(key)
            aliases.append(c)

    entry: dict = {"name": name}
    if aliases:
        entry["aliases"] = aliases
    name_key = reading_key(name)
    if len(name_key) < DISABLE_BELOW or (name_key.isascii() and not name_key.isdigit() and len(name_key) < ASCII_DISABLE_BELOW):
        entry["enabled"] = False
    length = min(len(reading_key(v)) for v in [name, *aliases])
    if length < EXACT_BELOW:
        entry["threshold"] = 100
    elif length < FUZZY_SHORT_BELOW:
        entry["threshold"] = FUZZY_SHORT_THRESHOLD

    title = clean(row["quote"].split("\n")[0])
    entry["title"] = title
    if row["speaker"]:
        entry["speaker"] = row["speaker"]
    entry["stars"] = row["stars"]
    if row["video"]:
        entry["video"] = clean(row["video"])
    entry["hotword"] = False

    label = f"{title}（{row['speaker']}）" if row["speaker"] else title
    entry["reply"] = "{name}「{text}」\nヒカマニ語録: " + label.replace("{", "{{").replace("}", "}}")
    entry["label"] = label
    return entry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", default=str(ROOT / "data" / "hikamani_goroku.json"))
    parser.add_argument("--limit", type=int, default=200, help="採用する件数 (知名度の高い順)")
    parser.add_argument("--refresh", action="store_true", help="キャッシュを使わず再取得する")
    args = parser.parse_args()

    html, fetched_at = fetch_html(args.refresh)
    rows = collect_rows(html)
    # 知名度の高い順。同じ知名度なら記事の掲載順
    rows.sort(key=lambda r: (-r["stars"], r["order"]))

    keywords, seen = [], set()
    for row in rows:
        entry = build_entry(row)
        if entry is None:
            continue
        key = reading_key(entry["name"])
        if key in seen:
            continue
        seen.add(key)
        keywords.append(entry)
        if len(keywords) >= args.limit:
            break

    output = {
        "source": {
            "name": "ピクシブ百科事典「ヒカマニ語録」",
            "url": SOURCE_URL,
            "fetched_at": fetched_at,
            "generator": "scripts/fetch_hikamani_goroku.py",
        },
        "keywords": keywords,
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    enabled = sum(1 for k in keywords if k.get("enabled", True))
    print(f"{out}: {len(keywords)}件 (候補 {len(rows)}件 / 有効 {enabled} / 無効 {len(keywords) - enabled})", file=sys.stderr)


if __name__ == "__main__":
    main()
