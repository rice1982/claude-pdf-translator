"""工程(6): 数式保護のテスト。

対応関数: stage6.postprocess（工程6全体を代表する統括関数。単体アルファ
ベット変数の自動保護・数式断片の統合2種・翻訳済みMarkdownの書き出しを
この順でまとめて実行する）、およびそれを構成する
個々の関数（各検出・保護関数・write_translated_pages）単体。これらは
stage6/に置かれている（CLAUDE.md「テスト・実行運用規定」項目8参照）。
数式プレースホルダの復元（apply_restore）は工程(5)の責務のため対象外
（test_stage5.py参照）。
"""
from __future__ import annotations

import pytest

from mainCode.shared.shared import DocUnit
from mainCode.stage6.stage6 import (
    find_untranslated_fragment_candidates,
    merge_comparison_math_spans,
    merge_function_call_math_spans,
    postprocess,
    protect_confirmed_single_letter_leaks,
    write_translated_pages,
)


class TestWriteTranslatedPages:
    def test_write_translated_pages_preserves_tag_format(self, tmp_path):
        """write_translated_pagesが、page_XX_en.mdと同じタグ形式でja_textを
    書き出せているかを、自作unitsで直接検証する。process_pdf/MinerU/DeepLを
    一切経由しないため高速。"""
        units = [
            DocUnit(tag="P1-TITLE", kind="title", page=1, en_text="A Title", ja_text="タイトル"),
            DocUnit(
                tag="P1-FIG1",
                kind="figure_image",
                page=1,
                image_rel_path="images/fig_p1_1.png",
            ),
            DocUnit(
                tag="P1-FIG1-CAPTION-S1",
                kind="caption_sentence",
                page=1,
                en_text="Fig. 1: Example.",
                ja_text="図1: 例。",
            ),
            DocUnit(tag="P2-S1-body-S1", kind="body_sentence", page=2, en_text="Body.", ja_text="本文。"),
        ]

        output_dir = tmp_path / "write_translated_pages_output"
        output_dir.mkdir()
        written = write_translated_pages(units, output_dir)

        assert [p.name for p in written] == [
            "page_01_en.md",
            "page_01_ja.md",
            "page_02_en.md",
            "page_02_ja.md",
        ]

        page1_en_text = (output_dir / "page_01_en.md").read_text(encoding="utf-8")
        assert "[P1-TITLE] A Title" in page1_en_text
        assert "![P1-FIG1](images/fig_p1_1.png) [P1-FIG1]" in page1_en_text
        assert "[P1-FIG1-CAPTION-S1] Fig. 1: Example." in page1_en_text

        page1_ja = (output_dir / "page_01_ja.md").read_text(encoding="utf-8")
        assert "[P1-TITLE] タイトル" in page1_ja
        assert "![P1-FIG1](images/fig_p1_1.png) [P1-FIG1]" in page1_ja
        assert "[P1-FIG1-CAPTION-S1] 図1: 例。" in page1_ja

        page2_en_text = (output_dir / "page_02_en.md").read_text(encoding="utf-8")
        assert "[P2-S1-body-S1] Body." in page2_en_text

        page2_ja = (output_dir / "page_02_ja.md").read_text(encoding="utf-8")
        assert "[P2-S1-body-S1] 本文。" in page2_ja


class TestPostprocess:
    def test_postprocess_protects_leaked_letter_and_writes_pages(self, tmp_path):
        """postprocessが、apply_restore適用済み（ja_text設定済み）のunitsを
    受け取り、未保護のまま残った単体アルファベット変数（"z"）を
    protect_confirmed_single_letter_leaksにより$...$保護した上で、
    write_translated_pagesによりpage_XX_en.md/page_XX_ja.mdを実際に
    書き出し、その戻り値を返すことを確認する（工程6全体を単一の入出力を
    持つ関数として単体テストできることの確認）。数式の復元（apply_restore）
    は工程(5)の責務のため、この関数の入力としてja_text復元済みのunitsを
    直接与える（apply_restore自体の検証はtest_stage5.py参照）。
    """
        units = [
            DocUnit(
                tag="P1-S1-body-S1",
                kind="body_sentence",
                page=1,
                en_text="We decode a latent z back to an edge map.",
                ja_text="潜在変数zをエッジマップにデコードする。",
                translatable=True,
            )
        ]
        output_dir = tmp_path / "postprocess_output"
        output_dir.mkdir()

        written = postprocess(units, output_dir, log=lambda _msg: None)

        # 未保護のまま残った単体アルファベット変数が自動的に$...$保護されている
        assert units[0].en_text == "We decode a latent $z$ back to an edge map."
        assert units[0].ja_text == "潜在変数$z$をエッジマップにデコードする。"

        # write_translated_pagesの戻り値（書き出したファイル一覧）がそのまま返る
        assert [p.name for p in written] == ["page_01_en.md", "page_01_ja.md"]
        page1_ja = (output_dir / "page_01_ja.md").read_text(encoding="utf-8")
        assert "[P1-S1-body-S1] 潜在変数$z$をエッジマップにデコードする。" in page1_ja


    def test_postprocess_runs_single_letter_protection_before_comparison_merge(self, tmp_path):
        """postprocessのステップ順序（1: 単体アルファベット保護 →
        2-3: 断片統合）が、実際にこの順で意味を持つことを確認する回帰
        テスト。

        merge_comparison_math_spansは、比較演算子の左辺が既に$...$で
        保護済みのスパンであることを要求する。ここでは、比較対象の右辺
        （"K"）が単体アルファベット変数として別の場所にも登場する
        ケースを使う。"K"はprotect_confirmed_single_letter_leaksにより
        テキスト中の全ての孤立した出現箇所へ一括で$...$保護が適用される
        ため、"$M$ > K"のK側も保護されて初めて"$M$ > $K$"となり、
        merge_comparison_math_spansが1つの$...$（"$M > K$"）へまとめ
        直せるようになる。

        postprocess内で1と2-3の順序を実際に入れ替えるmutation testingを
        行い、既存のtest_stage6.py全体はもちろん全体テスト・統合テストを
        含むフルスイートでも1件も失敗しないことを確認した上で追加した
        （個々のstep関数を単体でテストするだけでは検知できない、
        postprocess全体としての順序依存）。"""
        units = [
            DocUnit(
                tag="P1-S1-body-S1",
                kind="body_sentence",
                page=1,
                en_text="Consider variable K and note that $M$ > K.",
                ja_text="変数Kを考え、$M$ > Kであることに注意する。",
                translatable=True,
            )
        ]
        output_dir = tmp_path / "postprocess_order_output"
        output_dir.mkdir()

        postprocess(units, output_dir, log=lambda _msg: None)

        assert units[0].en_text == "Consider variable $K$ and note that $M > K$."
        assert units[0].ja_text == "変数$K$を考え、$M > K$であることに注意する。"


class TestFragmentCandidateDetection:
    @pytest.mark.parametrize(
        "ja_text,expected",
        [
            ("潜在変数zをエッジマップにデコードし、t=1からt=0まで積分する。", ["z", "t=1", "t=0"]),
            # スペースを挟む"t = 1"も、途中で分断されず1つの断片としてまとまるか
            ("誘導された常微分方程式(ODE)を t = 1 から t = 0 まで積分する。", ["(ODE)", "t = 1", "t = 0"]),
            ("K個の離散ステップを用いて", ["K"]),
            ("条件付き生成 $p ( y \\mid x )$ として定式化し", []),  # 保護済み数式は候補にならない
            ("これは完全に日本語だけの文です。", []),
            # ギリシャ文字も、半角英数字と同様にDeepLが翻訳せず残す数式記号のため検出対象に含む。
            ("推論時にはスケールγを用いた分類器フリーガイダンスによって", ["γ"]),
            # 記号を含まない場合の4文字/5文字の境界（len(token) <= 4）を直接
            # 確認する（実データでは"BIPED"（5文字）等の固有名詞が誤検知
            # 候補に挙がったことで境界の存在自体は判明していたが、
            # test_stage6.py自身にはこの境界を直接確認する合成テストが
            # 無く、cache/配下の実データに依存する高コストなテストでしか
            # 検知できていなかった）。
            ("これはABCDという略語です。", ["ABCD"]),
            ("これはABCDEという略語です。", []),
        ],
    )
    def test_find_untranslated_fragment_candidates(self, ja_text, expected):
        """翻訳後も半角のまま残る、未検出の数式らしき候補の検出テスト。"""
        assert find_untranslated_fragment_candidates(ja_text) == expected


class TestProtectConfirmedSingleLetterLeaks:
    def test_protect_confirmed_single_letter_leaks_wraps_matching_occurrences(self):
        """未保護の単体アルファベット（例:"z"）が、en_text・ja_text双方の
        同じ箇所で$...$に置き換わるか。DeepLへの再翻訳は行わないため、
        テスト内でcall_deepl/apply_restoreは一切呼ばない。"""
        units = [
            DocUnit(
                tag="P1-S1-body-S1",
                kind="body_sentence",
                page=1,
                en_text="We decode a latent z back to an edge map.",
                ja_text="潜在変数zをエッジマップにデコードする。",
                translatable=True,
            )
        ]
        messages = protect_confirmed_single_letter_leaks(units, log=lambda _msg: None)
        assert len(messages) == 1
        assert "'z'" in messages[0]
        assert units[0].en_text == "We decode a latent $z$ back to an edge map."
        assert units[0].ja_text == "潜在変数$z$をエッジマップにデコードする。"


    def test_protect_confirmed_single_letter_leaks_does_not_touch_existing_math_spans(self):
        """既に$...$で保護済みの数式スパン内部の同名文字（"y"）を二重に$で
        囲まず、スパン外のstandaloneな"x"だけを保護することを確認する。"""
        units = [
            DocUnit(
                tag="P2-S6-2.1.preliminaries-S1",
                kind="body_sentence",
                page=2,
                en_text="Given input image x, we formulate it as $p ( y \\mid x )$.",
                ja_text="入力画像xが与えられたとき、$p ( y \\mid x )$として定式化する。",
                translatable=True,
            )
        ]
        messages = protect_confirmed_single_letter_leaks(units, log=lambda _msg: None)

        assert len(messages) == 1
        assert units[0].en_text == "Given input image $x$, we formulate it as $p ( y \\mid x )$."
        assert units[0].ja_text == "入力画像$x$が与えられたとき、$p ( y \\mid x )$として定式化する。"


    def test_protect_confirmed_single_letter_leaks_skips_multi_char_tokens(self):
        """"DiT"のような複数文字のトークンは、単体アルファベットではないため
        保護されず、en_text/ja_textが変化しないことを確認する。"""
        units = [
            DocUnit(
                tag="P2-FIG1-CAPTION-S3",
                kind="caption_sentence",
                page=2,
                en_text="a frozen DiT-based foundation model",
                ja_text="凍結されたDiTベースのファウンデーションモデル",
                translatable=True,
            )
        ]
        original_en, original_ja = units[0].en_text, units[0].ja_text
        messages = protect_confirmed_single_letter_leaks(units, log=lambda _msg: None)
        assert messages == []
        assert units[0].en_text == original_en
        assert units[0].ja_text == original_ja


    def test_protect_confirmed_single_letter_leaks_strips_unpaired_leading_paren(self):
        """地の文の丸括弧書き（例:"...patchifying x)."）の中で、開き括弧が
        離れた位置にあるため候補抽出時に"(x"のように直前の"("だけが巻き
        込まれるケースでも、括弧を剥がした上で単体アルファベットとして
        保護できるか（P8-S35-B.1.details-S4の実データ回帰に対応）。"""
        units = [
            DocUnit(
                tag="P8-S35-B.1.details-S4",
                kind="body_sentence",
                page=8,
                en_text="condition tokens $Z_c$ (obtained by encoding and patchifying x).",
                ja_text="条件トークン $Z_c$ (x のエンコードとパッチ適用によって取得される)。",
                translatable=True,
            )
        ]
        messages = protect_confirmed_single_letter_leaks(units, log=lambda _msg: None)

        assert len(messages) == 1
        assert "'x'" in messages[0]
        assert units[0].en_text == "condition tokens $Z_c$ (obtained by encoding and patchifying $x$)."
        assert units[0].ja_text == "条件トークン $Z_c$ ($x$ のエンコードとパッチ適用によって取得される)。"


    def test_protect_confirmed_single_letter_leaks_strips_unpaired_trailing_paren(self):
        """開き括弧が遠く離れており、候補抽出時に"Z)"のように直後の")"だけが
        巻き込まれるケースでも保護できるか（実データ回帰: "stock W, X, Y, Z)"
        でWXYは単独トークンとして既に保護される一方、Zだけ同型のバグで
        すり抜けていた）。"""
        units = [
            DocUnit(
                tag="P4-S1-2.1.market-S2",
                kind="body_sentence",
                page=4,
                en_text="a four-asset market ($G = 4$, with stock W, X, Y, Z), during a step.",
                ja_text="4つの資産市場（$G = 4$、株式W、X、Y、Z）、1ステップの間。",
                translatable=True,
            )
        ]
        messages = protect_confirmed_single_letter_leaks(units, log=lambda _msg: None)

        assert any("'Z'" in message for message in messages)
        assert "$Z$" in units[0].en_text
        assert "$Z$" in units[0].ja_text
        assert "Z)" not in units[0].en_text
        assert "Z）" not in units[0].ja_text


    def test_protect_confirmed_single_letter_leaks_does_not_touch_paired_paren_enumerators(self):
        """"(i)"のように開き・閉じ括弧の両方が候補token内に揃っている場合は
        列挙記号の可能性が高いため、単体アルファベットとして誤って$...$で
        囲まないことを確認する（実データ回帰: sample0.pdf/sample1.pdfの
        "Our key idea is to (i) adapt..., (ii) ..., and (iii) ..."で、
        "(i)"を数式変数と誤認してはならない）。"""
        units = [
            DocUnit(
                tag="P2-S4-2.method-S2",
                kind="body_sentence",
                page=2,
                en_text="Our key idea is to (i) adapt a strong foundation model.",
                ja_text="私たちの重要なアイデアは、(i) 軽量の条件注入を介して適応させることです。",
                translatable=True,
            )
        ]
        original_en, original_ja = units[0].en_text, units[0].ja_text

        messages = protect_confirmed_single_letter_leaks(units, log=lambda _msg: None)

        assert messages == []
        assert units[0].en_text == original_en
        assert units[0].ja_text == original_ja


    def test_protect_confirmed_single_letter_leaks_does_not_corrupt_letter_embedded_in_other_word(self):
        """確定した単体アルファベット候補（"x"）を保護する際、同じ文字が
        別の単語（"context"）の内部にも偶然出現している場合に、その単語の
        中まで誤って$...$で囲んでしまわないか（"context"が"conte$x$t"に
        壊れないことを確認する）。

        _isolated_token_re（半角英数字に前後を挟まれていない孤立した出現
        箇所にのみマッチする正規表現）を機械的に壊しても、test_stage6.py
        内のどのテストも失敗しないことをmutation testingで確認した
        （実際にこの欠落を検知できていたのは、cache/配下の実データに
        依存する高コストなtest_integration.pyの許可リスト突合テストだけ
        だった）。合成データでも直接検知できるよう追加する。"""
        units = [
            DocUnit(
                tag="P1-S1-body-S1",
                kind="body_sentence",
                page=1,
                en_text="We use x in this context.",
                ja_text="このcontextの中でxを使用する。",
                translatable=True,
            )
        ]
        messages = protect_confirmed_single_letter_leaks(units, log=lambda _msg: None)

        assert len(messages) == 1
        assert units[0].en_text == "We use $x$ in this context."
        assert units[0].ja_text == "このcontextの中で$x$を使用する。"


    def test_protect_confirmed_single_letter_leaks_skips_non_translatable_units(self):
        """翻訳対象外のunitは、単体アルファベットが残っていても保護しないか。"""
        units = [DocUnit(tag="P1-EQ1-LATEX", kind="equation_latex", page=1, en_text="z", ja_text="z", translatable=False)]
        original_en = units[0].en_text
        assert protect_confirmed_single_letter_leaks(units, log=lambda _msg: None) == []
        assert units[0].en_text == original_en


class TestMergeFunctionCallMathSpans:
    def test_merge_function_call_math_spans_merges_matching_occurrences(self):
        """"P($A$, $B$)"のように、関数呼び出し記法の引数だけが個別に保護されて
        いる箇所が、識別子・括弧を含めた1つの$...$（"$P(A, B)$"）にまとまるか。
        """
        units = [
            DocUnit(
                tag="P1-S1-body-S1",
                kind="body_sentence",
                page=1,
                en_text="We formulate this as P($A$, $B$).",
                ja_text="これをP($A$, $B$)として定式化する。",
                translatable=True,
            )
        ]
        messages = merge_function_call_math_spans(units, log=lambda _msg: None)
        assert len(messages) == 1
        assert units[0].en_text == "We formulate this as $P(A, B)$."
        assert units[0].ja_text == "これを$P(A, B)$として定式化する。"


    def test_merge_function_call_math_spans_merges_conditional_probability_pattern(self):
        """"P($A$ | $B$)"のような、条件付き確率の"|"区切り表記を含む引数も
        1つの$...$（"$P(A | B)$"）にまとまるかを確認する。"""
        units = [
            DocUnit(
                tag="P1-S1-body-S6",
                kind="body_sentence",
                page=1,
                en_text="We formulate this as P($A$ | $B$).",
                ja_text="これをP($A$ | $B$)として定式化する。",
                translatable=True,
            )
        ]
        messages = merge_function_call_math_spans(units, log=lambda _msg: None)
        assert len(messages) == 1
        assert units[0].en_text == "We formulate this as $P(A | B)$."
        assert units[0].ja_text == "これを$P(A | B)$として定式化する。"


    def test_merge_function_call_math_spans_merges_prime_notation_pattern(self):
        """"P($X$'|$Y$)"のような、プライム記号（"X'"の更新後の値を表す表記）を
        含む引数も1つの$...$にまとまるかを確認する。DeepLの実翻訳結果では
        直立アポストロフィ"'"ではなくU+2019（右シングルクォーテーション
        マーク"'"）が使われることがあるため、両方の表記を確認する（実データ
        ではen_text・ja_textとも同じ文字が使われる。プライム記号は$...$の
        外側にある地の文の一部であり、en_textはMinerU抽出のまま、ja_textは
        DeepLがその文字をそのまま素通しするため、両者で表記が食い違う実例は
        確認されていない）。"""
        for quote_char in ("'", "’"):
            units = [
                DocUnit(
                    tag="P1-S1-body-S7",
                    kind="body_sentence",
                    page=1,
                    en_text=f"We formulate this as P($X${quote_char}|$Y$).",
                    ja_text=f"これをP($X${quote_char}|$Y$)として定式化する。",
                    translatable=True,
                )
            ]
            messages = merge_function_call_math_spans(units, log=lambda _msg: None)
            assert len(messages) == 1
            assert units[0].en_text == f"We formulate this as $P(X{quote_char}|Y)$."
            assert units[0].ja_text == f"これを$P(X{quote_char}|Y)$として定式化する。"


    def test_merge_function_call_math_spans_skips_when_no_protected_span(self):
        """引数部に$...$を含まない関数呼び出し（例:"P(A, B)"が未保護のまま）は
        対象外であり、新規の保護は行わないことを確認する。"""
        units = [
            DocUnit(
                tag="P1-S2-body-S2",
                kind="body_sentence",
                page=1,
                en_text="We formulate this as P(A, B).",
                ja_text="これをP(A, B)として定式化する。",
                translatable=True,
            )
        ]
        original_en, original_ja = units[0].en_text, units[0].ja_text
        messages = merge_function_call_math_spans(units, log=lambda _msg: None)
        assert messages == []
        assert units[0].en_text == original_en
        assert units[0].ja_text == original_ja


    def test_merge_function_call_math_spans_skips_when_space_before_paren(self):
        """識別子と"("の間に空白がある場合（"BSDS500 ($K$=10)"のような固有
        名詞＋ハイパーパラメータの丸括弧書き）は、数式の関数呼び出しと区別が
        付かないため対象外とし、書き換えないことを確認する。"""
        units = [
            DocUnit(
                tag="P1-S3-body-S3",
                kind="caption_sentence",
                page=1,
                en_text="BSDS500 ($K$=10)",
                ja_text="BSDS500 ($K$=10)",
                translatable=True,
            )
        ]
        original_en, original_ja = units[0].en_text, units[0].ja_text
        messages = merge_function_call_math_spans(units, log=lambda _msg: None)
        assert messages == []
        assert units[0].en_text == original_en
        assert units[0].ja_text == original_ja


    def test_merge_function_call_math_spans_skips_underscore_joined_identifiers(self):
        """"D_KL($p$, $q$)"のようなアンダースコア区切りの複合識別子は、識別子
        直前条件が"_"も除外対象としているため、"KL"だけを切り出して誤って
        マージすることがないか（"D_"が地の文に取り残される破損を防げているか）
        を確認する。"""
        units = [
            DocUnit(
                tag="P1-S4-body-S4",
                kind="body_sentence",
                page=1,
                en_text="We minimize D_KL($p$, $q$).",
                ja_text="D_KL($p$, $q$)を最小化する。",
                translatable=True,
            )
        ]
        original_en, original_ja = units[0].en_text, units[0].ja_text
        messages = merge_function_call_math_spans(units, log=lambda _msg: None)
        assert messages == []
        assert units[0].en_text == original_en
        assert units[0].ja_text == original_ja


    def test_merge_function_call_math_spans_skips_identifier_longer_than_four_chars(self):
        """識別子部分は英字1〜4文字に限定されており（コメント: "長い固有
        名詞・英単語との誤検知を避けるため上限を設けている"）、5文字以上の
        普通の英単語（例:"Consider"）の直後に偶然$...$を含む丸括弧書きが
        続いても、関数呼び出し記法として誤ってまとめ直されないことを
        確認する。

        `_FUNC_CALL_WITH_MATH_RE`の`{1,4}`を`{1,8}`へ緩めても、既存テスト・
        フルスイート286件のいずれも1件も失敗しないことをmutation testingで
        確認した。実際に緩めると"Consider($x$) carefully..."が
        "$Consider(x)$ carefully..."という数式に化けてしまう（英文の一部が
        誤って数式として描画される）ことを動的に確認した上で追加した。"""
        units = [
            DocUnit(
                tag="P1-S5-body-S5",
                kind="body_sentence",
                page=1,
                en_text="Consider($x$) carefully as a strategy.",
                ja_text="戦略としてConsider($x$)を慎重に検討する。",
                translatable=True,
            )
        ]
        original_en, original_ja = units[0].en_text, units[0].ja_text
        messages = merge_function_call_math_spans(units, log=lambda _msg: None)
        assert messages == []
        assert units[0].en_text == original_en
        assert units[0].ja_text == original_ja


    def test_merge_function_call_math_spans_skips_non_translatable_units(self):
        """翻訳対象外のunitは、関数呼び出し形式の断片が残っていても書き換え
        ないか。"""
        units = [
            DocUnit(
                tag="P1-EQ1-LATEX",
                kind="equation_latex",
                page=1,
                en_text="P($A$, $B$)",
                ja_text="P($A$, $B$)",
                translatable=False,
            )
        ]
        original_en = units[0].en_text
        assert merge_function_call_math_spans(units, log=lambda _msg: None) == []
        assert units[0].en_text == original_en


    def test_merge_function_call_math_spans_does_not_redetect_independently_on_en_text(self):
        """検出はja_textに対してのみ行われ、en_text単独に対して独立に正規表現
        を再実行しないことを確認する回帰テスト。

        sample2.pdfの実データで見つかった不具合の再現ケース: en_textが
        "min(1, $\\frac{...}{...})$ , and when $w$ and $v$ differ, ..."の
        ように、無関係な後続の英文プロパーの中に別の$...$スパン（$w$・$v$）
        を含む場合、もしen_textに対して独立に検出をやり直すと、"min("から
        はるか後方の無関係な")"までを1つの$...$に誤って巻き込んでしまう
        （引数用の文字クラスが英単語・スペース・カンマをほぼ無制限に許容
        するため）。ja_text側にはこのパターンが存在しない（＝日本語の地の文
        に区切られるため誤爆しない）ケースを再現し、en_textが誤って巻き込ま
        れず、翻訳対象外のまま変化しないことを検証する。"""
        en_text = (
            "the probability is min(1, $\\frac{P(A)}{P(B)})$ , and when $w$ and $v$ "
            "differ significantly, this probability is small)."
        )
        units = [
            DocUnit(
                tag="P1-S8-body-S8",
                kind="body_sentence",
                page=1,
                en_text=en_text,
                ja_text="確率はminの値になる。$w$と$v$が大きく異なる場合、この確率は小さくなる。",
                translatable=True,
            )
        ]
        original_en = units[0].en_text
        messages = merge_function_call_math_spans(units, log=lambda _msg: None)
        assert messages == []
        assert units[0].en_text == original_en


    def test_merge_function_call_math_spans_leaves_en_text_untouched_when_not_found_verbatim(self):
        """ja_textで検出された文字列（マージ"前"の形。"P($A$, $B$)"のように
        引数だけ個別に保護済み）がen_textに一字一句一致しない場合、en_textには
        触れず、ja_textだけが書き換わることを確認する。"""
        units = [
            DocUnit(
                tag="P1-S9-body-S9",
                kind="body_sentence",
                page=1,
                en_text="We formulate this as P(A, B) in the original text.",
                ja_text="これをP($A$, $B$)として定式化する。",
                translatable=True,
            )
        ]
        original_en = units[0].en_text
        messages = merge_function_call_math_spans(units, log=lambda _msg: None)
        assert len(messages) == 1
        assert units[0].en_text == original_en
        assert units[0].ja_text == "これを$P(A, B)$として定式化する。"


class TestMergeComparisonMathSpans:
    def test_merge_comparison_math_spans_merges_matching_occurrences(self):
        """"$K$ > 1"のように、既存の$...$スパンの直後に比較演算子と数値が
        地の文として続く箇所が、1つの$...$（"$K > 1$"）にまとまるか。"""
        units = [
            DocUnit(
                tag="P1-S1-body-S1",
                kind="body_sentence",
                page=1,
                en_text="multi-step inference ($K$ > 1) achieves better results.",
                ja_text="多段階推論（$K$ > 1）はより良い結果を達成する。",
                translatable=True,
            )
        ]
        messages = merge_comparison_math_spans(units, log=lambda _msg: None)
        assert len(messages) == 1
        assert units[0].en_text == "multi-step inference ($K > 1$) achieves better results."
        assert units[0].ja_text == "多段階推論（$K > 1$）はより良い結果を達成する。"


    def test_merge_comparison_math_spans_merges_various_operators(self):
        """">"以外の比較演算子（"<", ">=", "<=", "=="）でも同様にまとまるかを
        確認する。"""
        units = [
            DocUnit(
                tag=f"P1-S{i}-body-S{i}",
                kind="body_sentence",
                page=1,
                en_text=text,
                ja_text=text,
                translatable=True,
            )
            for i, text in enumerate(
                ["$p$ < 0.05 is significant.", "$x$ >= 10 holds.", "$y$ <= 0 fails.", "$z$ == 1 succeeds."],
                start=1,
            )
        ]
        merge_comparison_math_spans(units, log=lambda _msg: None)
        assert units[0].en_text == "$p < 0.05$ is significant."
        assert units[1].en_text == "$x >= 10$ holds."
        assert units[2].en_text == "$y <= 0$ fails."
        assert units[3].en_text == "$z == 1$ succeeds."


    def test_merge_comparison_math_spans_merges_when_right_side_is_math_span(self):
        """右辺が別の$...$スパン（例:"$K$ > $M$"）の場合も、中身だけ展開して
        1つの$...$（"$K > M$"）にまとまるかを確認する。"""
        units = [
            DocUnit(
                tag="P1-S5-body-S5",
                kind="body_sentence",
                page=1,
                en_text="This holds when $K$ > $M$.",
                ja_text="これは$K$ > $M$のとき成立する。",
                translatable=True,
            )
        ]
        messages = merge_comparison_math_spans(units, log=lambda _msg: None)
        assert len(messages) == 1
        assert units[0].en_text == "This holds when $K > M$."
        assert units[0].ja_text == "これは$K > M$のとき成立する。"


    def test_merge_comparison_math_spans_skips_when_right_side_is_word(self):
        """右辺が数値でも$...$スパンでもない自然文の単語（例:"$X$ > many
        models"）は、比較記号を含む地の文と区別できないため対象外とし、
        書き換えないことを確認する。"""
        units = [
            DocUnit(
                tag="P1-S6-body-S6",
                kind="body_sentence",
                page=1,
                en_text="$X$ > many models in this comparison.",
                ja_text="$X$ > many models in this comparison.",
                translatable=True,
            )
        ]
        original_en, original_ja = units[0].en_text, units[0].ja_text
        messages = merge_comparison_math_spans(units, log=lambda _msg: None)
        assert messages == []
        assert units[0].en_text == original_en
        assert units[0].ja_text == original_ja


    def test_merge_comparison_math_spans_skips_html_sup_tag(self):
        """"$X$</sup>"のようなHTML上付きタグ（"<sup>"は数字でも$でもなく
        文字始まり）は、比較演算子の"<"と誤認しないことを確認する。"""
        units = [
            DocUnit(
                tag="P1-S7-body-S7",
                kind="body_sentence",
                page=1,
                en_text="Panasonic$X$</sup> Holdings Corporation",
                ja_text="Panasonic$X$</sup> Holdings Corporation",
                translatable=True,
            )
        ]
        original_en, original_ja = units[0].en_text, units[0].ja_text
        messages = merge_comparison_math_spans(units, log=lambda _msg: None)
        assert messages == []
        assert units[0].en_text == original_en
        assert units[0].ja_text == original_ja


    def test_merge_comparison_math_spans_skips_non_translatable_units(self):
        """翻訳対象外のunitは、比較演算子付きの断片が残っていても書き換え
        ないか。"""
        units = [
            DocUnit(
                tag="P1-EQ2-LATEX",
                kind="equation_latex",
                page=1,
                en_text="$K$ > 1",
                ja_text="$K$ > 1",
                translatable=False,
            )
        ]
        original_en = units[0].en_text
        assert merge_comparison_math_spans(units, log=lambda _msg: None) == []
        assert units[0].en_text == original_en
