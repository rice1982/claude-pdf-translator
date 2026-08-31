"""shared.py: 複数工程から共通して呼ばれるデータ構造・関数のテスト。

対応関数: ``protect``/``restore``（数式プレースホルダの退避・復元）・
``filter_translatable_units``（translatable=Trueのunitだけを列挙する共通
フィルタ。工程(4)〜(6)から使われる）。``DocUnit``は工程(3)〜工程(7)を
貫通する共有データ構造だが、フィールドの型定義のみでロジックを持たない
ため専用のテストは設けない（工程3〜7の各テストを通じて間接的に使われ
続けることで、フィールド構成の妥当性は実質的に検証される）。``log``は
標準出力への薄いラッパーで、print文の副作用は通常テスト対象としない
ため専用のテストは設けない（関数自身のdocstring参照）。

ここでは、``protect``/``restore``/``filter_translatable_units``のうち、特定の
工程の役割（stage3の``protect_units``/``normalize``・stage5の
``apply_restore``等）に紐づかない、shared.py自身の入出力契約を直接
対象とする。特定の工程の役割として既に検証されている振る舞いは、重複を
避けるためここでは再テストせず、該当する工程別テストファイルへの参照
コメントのみを残す。
"""
from __future__ import annotations

from mainCode.shared.shared import DocUnit, filter_translatable_units, protect, restore


class TestProtect:
    # 単一のインライン数式スパンを1つのプレースホルダへ置換する基本の
    # 振る舞いは test_stage3.py::TestProtectUnits::
    # test_protect_units_sets_protected_text_only_for_translatable_units、
    # \textless/\textgreaterの正規化（_normalize_math_escape）の適用範囲は
    # test_stage3.py::TestNormalize::
    # test_normalize_converts_textless_textgreater_only_inside_math_spans、
    # 実データに含まれる多数の数式スパンでのラウンドトリップ確認は
    # test_stage3.py::TestNormalize::
    # test_math_protection_round_trips_all_real_inline_math で検証済み
    # （重複を避けここでは省略）。

    def test_protect_returns_text_unchanged_and_empty_spans_when_no_math_present(self):
        text = "no math here"
        assert protect(text) == (text, [])


    def test_protect_leaves_unpaired_dollar_sign_untouched(self):
        """閉じられていない単独の$（ペアが無い）は数式スパンとして
    マッチせず、そのまま残ることを確認する。"""
        text = "Cost is $5 total, no closing delimiter"
        assert protect(text) == (text, [])


    def test_protect_captures_display_math_as_single_span_not_split_into_inline(self):
        """$$...$$（ディスプレイ数式）が、$...$（インライン数式）2つに
    分割されず、1つのスパンとして捕捉されることを確認する。"""
        protected, spans = protect("Before $$a=b$$ after")
        assert protected == "Before __MATH0__ after"
        assert spans == ["$$a=b$$"]


    def test_protect_assigns_sequential_indices_to_multiple_spans(self):
        """複数の数式スパンに、出現順で0始まりの連番インデックスが
    割り当てられることを確認する。"""
        protected, spans = protect("First $a$ then $b$ then $c$.")
        assert protected == "First __MATH0__ then __MATH1__ then __MATH2__."
        assert spans == ["$a$", "$b$", "$c$"]


class TestRestore:
    # 単一プレースホルダの基本の復元、およびプレースホルダ番号が
    # math_spansの範囲外だった場合のフォールバック（プレースホルダ文字列
    # をそのまま残す）は test_stage5.py::TestApplyRestore::
    # test_apply_restore_writes_ja_text_from_raw_results_with_math_restored
    # / test_apply_restore_leaves_placeholder_when_index_is_out_of_range
    # で検証済み（重複を避けここでは省略）。

    def test_restore_returns_text_unchanged_when_no_placeholder_present(self):
        assert restore("no placeholder text", ["$x$"]) == "no placeholder text"


    def test_restore_leaves_placeholder_when_index_equals_spans_length_exactly(self):
        """test_stage5.py::test_apply_restore_leaves_placeholder_when_index_is_out_of_range
    は範囲外indexとして5（spans長1）という、境界からかけ離れた値しか
    使っておらず、「ちょうど1つだけ外れた」境界値（index == len(spans)）
    は restore単体・apply_restore経由のどちらからも一度もテストされて
    いなかった（mutation testingで`index < len(spans)`を`index <= len(spans)`
    へ壊しても全269件のテストスイートが通ってしまうことを確認した上で
    追加）。"""
        assert restore("See __MATH1__ here.", ["$x$"]) == "See __MATH1__ here."


    def test_restore_replaces_every_occurrence_of_the_same_placeholder_index(self):
        """同一番号のプレースホルダがテキスト中に複数回出現した場合、
    出現箇所全てが対応する数式スパンへ置換されることを確認する
    （出現回数を1回に制限する処理は無く、re.subの通常の挙動どおり全て
    置換される。翻訳エンジンが言い回し上プレースホルダを正当に複数回
    参照するケースを想定した仕様であり、不具合ではない）。"""
        text = "See __MATH0__ and again __MATH0__ here."
        assert restore(text, ["$x$"]) == "See $x$ and again $x$ here."


class TestFilterTranslatableUnits:
    # stage4.translate_units/stage5.apply_restore/stage6の各関数が、
    # 実際にtranslatable=Trueのunitだけを処理していることは各工程別
    # テストファイルで間接的に検証済み（重複を避けここでは省略）。
    # ここではfilter_translatable_units自体の入出力契約（フィルタ条件・
    # 順序保持）のみを直接対象とする。

    def test_filter_translatable_units_returns_empty_for_empty_list(self):
        assert list(filter_translatable_units([])) == []


    def test_filter_translatable_units_filters_by_flag_and_preserves_order(self):
        """translatable=True/Falseが混在する場合、Trueのunitだけを元の
    順序を保ったまま列挙することを確認する。"""
        a = DocUnit(tag="A", kind="body_sentence", page=1, translatable=True)
        b = DocUnit(tag="B", kind="figure_image", page=1, translatable=False)
        c = DocUnit(tag="C", kind="body_sentence", page=1, translatable=True)

        assert list(filter_translatable_units([a, b, c])) == [a, c]
