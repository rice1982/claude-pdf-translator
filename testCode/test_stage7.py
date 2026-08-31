"""工程(7): PDF生成のテスト。

対応関数: stage7.build_blocks / stage7.render_all_pdfs単体。モック翻訳済み
の自作DocUnitを使えば独立テスト可能（MinerU・DeepLいずれも経由しない）。
render_all_pdfsはPlaywrightでローカルにPDF化するだけで外部APIも課金も
発生しない。

stage7.render_units_to_pdfs（工程7の統括関数。build_blocksとrender_all_
pdfsをこの順でまとめて実行するだけの委譲）自体には専用テストを設けない。
間に副作用・順序依存・分岐を一切挟まない純粋な2行の委譲のため、
build_blocks/render_all_pdfsをそれぞれ単体テストすれば、まとめて呼んだ
場合の正しさもそれだけで保証される。
"""
from __future__ import annotations

import json
from pathlib import Path

import fitz  # PyMuPDF
import pytest

from mainCode.stage7.stage7 import (
    _heading_level,
    _render_bilingual_block,
    _render_mono_block,
    build_blocks,
    load_katex_assets,
    render_all_pdfs,
    render_bilingual_html,
    render_mono_html,
)
from mainCode.shared.shared import DocUnit

from conftest import _find_latest_real_deepl_cache


class TestBuildBlocks:
    def test_build_blocks_groups_consecutive_body_sentences_into_one_paragraph(self):
        units = [
            DocUnit(tag="P1-TITLE", kind="title", page=1, en_text="A Title", ja_text="タイトル"),
            DocUnit(tag="P1-HEADING-1.introduction", kind="heading", page=1, en_text="1. INTRODUCTION", ja_text="1. はじめに"),
            DocUnit(tag="P1-S1-1.introduction-S1", kind="body_sentence", page=1, en_text="First.", ja_text="一つ目。"),
            DocUnit(tag="P1-S2-1.introduction-S2", kind="body_sentence", page=1, en_text="Second.", ja_text="二つ目。"),
            DocUnit(tag="P1-HEADING-2.method", kind="heading", page=1, en_text="2. METHOD", ja_text="2. 手法"),
            DocUnit(tag="P1-S1-2.method-S1", kind="body_sentence", page=1, en_text="Third.", ja_text="三つ目。"),
        ]
        blocks = build_blocks(units, output_dir=Path("."))

        assert [b.kind for b in blocks] == ["title", "heading", "paragraph", "heading", "paragraph"]
        assert len(blocks[2].sentences) == 2  # 同じ章内の2文が1つのparagraphブロックにまとまる
        assert len(blocks[4].sentences) == 1


    def test_build_blocks_assigns_heading_level_from_section_number_depth(self):
        units = [
            DocUnit(tag="P1-HEADING-1.introduction", kind="heading", page=1, en_text="1. INTRO", ja_text="1. はじめに"),
            DocUnit(tag="P1-HEADING-2.1.method", kind="heading", page=1, en_text="2.1 METHOD", ja_text="2.1 手法"),
        ]
        blocks = build_blocks(units, output_dir=Path("."))
        assert [b.level for b in blocks] == [2, 3]


    def test_build_blocks_skips_equation_image_and_keeps_equation_latex_block(self):
        """equation_imageは対応するequation_latexブロックがKaTeXで描画する
    ため、build_blocksの時点で出力から除かれる。"""
        units = [
            DocUnit(tag="P1-EQ1", kind="equation_image", page=1, image_rel_path="images/eq_p1_1.png"),
            DocUnit(tag="P1-EQ1-LATEX", kind="equation_latex", page=1, en_text="x = 1", ja_text="x = 1"),
        ]
        blocks = build_blocks(units, output_dir=Path("."))
        assert [b.kind for b in blocks] == ["equation"]


    def test_build_blocks_caps_heading_level_at_four_for_deeply_nested_sections(self):
        """章番号の数字部分の深さ(numeric_depth)が3以上あっても、見出し
    レベルは4でキャップされることを確認する（_heading_levelのmin(...,4)
    分岐。これまで深さ2まで（レベル2・3）しかテストされておらず、
    キャップ自体は一度も踏まれていなかった）。"""
        assert _heading_level("P1-HEADING-2.1.3.sub") == 4
        assert _heading_level("P1-HEADING-2.1.3.4.deep") == 4  # さらに深くても4のまま
        assert _heading_level("P1-HEADING-not-a-number") == 2  # 数字部分が無ければ既定の2


    def test_build_blocks_groups_consecutive_caption_sentences_into_one_paragraph(self):
        """本文文（body_sentence）だけでなく、キャプション文
    （caption_sentence）も同じ_paragraph_keyの仕組みで複数文が1つの
    paragraphブロックへまとめられることを確認する（これまでbuild_blocksの
    単体テストで一度もcaption_sentence種別が使われていなかった）。"""
        units = [
            DocUnit(
                tag="P2-FIG1-CAPTION-S1", kind="caption_sentence", page=2,
                en_text="Fig. 1: Overview.", ja_text="図1: 概要。",
            ),
            DocUnit(
                tag="P2-FIG1-CAPTION-S2", kind="caption_sentence", page=2,
                en_text="Second caption sentence.", ja_text="二文目。",
            ),
        ]
        blocks = build_blocks(units, output_dir=Path("."))
        assert [b.kind for b in blocks] == ["paragraph"]
        assert len(blocks[0].sentences) == 2


    def test_build_blocks_figure_image_without_rel_path_has_no_data_uri(self):
        """image_rel_pathがNoneのfigure_image（何らかの理由で画像切り出し
    自体に失敗した場合を想定）は、ファイル読み込みを試みずに
    image_data_uri=Noneのfigureブロックになることを確認する（これまで
    build_blocksの単体テストで一度もfigure_image種別が使われておらず、
    完全に未検証だった）。"""
        units = [DocUnit(tag="P2-FIG1", kind="figure_image", page=2, image_rel_path=None)]
        blocks = build_blocks(units, output_dir=Path("."))
        assert [b.kind for b in blocks] == ["figure"]
        assert blocks[0].image_data_uri is None


class TestRenderBlockEscaping:
    def test_meta_block_html_escapes_author_affiliation_text(self):
        """metaブロック（著者・所属欄）が、他の全ブロック種別（title/
    heading/paragraph/equation）と同様にHTMLエスケープされることを
    確認する回帰テスト。修正前は_render_bilingual_block/_render_mono_
    blockのmeta分岐だけ_esc()の呼び出しが漏れており、著者名・所属に
    "<"/">"/"&"を含む場合（MinerUのOCR誤認識等）にHTMLとして誤って
    解釈されてしまうバグがあった。"""
        units = [
            DocUnit(tag="P1-AUTHORS", kind="authors", page=1, en_text="Jane <Doe> & Co.", ja_text="Jane <Doe> & Co."),
        ]
        block = build_blocks(units, output_dir=Path("."))[0]

        assert "&lt;Doe&gt;" in _render_bilingual_block(block)
        assert "<Doe>" not in _render_bilingual_block(block)
        assert "&lt;Doe&gt;" in _render_mono_block(block, "en")
        assert "<Doe>" not in _render_mono_block(block, "en")


    @pytest.mark.parametrize(
        "unit",
        [
            DocUnit(tag="P1-TITLE", kind="title", page=1, en_text="A <Title> & Co.", ja_text="A <Title> & Co."),
            DocUnit(
                tag="P1-HEADING-1.introduction", kind="heading", page=1,
                en_text="1. <Introduction> & Overview", ja_text="1. <Introduction> & Overview",
            ),
            DocUnit(
                tag="P1-S1-1.introduction-S1", kind="body_sentence", page=1,
                en_text="A <tag> & body sentence.", ja_text="A <tag> & body sentence.",
            ),
            DocUnit(
                tag="P1-EQ1-LATEX", kind="equation_latex", page=1,
                en_text="a < b & c", ja_text="a < b & c",
            ),
        ],
        ids=["title", "heading", "paragraph", "equation"],
    )
    def test_non_meta_blocks_also_html_escape_their_text(self, unit):
        """metaブロックのエスケープ漏れ（上のテスト）は修正済みだが、その
    修正が「metaだけ後から辻褄合わせした」ものになっていないか、つまり
    残る4種類（title/heading/paragraph/equation）についても実際に
    _esc()が呼ばれていることを直接確認する。

    mutation testingで、titleブロックの_render_bilingual_block分岐から
    _esc()呼び出しを取り除いても、test_stage7.py単体はもちろん全体
    スイート279件のうち1件も失敗しないことを確認した（meta以外の
    ブロック種別のエスケープを直接検証するテストがこれまで1つも
    無かったため）。"""
        block = build_blocks([unit], output_dir=Path("."))[0]

        assert "&lt;" in _render_bilingual_block(block)
        assert unit.en_text not in _render_bilingual_block(block)
        assert "&lt;" in _render_mono_block(block, "en")
        assert unit.en_text not in _render_mono_block(block, "en")


class TestRenderMonoHtml:
    def test_render_mono_html_uses_matching_document_title_for_each_language(self):
        """render_mono_htmlが組み立てるHTMLの<title>タグ（PDFビューア上の
        タブ・タイトルバーに表示される文書メタタイトル）が、英語版では
        "英語版"、日本語版では"日本語版"と、実際に渡したlangと正しく
        対応していることを確認する。

        render_mono_html自体はこれまでtest_stage7.pyで一度も直接import・
        テストされておらず、render_all_pdfs経由の統合テスト（PDFが空で
        ないことのみ確認）でしか間接的に触れられていなかった。
        `title = "英語版" if lang == "en" else "日本語版"`を
        `title = "日本語版"`に固定しても、既存テスト・フルスイート287件の
        いずれも1件も失敗しないことをmutation testingで確認した上で
        追加した（英語版PDFのタブ表示が実際には"日本語版"になってしまう、
        という利用者に見える形の不具合を検知できていなかった）。"""
        units = [DocUnit(tag="P1-TITLE", kind="title", page=1, en_text="A Title", ja_text="タイトル")]
        blocks = build_blocks(units, output_dir=Path("."))

        assert "<title>英語版</title>" in render_mono_html(blocks, "en")
        assert "<title>日本語版</title>" in render_mono_html(blocks, "ja")


class TestLoadKatexAssets:
    def test_load_katex_assets_inlines_fonts_as_base64_data_uris(self):
        """load_katex_assetsが、vendor/katex/のCSS中の@font-face
    参照（url(fonts/*.woff2)）を全てbase64のdata URIへ置き換え、
    生のフォントパス参照を1つも残さないことを確認する（オフラインでの
    PDF生成が失敗しないための前提。これまで一度もテストされていな
    かった）。"""
        css, katex_js, autorender_js = load_katex_assets()

        assert "data:font/woff2;base64," in css
        assert "url(fonts/" not in css  # 生のフォントパス参照が残っていない
        assert katex_js.strip() != ""
        assert autorender_js.strip() != ""


class TestRenderAllPdfs:
    def test_render_all_pdfs_produces_three_nonempty_pdfs_with_japanese_text(self, tmp_path):
        units = [
            DocUnit(tag="P1-TITLE", kind="title", page=1, en_text="A Title", ja_text="タイトル"),
            DocUnit(tag="P1-S1-body-S1", kind="body_sentence", page=1, en_text="Hello.", ja_text="こんにちは。"),
        ]
        blocks = build_blocks(units, output_dir=tmp_path)

        pdf_paths = render_all_pdfs(blocks, tmp_path, log=lambda _msg: None)

        assert [p.name for p in pdf_paths] == ["paper_bilingual.pdf", "paper_en.pdf", "paper_ja.pdf"]
        for path in pdf_paths:
            assert path.exists()
            assert path.stat().st_size > 0

        ja_pdf_path = next(p for p in pdf_paths if p.name == "paper_ja.pdf")
        with fitz.open(ja_pdf_path) as doc:
            ja_text = "".join(page.get_text() for page in doc)
        assert "こんにちは" in ja_text


    @pytest.mark.parametrize("run_id", ["sample0_full", "sample1_full", "sample2_full", "sample3_label55-60"])
    def test_render_all_pdfs_on_real_translated_sample(self, tmp_path, run_id):
        """cache/<sample>/real_deepl_output/05_restored/units_raw.jsonに
    凍結された、実際にDeepLで翻訳済みのDocUnitをbuild_blocks/render_all_pdfs
    に通す実データ・スモークテスト。図表画像自体（image_rel_path先の
    PNGファイル）はcache/配下に永続化されていないため、figure_image種別の
    unitは対象から除外し、テキスト系要素（タイトル・見出し・本文・数式
    LaTeX等）のレンダリングのみを対象にする。CLAUDE.mdの規定により実DeepL
    はsample0/sample1限定のため、現時点ではsample2/sample3はキャッシュが
    無くskipされる（サンプルを選べるようにするためのparametrize）。同じ
    run_idで複数回実行されたキャッシュがある場合は最も新しいものを使う
    （`_find_latest_real_deepl_cache`参照）。
    """
        cache_root = _find_latest_real_deepl_cache(run_id)
        units_raw_path = cache_root / "05_restored" / "units_raw.json" if cache_root else None
        if units_raw_path is None or not units_raw_path.exists():
            pytest.skip(
                f"{run_id} の実DeepLキャッシュが無いためスキップ"
                "（該当サンプルでtest_page_ja_md_and_candidate_detection_against_real_deeplを"
                "一度実行すると生成される）"
            )

        all_units = [DocUnit(**d) for d in json.loads(units_raw_path.read_text(encoding="utf-8"))]
        units = [u for u in all_units if u.kind != "figure_image"]

        blocks = build_blocks(units, output_dir=tmp_path)
        assert blocks, f"{run_id} からブロックが1つも組み立てられなかった"

        pdf_paths = render_all_pdfs(blocks, tmp_path, log=lambda _msg: None)
        assert [p.name for p in pdf_paths] == ["paper_bilingual.pdf", "paper_en.pdf", "paper_ja.pdf"]
        for path in pdf_paths:
            assert path.exists()
            assert path.stat().st_size > 0
