from ktbgr.matcher import Keyword, KeywordMatcher


def names(matcher: KeywordMatcher, text: str) -> list[str]:
    return [m.keyword.name for m in matcher.find(text)]


def test_exact_and_notation_variants():
    matcher = KeywordMatcher([Keyword("ラーメン"), Keyword("お疲れ様")], 80)
    assert names(matcher, "今日らーめん食べた") == ["ラーメン"]
    assert names(matcher, "みんなおつかれさまでした") == ["お疲れ様"]  # 漢字 ⇔ かな


def test_fuzzy_misrecognition():
    matcher = KeywordMatcher([Keyword("寝落ち")], 80)
    assert names(matcher, "もうねおちしそう") == ["寝落ち"]  # 漢字 ⇔ かな
    # 「眠落ち」は読みが「みんおち」になりスコアが足りない。長文での誤爆防止を優先して拾わない
    assert names(matcher, "もう眠落ちしそう") == []
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


def test_matches_sorted_by_score():
    matcher = KeywordMatcher([Keyword("してはいけない"), Keyword("まずうちさぁ、屋上…あんだけど、焼いてかない？")], 70)
    found = names(matcher, "まずうちさあ屋上あるんだけど焼いていかない")
    assert found[0] == "まずうちさぁ、屋上…あんだけど、焼いてかない？"


def test_long_text_does_not_match_short_keywords_by_chance():
    # 歌詞を貼り付けたときに、ローマ字や短いキーワードが偶然似た並びに一致していた
    lyrics = "\n".join([
        "田所浩二！田所浩二！", "王道を征く常夏のおっさん", "ネットの頂点に降り立つメシア", "喘ぎ声で命救うビースト",
        "いいよ！こいよ！ 我ら包む抱擁", "さあ今このアイスティーを飲み干そう", "お前やりますねぇスギ",
        "ネットでバカにする愚者たちよ", "悔い改めて彼に許しを請え", "ブッチッパ！ブッチッパ！ブッチッパ！",
        "BBの数だけ強くなる男", "INMU KING! INMU KING!",
    ])
    matcher = KeywordMatcher(
        [Keyword("ラーメン", threshold=75), Keyword("寝落ち"), Keyword("お疲れ様", aliases=["おつかれ", "おつ"]),
         Keyword("なんだこのオッサン⁉"), Keyword("ありますあります"), Keyword("ココアライオン"),
         Keyword("やりますねぇ！"), Keyword("ブッチッパ！"), Keyword("王道を征く")],
        70,
    )
    assert set(names(matcher, lyrics)) == {"やりますねぇ！", "ブッチッパ！", "王道を征く"}


def test_loose_match_on_shared_word():
    # こじつけ: 熟語や長い並びが共通していれば反応する
    matcher = KeywordMatcher([Keyword("おっ大丈夫か大丈夫か？"), Keyword("ケンちゃんまだ一回表、試合は始まったばっかりよ！")], 70)
    assert names(matcher, "マイクの音量大丈夫?聞こえてる?") == ["おっ大丈夫か大丈夫か？"]
    assert names(matcher, "さっきの試合惜しかったな") == ["ケンちゃんまだ一回表、試合は始まったばっかりよ！"]
    matcher = KeywordMatcher([Keyword("いいよ、来いよ！胸にかけて！胸に！")], 70)
    assert names(matcher, "いいよ！こいよ！ 我ら包む抱擁") == ["いいよ、来いよ！胸にかけて！胸に！"]


def test_no_match_on_shared_grammar_only():
    # 関係ない: 語尾や助詞だけが共通する場合は反応しない
    matcher = KeywordMatcher(
        [Keyword("10が、すごく欲しかったんですよね"), Keyword("ドジョウと俺のさ、子供ができたらどうする？"),
         Keyword("ラグビーってなんだよ"), Keyword("大切にしようよ～"), Keyword("今日も俺、恥ずかしい姿いっぱい晒すよ？")],
        70,
    )
    for text in ["さっきの試合惜しかったな", "駅前に新しいカフェできたらしいよ", "明日の会議って何時からだっけ",
                 "昼ごはん何にしようかな", "明日も早いからもう寝るね"]:
        assert names(matcher, text) == [], text


def test_newline_does_not_duplicate_words():
    from ktbgr.matcher import to_forms

    assert to_forms("メシア\n喘ぎ声").reading == to_forms("メシア 喘ぎ声").reading == "めしああえぎこえ"


def test_disabled_keyword_is_ignored():
    matcher = KeywordMatcher([Keyword("そう…", enabled=False)], 80)
    assert names(matcher, "そう…") == []


def test_short_keyword_requires_exact():
    matcher = KeywordMatcher([Keyword("おつ")], 80)
    assert names(matcher, "おつー") == ["おつ"]
    assert names(matcher, "おかしい") == []
