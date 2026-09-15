import json
from pathlib import Path

from ktbgr.matcher import KeywordMatcher

DATA = Path(__file__).parent.parent / "data" / "hikamani_goroku.json"


def test_hikamani_data_shape():
    keywords = json.loads(DATA.read_text(encoding="utf-8"))["keywords"]
    assert len(keywords) == 200
    stars = [k["stars"] for k in keywords]
    assert stars == sorted(stars, reverse=True)  # 知名度の高い順


def test_hikamani_excludes_crime_related_quotes():
    keywords = json.loads(DATA.read_text(encoding="utf-8"))["keywords"]
    for k in keywords:
        for word in ("レイプ", "殺", "コロス", "テロ", "通り魔"):
            assert word not in k["title"], k["title"]


def test_hikamani_quotes_are_detected():
    matcher = KeywordMatcher.from_files([DATA], 70)
    for text, expected in [
        ("何を四天王", "何を四天王"),
        ("下品だなぁ、そうに決まってる", "下品だなぁそうに決まってる"),
        ("ローション？", "ローション!?"),
    ]:
        assert expected in [m.keyword.name for m in matcher.find(text)], text
