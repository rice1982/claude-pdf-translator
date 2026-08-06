"""PDF学術論文の前処理・構造化解析パイプライン（オーケストレーター）。

処理を4つの独立したステップに分離し、このモジュールはそれらを順番に
呼び出すだけの薄い層として振る舞う。各ステップは単体でテスト・差し替え
可能で、入出力の型は :mod:`pdf_models` で定義する。

    ステップ1（:mod:`pdf_mineru_runner`）: MinerU実行
        PDFをMinerUに通し、生の構造化JSON（content_list）と画像群を得る。
    ステップ2（:mod:`pdf_structure_analyzer`）: 構造解析
        content_listを、翻訳対象の本文テキストと非翻訳要素（数式・図表等）
        に分離し、本ツール独自のID体系を付与する。未知の要素や解析失敗は
        フォールバックし、ドキュメント全体の処理は止めない。
    ステップ3（:mod:`pdf_translator`）: 翻訳実行
        翻訳対象の本文テキストのみを翻訳処理へ渡す。デフォルトでは
        実際の翻訳バックエンドが未接続のため恒等関数（原文をそのまま返す）
        が使われる。
    ステップ4（:mod:`pdf_document_builder`）: 成果物結合
        翻訳結果と非翻訳要素を再結合し、ページ別Markdownとして出力する。

このモジュール自体は上記4ステップのオーケストレーションと、CLIエントリ
ポイント（``python pdf_processor.py <pdf> <output_dir>``）の提供のみを行う。
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import fitz  # PyMuPDF（総ページ数の取得のみに使用）

from pdf_document_builder import build_document
from pdf_mineru_runner import run_mineru
from pdf_structure_analyzer import analyze_structure
from pdf_text_utils import HEADING_RE, parse_caption_label, slugify_section_name, split_sentences
from pdf_translator import TranslateFunc, identity_translator, translate_units

# --- 後方互換のためのエイリアス -----------------------------------------------
# このモジュールを直接 `pdf_processor.foo` の形で参照している既存コード
# （テスト等）が動き続けるよう、実体を移した関数・正規表現を再公開する。
__all__ = ["process_pdf", "main", "split_sentences", "HEADING_RE"]

_slugify_section_name = slugify_section_name
_parse_caption_label = parse_caption_label


def process_pdf(
    pdf_path: str | Path,
    output_dir: str | Path,
    translate: TranslateFunc = identity_translator,
    start_page: int | None = None,
    end_page: int | None = None,
) -> list[Path]:
    """PDFを解析し、ページ別Markdown（文ID・画像切り出し付き）を出力する。

    内部では以下の4ステップを順に実行する（詳細はモジュール docstring 参照）。
    1. MinerU実行 (:func:`pdf_mineru_runner.run_mineru`)
    2. 構造解析 (:func:`pdf_structure_analyzer.analyze_structure`)
    3. 翻訳実行 (:func:`pdf_translator.translate_units`)
    4. 成果物結合 (:func:`pdf_document_builder.build_document`)

    Args:
        pdf_path: 入力PDFファイルのパス。
        output_dir: 出力先ディレクトリ（``images/`` サブディレクトリに画像を保存する）。
        translate: 本文の1文を翻訳する関数（ステップ3で使用）。省略時は
            原文をそのまま返す恒等関数が使われ、出力は従来通り英語のままになる。
            実際の翻訳バックエンドを使う場合は、この引数に差し替えるだけでよい。
        start_page: 処理対象の開始ページ番号（1始まり）。``None``（省略時）
            はPDFの先頭ページから処理する。
        end_page: 処理対象の終了ページ番号（1始まり・両端含む）。``None``
            （省略時）はPDFの末尾ページまで処理する。

    Returns:
        生成されたMarkdownファイルパスのリスト（ページ順）。

    Raises:
        ValueError: ``start_page``/``end_page``がPDFの実際のページ数に対して
            不正な範囲を指定している場合。
        pdf_mineru_runner.MinerURunError: MinerUの実行に失敗した場合。
            後続のステップに渡す生データが得られないため、この失敗のみは
            フォールバックせず呼び出し元に伝播する。
    """
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)

    with fitz.open(pdf_path) as doc:
        total_pages = doc.page_count

    first_page = start_page if start_page is not None else 1
    last_page = end_page if end_page is not None else total_pages
    if not (1 <= first_page <= last_page <= total_pages):
        raise ValueError(
            f"不正なページ範囲です: start_page={start_page}, end_page={end_page}"
            f"（PDFの総ページ数: {total_pages}）"
        )
    page_range_specified = start_page is not None or end_page is not None

    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp)

        # ステップ1: MinerU実行
        if page_range_specified:
            mineru_output = run_mineru(pdf_path, work_dir, first_page - 1, last_page - 1)
        else:
            mineru_output = run_mineru(pdf_path, work_dir)

        # ステップ2: 構造解析
        page_offset = (first_page - 1) if page_range_specified else 0
        structured_doc = analyze_structure(mineru_output.items, mineru_output.images_base, page_offset)

        # ステップ3: 翻訳実行（本文テキストのみを渡す）
        translations = translate_units(structured_doc.translation_units, translate=translate)

        # ステップ4: 成果物結合
        return build_document(structured_doc, translations, output_dir, first_page, last_page)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PDF学術論文を解析し、文単位ID付きのページ別Markdownを出力する。"
    )
    parser.add_argument("pdf_path", help="入力PDFファイルのパス")
    parser.add_argument("output_dir", help="出力先ディレクトリ（images/ サブディレクトリに画像を保存する）")
    parser.add_argument("--start", type=int, default=None, help="処理対象の開始ページ番号（1始まり）")
    parser.add_argument("--end", type=int, default=None, help="処理対象の終了ページ番号（1始まり・両端含む）")
    args = parser.parse_args()

    md_paths = process_pdf(args.pdf_path, args.output_dir, start_page=args.start, end_page=args.end)
    for path in md_paths:
        print(path)


if __name__ == "__main__":
    main()
