from types import SimpleNamespace

from ktbgr.combo import Hit, build_message, hits_in_order
from ktbgr.matcher import Keyword, KeywordMatcher

USER = SimpleNamespace(mention="<@1>", display_name="テスター")


def test_multiple_keywords_in_one_utterance():
    matcher = KeywordMatcher(
        [Keyword("やりますねぇ"), Keyword("頭にきますよ"), Keyword("当たり前だよなぁ？"), Keyword("ラーメン")], 70
    )
    found = matcher.find("頭にきますよ、当たり前だよなぁ？やりますねぇ")
    assert {m.keyword.name for m in found} == {"やりますねぇ", "頭にきますよ", "当たり前だよなぁ？"}


def test_overlapping_matches_are_counted_once():
    matcher = KeywordMatcher([Keyword("してはいけない"), Keyword("まずうちさぁ、屋上…あんだけど、焼いてかない？")], 70)
    text = "まずうちさあ屋上あるんだけど焼いていかない"
    assert len(matcher.find(text, allow_overlap=True)) == 2
    assert [m.keyword.name for m in matcher.find(text)] == ["まずうちさぁ、屋上…あんだけど、焼いてかない？"]


def test_short_romaji_does_not_match_across_mora():
    matcher = KeywordMatcher([Keyword("おつ")], 70)
    assert matcher.find("バイト疲れたー") == []


def test_single_match_uses_reply():
    matcher = KeywordMatcher([Keyword("ラーメン", reply="🍜 {name}「{text}」")], 70)
    text = "ラーメン食べたい"
    assert build_message(hits_in_order(text, matcher.find(text)), USER) == "🍜 テスター「ラーメン食べたい」"


def test_combo_message_in_order_of_appearance():
    matcher = KeywordMatcher([Keyword("寝落ち"), Keyword("ラーメン", label="🍜 ラーメン")], 70)
    text = "ラーメン食べてから寝落ちした"
    lines = build_message(hits_in_order(text, matcher.find(text)), USER).splitlines()
    assert "2 COMBO!" in lines[0] and text in lines[0]
    assert lines[1].endswith("🍜 ラーメン")
    assert lines[2].endswith("**寝落ち**")


def test_combo_across_utterances_shows_each_text():
    matcher = KeywordMatcher([Keyword("頭にきますよ"), Keyword("やりますねぇ"), Keyword("やったぜ")], 70)
    hits: list[Hit] = []
    for text in ["頭に来ますよ", "やりますね", "やったぜ"]:
        hits += hits_in_order(text, matcher.find(text))
    lines = build_message(hits, USER).splitlines()
    assert "3 COMBO!!" in lines[0]
    assert lines[1].endswith("「頭に来ますよ」") and lines[3].endswith("「やったぜ」")
