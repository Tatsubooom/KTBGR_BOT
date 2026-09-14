"""真夏の夜の淫夢Wiki の「カテゴリ:淫夢語録」から語録を収集し、キーワードファイルを生成する。

使い方:
    python scripts/fetch_inmu_goroku.py                   # data/inmu_goroku.json を生成
    python scripts/fetch_inmu_goroku.py -o out.json --refresh

- カテゴリ配下 (人物別・五十音順などのサブカテゴリ含む) の語録ページを再帰的に収集
- 各ページの {{語録}} テンプレートから「読み方」「発言者」を取得
- 語録ページへのリダイレクト (表記ゆれ) と {{コピペ用|...}} の簡易表記を別表記 (aliases) として追加
- 「（困惑）」のような注釈は発話されないので、検出用の表記からは除去する
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ktbgr.matcher import to_forms  # noqa: E402

API = "https://wiki.yjsnpi.nu/w/api.php"
WIKI = "https://wiki.yjsnpi.nu/wiki/"
ROOT_CATEGORY = "淫夢語録"
USER_AGENT = "KTBGR_BOT goroku collector (https://github.com/Tatsubooom/KTBGR_BOT)"
CACHE = ROOT / ".cache" / "inmu_goroku_raw.json"

# 読み (ひらがな・長音除去後) の長さによる扱い
# 「言葉狩り」用途なので基本はゆるめ。ただし2文字以下 (「そう」「なに」など) はほぼ全発言に反応してしまうので無効
DISABLE_BELOW = 3  # これ未満は無効化 (enabled: false)
EXACT_BELOW = 4  # これ未満は完全一致のみ
FUZZY_SHORT_BELOW = 7  # これ未満はやや厳しめのしきい値
FUZZY_SHORT_THRESHOLD = 90


def api(**params) -> dict:
    params.update(format="json", formatversion="2")
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as res:
                data = json.load(res)
            time.sleep(0.3)  # サーバーに負荷をかけすぎない
            return data
        except Exception as e:  # noqa: BLE001
            if attempt == 4:
                raise
            print(f"  retry ({e})", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    raise AssertionError


def query_all(**params):
    cont: dict = {}
    while True:
        data = api(action="query", **params, **cont)
        yield data
        if "continue" not in data:
            return
        cont = data["continue"]


def crawl_category(root: str) -> tuple[list[str], list[str]]:
    """カテゴリを再帰的にたどり、(記事ページ, 巡回したカテゴリ) を返す。"""
    pages: dict[str, None] = {}
    seen: set[str] = set()
    queue = [root]
    while queue:
        cat = queue.pop(0)
        if cat in seen:
            continue
        seen.add(cat)
        for data in query_all(list="categorymembers", cmtitle=f"Category:{cat}", cmlimit="500"):
            for m in data["query"]["categorymembers"]:
                if m["ns"] == 14:
                    queue.append(m["title"].split(":", 1)[1])
                elif m["ns"] == 0:
                    pages[m["title"]] = None
    return list(pages), sorted(seen)


def fetch_pages(titles: list[str]) -> dict[str, dict]:
    """wikitext とリダイレクトを取得する。リダイレクトページは転送先にまとめる。"""
    result: dict[str, dict] = {}
    for i in range(0, len(titles), 50):
        batch = titles[i : i + 50]
        print(f"  {i + len(batch)}/{len(titles)}", file=sys.stderr)
        for data in query_all(
            titles="|".join(batch),
            redirects="1",
            prop="revisions|redirects",
            rvprop="content",
            rvslots="main",
            rdlimit="max",
        ):
            for page in data["query"].get("pages", []):
                if page.get("missing"):
                    continue
                entry = result.setdefault(page["title"], {"wikitext": None, "redirects": []})
                if "revisions" in page:
                    entry["wikitext"] = page["revisions"][0]["slots"]["main"]["content"]
                for rd in page.get("redirects", []):
                    if rd["ns"] == 0 and rd["title"] not in entry["redirects"]:
                        entry["redirects"].append(rd["title"])
    return result


# ---- wikitext 解析 ----
_LINK_RE = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]")
_TAG_RE = re.compile(r"<[^>]+>")
_ANNOTATION_RE = re.compile(r"[（(][^（）()]*[）)]")
_COPIPE_RE = re.compile(r"\{\{\s*コピペ用\s*\|([^}]*)\}\}")


def template_fields(wikitext: str) -> dict[str, str] | None:
    """{{語録}} (一部は {{辞典}}) テンプレートの引数を返す。テンプレートがなければ None。"""
    m = re.search(r"\{\{\s*(?:語録|辞典)\s*(\|.*?)\}\}", wikitext, re.S)
    if not m:
        return None
    fields = {}
    for part in re.split(r"\n\s*\|", "\n" + m.group(1).lstrip("|")):
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key.strip()] = value.strip()
    return fields


def plain(text: str) -> str:
    text = _LINK_RE.sub(r"\1", text)
    text = _TAG_RE.sub("", text)
    return text.replace("'''", "").replace("''", "").strip()


def spoken(text: str) -> str:
    """「（困惑）」などの注釈を除いた、実際に発話される部分。"""
    stripped = _ANNOTATION_RE.sub("", text).strip()
    return stripped or text


def reading_key(text: str) -> str:
    return to_forms(text).reading


def build_entry(title: str, page: dict) -> dict:
    wikitext = page["wikitext"] or ""
    fields = template_fields(wikitext) or {}

    candidates = [spoken(title)]
    candidates += [spoken(r) for r in page["redirects"]]
    candidates += [spoken(plain(c)) for c in _COPIPE_RE.findall(wikitext)]
    if fields.get("読み方"):
        candidates.append(spoken(plain(fields["読み方"])))

    # 読みが同じ表記は重複なのでまとめる。短すぎる別表記 (「110」など) は誤爆の元なので捨てる
    name = candidates[0]
    seen = {reading_key(name)}
    aliases = []
    for c in candidates[1:]:
        key = reading_key(c)
        if c and len(key) >= DISABLE_BELOW and not key.isdigit() and key not in seen:
            seen.add(key)
            aliases.append(c)

    entry: dict = {"name": name}
    if aliases:
        entry["aliases"] = aliases
    if len(reading_key(name)) < DISABLE_BELOW:
        entry["enabled"] = False
    # しきい値は一番短い表記に合わせる (短い表記ほど曖昧一致で誤爆しやすい)
    length = min(len(reading_key(v)) for v in [name, *aliases])
    if length < EXACT_BELOW:
        entry["threshold"] = 100
    elif length < FUZZY_SHORT_BELOW:
        entry["threshold"] = FUZZY_SHORT_THRESHOLD

    speaker = plain(fields.get("発言者", ""))
    entry["title"] = title
    if speaker:
        entry["speaker"] = speaker
    if fields.get("由来"):
        entry["origin"] = plain(fields["由来"])
    entry["url"] = WIKI + urllib.parse.quote(title.replace(" ", "_"))
    entry["hotword"] = False  # 数百件を Whisper のヒントに渡すと認識が崩れるため無効

    label = f"{title}（{speaker}）" if speaker else title
    entry["reply"] = "{name}「{text}」\n淫夢語録: " + label.replace("{", "{{").replace("}", "}}")
    entry["label"] = label  # コンボ表示用 (format しない)
    return entry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", default=str(ROOT / "data" / "inmu_goroku.json"))
    parser.add_argument("--refresh", action="store_true", help="キャッシュを使わず再取得する")
    args = parser.parse_args()

    if CACHE.exists() and not args.refresh:
        raw = json.loads(CACHE.read_text(encoding="utf-8"))
        print(f"キャッシュを使用: {CACHE}", file=sys.stderr)
    else:
        print(f"カテゴリ:{ROOT_CATEGORY} を巡回中…", file=sys.stderr)
        titles, categories = crawl_category(ROOT_CATEGORY)
        print(f"{len(categories)}カテゴリ / {len(titles)}ページ。本文を取得中…", file=sys.stderr)
        raw = {
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "categories": categories,
            "pages": fetch_pages(titles),
        }
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    pages = raw["pages"]
    # リダイレクトの転送先が同じページは fetch_pages でまとまっている。語録テンプレートのないページ (一覧ページなど) は除外
    keywords = [
        build_entry(title, page)
        for title, page in sorted(pages.items(), key=lambda kv: unicodedata.normalize("NFKC", kv[0]))
        if page["wikitext"] and template_fields(page["wikitext"]) is not None
    ]

    output = {
        "source": {
            "name": "真夏の夜の淫夢Wiki カテゴリ:淫夢語録",
            "url": WIKI + urllib.parse.quote("カテゴリ:" + ROOT_CATEGORY),
            "fetched_at": raw["fetched_at"],
            "generator": "scripts/fetch_inmu_goroku.py",
        },
        "keywords": keywords,
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    enabled = sum(1 for k in keywords if k.get("enabled", True))
    print(f"{out}: {len(keywords)}件 (有効 {enabled} / 無効 {len(keywords) - enabled})", file=sys.stderr)


if __name__ == "__main__":
    main()
