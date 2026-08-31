"""工程(3): 構造化・タグ処理のテスト。

対応関数: parse_output_dir / exclude_references_section /
build_document_context / parse_page_file（タグ付きMarkdown → DocUnit+
文脈への変換）に加え、工程(3)の仕上げ（normalize/protect_units）・
入口（prepare_translation_input）も本ファイルの対象に含む。自作の
タグ付きMarkdown・自作DocUnitのみを使い、MinerU・DeepLのどちらも
呼ばずに検証する（ただし、shared.protect/restoreの実データラウンド
トリップ確認（TestNormalize）のみ例外で、test_stage2.pyと共有する
`processed`fixture経由でMinerUの実行結果を使う）。
"""
from __future__ import annotations

import pytest

from mainCode.shared.shared import DocUnit, protect, restore
from mainCode.stage3.stage3 import (
    build_document_context,
    exclude_references_section,
    normalize,
    parse_output_dir,
    parse_page_file,
    prepare_translation_input,
    protect_units,
)


class TestMdTagParser:
    def test_parse_page_file_classifies_tag_kinds_and_translatability(self, tmp_path):
        page_path = tmp_path / "page_01_en.md"
        page_path.write_text(
            "[P1-TITLE] A Title\n"
            "[P1-AUTHORS] Jane Doe\n"
            "[P1-AFFIL] Example University\n"
            "[P1-HEADING-1.introduction] 1. INTRODUCTION\n"
            "[P1-S1-1.introduction-S1] Body sentence.\n"
            "[P1-FIG1-CAPTION-S1] Fig. 1: Example.\n"
            "![P1-FIG1](images/fig_p1_1.png) [P1-FIG1]\n"
            "[P1-EQ1-LATEX] $$x = 1$$\n",
            encoding="utf-8",
        )
        units = parse_page_file(page_path)
        kinds = {u.tag: u.kind for u in units}
        translatable = {u.tag: u.translatable for u in units}

        assert kinds == {
            "P1-TITLE": "title",
            "P1-AUTHORS": "authors",
            "P1-AFFIL": "affil",
            "P1-HEADING-1.introduction": "heading",
            "P1-S1-1.introduction-S1": "body_sentence",
            "P1-FIG1-CAPTION-S1": "caption_sentence",
            "P1-FIG1": "figure_image",
            "P1-EQ1-LATEX": "equation_latex",
        }
        assert translatable["P1-TITLE"] is True
        assert translatable["P1-FIG1"] is False
        assert translatable["P1-EQ1-LATEX"] is False


    def test_parse_page_file_classifies_unrecognized_tag_format_as_unknown(self, tmp_path):
        """既知のどのタグ形式にも一致しないタグ（_classifyの最終フォール
    バック分岐）が"unknown"として扱われ、翻訳対象外になることを確認する
    （これまで一度もテストされていなかった）。"""
        page_path = tmp_path / "page_01_en.md"
        page_path.write_text("[P1-SOMETHING-WEIRD] Unrecognized tag body.\n", encoding="utf-8")
        units = parse_page_file(page_path)
        assert units[0].kind == "unknown"
        assert units[0].translatable is False
        assert units[0].ja_text == units[0].en_text  # 非翻訳対象unitはja_textもen_textと同一


    def test_parse_page_file_ignores_orphan_continuation_line_at_file_start(self, tmp_path):
        """`[TAG]`で始まらない行が、連結先となる直前unitを持たない場合
    （ファイル先頭に出現した場合）は、従来通り無視されることを確認する
    （continuation連結ロジックの`if units:`がFalseになる分岐。これまで
    一度もテストされていなかった）。"""
        page_path = tmp_path / "page_01_en.md"
        page_path.write_text(
            "orphan continuation line with no preceding tag\n"
            "[P1-TITLE] A Title\n",
            encoding="utf-8",
        )
        units = parse_page_file(page_path)
        assert [u.tag for u in units] == ["P1-TITLE"]
        assert units[0].en_text == "A Title"


    def test_parse_page_file_joins_continuation_line_to_preceding_unit(self, tmp_path):
        """`[TAG]`で始まらない行が、直前unitへ半角スペース区切りで連結
    されること自体を、stage3.py側から直接検証する。

    この振る舞い自体はtest_stage2.py::TestBuildDocument::
    test_build_document_preserves_multiline_text_and_reparses_without_loss
    がbuild_documentとの往復（round-trip）確認の一環として間接的に踏んで
    いるが、CLAUDE.md「テスト・実行運用規定」項目8（検証対象が担う役割で
    テストファイルを分類する）に照らすと、parse_page_file自身が担う
    継続行連結の振る舞いはstage3.py側のtest_stage3.pyが直接持つべきもの。
    mutation testingで継続行連結ロジック自体を丸ごと削除しても
    test_stage3.py単体は1件も落ちず、上記のstage2側のround-tripテストで
    しか検知できないことを確認した上で追加した。

    翻訳対象unit（body_sentence）への連結を確認する（非翻訳対象unitへの
    連結時のja_text同期は既存のオーファン行テストの対極として、こちらは
    ja_textが空文字列のまま変化しないことを確認する）。"""
        page_path = tmp_path / "page_01_en.md"
        page_path.write_text(
            "[P1-S1-body-S1] First physical line,\n"
            "second physical line.\n",
            encoding="utf-8",
        )
        units = parse_page_file(page_path)
        assert len(units) == 1
        assert units[0].en_text == "First physical line, second physical line."
        assert units[0].ja_text == ""  # 翻訳対象unitなのでja_textは同期されず空のまま


    def test_parse_output_dir_combines_pages_in_page_number_order(self, tmp_path):
        (tmp_path / "page_02_en.md").write_text("[P2-S1-body-S1] Second page.\n", encoding="utf-8")
        (tmp_path / "page_01_en.md").write_text("[P1-S1-body-S1] First page.\n", encoding="utf-8")
        units = parse_output_dir(tmp_path)
        assert [u.tag for u in units] == ["P1-S1-body-S1", "P2-S1-body-S1"]


    def test_exclude_references_section_marks_units_non_translatable(self):
        units = [
            DocUnit(tag="P5-HEADING-5.references", kind="heading", page=5, en_text="References", translatable=True),
            DocUnit(
                tag="P5-S1-5.references-S1",
                kind="body_sentence",
                page=5,
                en_text="A. Author, Title, 2020.",
                translatable=True,
            ),
            DocUnit(
                tag="P1-S1-1.introduction-S1",
                kind="body_sentence",
                page=1,
                en_text="Intro sentence.",
                translatable=True,
            ),
        ]
        exclude_references_section(units)

        assert units[0].translatable is False and units[0].ja_text == "References"
        assert units[1].translatable is False and units[1].ja_text == "A. Author, Title, 2020."
        assert units[2].translatable is True


    def test_exclude_references_section_is_case_insensitive_and_requires_exact_word(self):
        """大文字小文字を区別せず"references"に一致する一方、"reference"
    （単数形）のような部分一致では除外されないことを確認する
    （_REFERENCES_SLUG_REの境界。これまで一度もテストされていなかった）。"""
        units = [
            DocUnit(tag="P5-HEADING-5.References", kind="heading", page=5, en_text="References", translatable=True),
            DocUnit(
                tag="P6-HEADING-6.reference", kind="heading", page=6,
                en_text="Reference Architecture (not the references section)", translatable=True,
            ),
        ]
        exclude_references_section(units)
        assert units[0].translatable is False  # 大文字小文字を区別しない
        assert units[1].translatable is True  # "reference"（単数形）は誤って一致しない


    def test_build_document_context_uses_title_and_abstract_sentences(self):
        units = [
            DocUnit(tag="P1-TITLE", kind="title", page=1, en_text="A Great Paper"),
            DocUnit(tag="P1-AUTHORS", kind="authors", page=1, en_text="Jane Doe"),
            DocUnit(tag="P1-S1-abstract-S1", kind="body_sentence", page=1, en_text="We propose a method."),
            DocUnit(tag="P1-S2-abstract-S2", kind="body_sentence", page=1, en_text="It works well."),
            DocUnit(tag="P1-S1-1.introduction-S1", kind="body_sentence", page=1, en_text="Intro text."),
        ]
        assert build_document_context(units) == "A Great Paper\nWe propose a method. It works well."


    def test_build_document_context_falls_back_when_title_or_abstract_missing(self):
        """タイトル・Abstractのどちらか（または両方）が存在しない場合、
    存在する側だけを使って組み立てられ、両方無ければ空文字列になる
    ことを確認する（これまで片方でも欠けたケースは一度もテストされて
    いなかった）。"""
        title_only = [DocUnit(tag="P1-TITLE", kind="title", page=1, en_text="Only Title")]
        assert build_document_context(title_only) == "Only Title"

        abstract_only = [
            DocUnit(tag="P1-S1-abstract-S1", kind="body_sentence", page=1, en_text="Only abstract sentence."),
        ]
        assert build_document_context(abstract_only) == "Only abstract sentence."

        neither = [
            DocUnit(tag="P1-S1-1.introduction-S1", kind="body_sentence", page=1, en_text="Not abstract, not title."),
        ]
        assert build_document_context(neither) == ""


    def test_build_document_context_does_not_match_section_slug_prefixed_with_abstract(self):
        """セクションスラッグが"abstract"で始まるだけの別セクション（例:
    "Abstractive Summarization"というセクション名がslugify_section_name
    により"abstractive"になったもの）が、実際のAbstractセクションと
    誤って混同されないことを確認する。

    build_document_contextの判定条件は`"-abstract-" in u.tag`（ハイフンで
    区切られた完全一致）であり、単なる`"abstract" in u.tag`（部分一致）
    ではない。mutation testingで後者へ弱めても、既存テスト・フルスイート
    284件のいずれも1件も失敗しないことを確認した上で追加した（"abstractive"
    のような"abstract"を接頭辞に持つ実在しうるセクション名を区別する
    唯一のテストが、これまで一度も無かった）。"""
        units = [
            DocUnit(tag="P1-S1-abstract-S1", kind="body_sentence", page=1, en_text="Real abstract sentence."),
            DocUnit(
                tag="P2-S1-2.abstractive-S1", kind="body_sentence", page=2,
                en_text="A sentence from the Abstractive Summarization section.",
            ),
        ]
        assert build_document_context(units) == "Real abstract sentence."


class TestNormalize:
    def test_normalize_converts_textless_textgreater_only_inside_math_spans(self):
        """MinerUが数式中に出力する\\textless/\\textgreaterが$...$の
    内側でだけ</>へ正規化され、数式スパンの外側（地の文）にある
    同じ文字列は変更されないことを確認する（normalize自体は、これまで
    一度も直接テストされていなかった。protect内部のヘルパー
    _normalize_math_escapeの適用範囲がこのnormalize関数の対象範囲と
    一致することの裏取り）。"""
        text = r"We have $a \textless b \textgreater c$ and plain \textless text."
        assert normalize(text) == r"We have $a < b > c$ and plain \textless text."


    def test_math_protection_round_trips_all_real_inline_math(self, processed):
        """page_02_en.md（sample0.pdf）に実際に含まれる全てのインライン数式・
    ディスプレイ数式スパンが、shared.protect/restoreのラウンドトリップで
    （\\textless/\\textgreaterの正規化を除き）壊れず復元されるか。自作
    データではなく実データの数式スパン全数を対象にする点が、他のprotect/
    restoreテストと異なる。

    shared.protect/restore自体の役割上の呼び出し元はstage2ではなくstage3
    （本ファイル）である（このnormalize関数自体が、まさに同じprotect→
    restoreのラウンドトリップを実装として使っている）。CLAUDE.md
    「テスト・実行運用規定」項目8の方針（ソースの置き場所ではなく役割で
    分類する）に従いここに置く。

    このテストはtest_stage2.pyと同じ`processed`fixture（sample0.pdfを
    実際にMinerUで処理する、モジュール単位で使い回すfixture）に依存する
    ため、本ファイル用にMinerU処理がもう1回走る。MinerU自身のディスク
    キャッシュ（cache/、mainCode.stage2側の機構）が温まっていればほぼ
    無視できるコストだが、キャッシュが無い状態（初回実行・キャッシュ
    削除後・MinerUバージョン更新直後）では追加で数分かかりうる。
    """
        text = processed["texts"]["page_02_en.md"]

        protected, spans = protect(text)
        # page_02_en.mdに実際に含まれる数式スパン数（インライン24+ディスプレイ3）。
        # MinerUの数式検出結果や自動保護ロジックが変われば変化しうる値のため、
        # 変化した場合は数式検出の挙動が変わっていないか確認すること。
        assert len(spans) == 27
        # プレースホルダ置換後のテキストに数式デリミタ$が一切残っていないこと
        assert "$" not in protected

        restored = restore(protected, spans)
        expected = text.replace("\\textless", "<").replace("\\textgreater", ">")
        assert restored == expected


class TestProtectUnits:
    def test_protect_units_sets_protected_text_only_for_translatable_units(self):
        """翻訳対象（translatable=True）のunitだけprotected_en_text/
    math_spansが設定され、非翻訳対象unitはデフォルト値（空文字列/空
    リスト）のまま変更されないことを確認する（protect_units自体は、
    これまで一度も直接テストされていなかった。call_deepl等のセット
    アップ用ヘルパーとしてのみ間接的に使われていた）。"""
        units = [
            DocUnit(tag="P1-S1-body-S1", kind="body_sentence", page=1, en_text="Use $x$ here.", translatable=True),
            DocUnit(tag="P1-FIG1", kind="figure_image", page=1, en_text="", translatable=False),
        ]
        protect_units(units)

        assert units[0].protected_en_text == "Use __MATH0__ here."
        assert units[0].math_spans == ["$x$"]
        assert units[1].protected_en_text == ""  # DocUnitの既定値のまま
        assert units[1].math_spans == []


class TestPrepareTranslationInput:
    def test_prepare_translation_input_raises_when_no_markdown_files(self, tmp_path):
        """output_dir配下にpage_*_en.mdが1つも無い場合、分かりやすい
    SystemExitで終了することを確認する（これまで一度もテストされて
    いなかった）。"""
        with pytest.raises(SystemExit, match="page_\\*_en.md"):
            prepare_translation_input(tmp_path)


    def test_prepare_translation_input_runs_full_pipeline_and_normalizes_only_specific_kinds(self, tmp_path):
        """prepare_translation_inputが、parse_output_dir→数式正規化
    （_TRANSLATABLE_KINDSに該当するkindのみ）→exclude_references_
    section→build_document_context→protect_unitsをこの順で実行する
    工程(3)全体の統合的な振る舞いを、実PDF・MinerU・DeepLを一切経由
    せず自作のタグ付きMarkdownだけで検証する（これまでこの関数自体を
    直接呼ぶ単体テストが無く、実PDFを処理する統合テスト経由でしか
    間接的に確認されていなかった）。

    _TRANSLATABLE_KINDS = {"title","heading","body_sentence",
    "caption_sentence"}には"authors"が含まれないため、AUTHORSタグの
    本文中の\\textlessは正規化されず、TITLEタグの方だけ正規化される
    ことも確認する。
    """
        (tmp_path / "page_01_en.md").write_text(
            "[P1-TITLE] Title with $a \\textless b$ math.\n"
            "[P1-AUTHORS] Jane $x \\textless y$ Doe.\n"
            "[P1-HEADING-5.references] References\n"
            "[P1-S1-5.references-S1] A. Author, Title, 2020.\n",
            encoding="utf-8",
        )

        units, document_context = prepare_translation_input(tmp_path)
        by_tag = {u.tag: u for u in units}

        # TITLE（_TRANSLATABLE_KINDSに含まれる）は正規化される
        assert by_tag["P1-TITLE"].en_text == "Title with $a < b$ math."
        # AUTHORS（_TRANSLATABLE_KINDSに含まれない）は正規化されない
        assert by_tag["P1-AUTHORS"].en_text == "Jane $x \\textless y$ Doe."

        # exclude_references_sectionにより参考文献セクションが翻訳対象外になる
        assert by_tag["P1-HEADING-5.references"].translatable is False
        assert by_tag["P1-S1-5.references-S1"].translatable is False

        # build_document_contextの戻り値（正規化後のTITLEが使われる）
        assert document_context == "Title with $a < b$ math."

        # protect_unitsにより翻訳対象unitのprotected_en_text/math_spansが設定される
        assert by_tag["P1-TITLE"].protected_en_text == "Title with __MATH0__ math."
        assert by_tag["P1-TITLE"].math_spans == ["$a < b$"]
        # 翻訳対象外になったunitはprotect_unitsの対象外（既定値のまま）
        assert by_tag["P1-HEADING-5.references"].protected_en_text == ""
