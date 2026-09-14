from ktbgr.matcher import Keyword, KeywordMatcher


def names(matcher: KeywordMatcher, text: str) -> list[str]:
    return [m.keyword.name for m in matcher.find(text)]


def test_exact_and_notation_variants():
    matcher = KeywordMatcher([Keyword("ラーメン"), Keyword("お疲れ様")], 80)
    assert names(matcher, "今日らーめん食べた") == ["ラーメン"]
    assert names(matcher, "みんなおつかれさまでした") == ["お疲れ様"]  # 漢字 ⇔ かな


def test_fuzzy_misrecognition():
    matcher = KeywordMatcher([Keyword("寝落ち")], 80)
    assert names(matcher, "もう眠落ちしそう") == ["寝落ち"]  # 誤変換でも読みが同じなら一致
    matcher = KeywordMatcher([Keyword("ミーティング")], 80)
    assert names(matcher, "このあとミーテングあるよ") == ["ミーティング"]  # 1文字の聞き間違い


def test_no_false_positive():
    matcher = KeywordMatcher([Keyword("ラーメン"), Keyword("おはよう")], 80)
    assert names(matcher, "今日はいい天気ですね") == []


def test_long_vowel_small_kana_and_repeats():
    matcher = KeywordMatcher([Keyword("いいゾ～これ"), Keyword("ぬわあああああん疲れたもおおおおおん")], 100)
    assert names(matcher, "いいぞーこれ") == ["いいゾ～これ"]
    matcher = KeywordMatcher([Keyword("当たり前だよなぁ？")], 100)
    assert names(matcher, "当たり前だよなあ") == ["当たり前だよなぁ？"]


def test_utterance_shorter_than_keyword_does_not_match_partially():
    matcher = KeywordMatcher([Keyword("まずうちさぁ、屋上…あんだけど、焼いてかない？")], 80)
    assert names(matcher, "まずうち") == []


def test_disabled_keyword_is_ignored():
    matcher = KeywordMatcher([Keyword("そう…", enabled=False)], 80)
    assert names(matcher, "そう…") == []


def test_short_keyword_requires_exact():
    matcher = KeywordMatcher([Keyword("おつ")], 80)
    assert names(matcher, "おつー") == ["おつ"]
    assert names(matcher, "おかしい") == []
