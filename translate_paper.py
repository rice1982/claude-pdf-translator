"""PDF学術論文の解析からPDF翻訳成果物の生成までを1コマンドで実行する
エントリポイント。

処理の流れ:
    1. PDF解析（:func:`pdf_processor.process_pdf`）
       PDFをMinerUで解析し、文単位ID付きのページ別タグ付きMarkdownと
       画像群を出力する（既存の :mod:`pdf_processor` パイプライン）。
    2. タグ解析（:mod:`md_tag_parser`）
       タグ付きMarkdownを :class:`translation_models.DocUnit` の順序付き
       リストへ変換し、参考文献セクションを翻訳対象から除外する。
    3. 翻訳（:mod:`deepl_translator`）
       DeepL（文脈パラメータ付き）で翻訳する。キー未設定・上限到達・
       通信エラーなどDeepL側の障害を検知した場合はエラーとして終了する。
    4. PDFレンダリング（:mod:`pdf_renderer`）
       対訳版／英語版／日本語版の3種類のPDFを生成する。インライン数式は
       KaTeXにより実際の数式として描画される。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from deepl_translator import TranslationBackendError, translate_with_deepl
from math_protection import normalize as normalize_math
from md_tag_parser import build_document_context, exclude_references_section, parse_output_dir
from pdf_chapter_resolver import ChapterResolutionError, resolve_chapter_page_range
from pdf_page_label_resolver import PageLabelResolutionError, resolve_physical_page_range
from pdf_processor import process_pdf
from pdf_renderer import build_blocks, render_all_pdfs
from translation_models import DocUnit

_MATH_NORMALIZE_KINDS = {"title", "heading", "body_sentence", "caption_sentence"}


def _log(message: str) -> None:
    print(message, flush=True)


def run_translation(units: list[DocUnit], document_context: str) -> None:
    """DeepLで翻訳を行う。

    Raises:
        TranslationBackendError: キー未設定、上限到達、通信エラーなど、
            DeepLでの翻訳継続が不可能な場合。
    """
    api_key = os.environ.get("DEEPL_API_KEY")
    _log("[DeepL] 文脈付き翻訳を開始します...")
    translate_with_deepl(units, api_key, document_context, log=_log)
    _log("[DeepL] 翻訳が完了しました。")


def translate_and_export(output_dir: str | Path) -> list[Path]:
    """既にタグ付きMarkdownが生成済みの ``output_dir`` を対象に、翻訳と
    PDF生成（ステップ2〜4）のみを実行する。

    Args:
        output_dir: page_*_en.md が格納されたディレクトリ。
    """
    output_dir = Path(output_dir)
    units = parse_output_dir(output_dir)
    if not units:
        raise SystemExit(f"{output_dir} に page_*_en.md が見つかりませんでした。")

    for unit in units:
        if unit.kind in _MATH_NORMALIZE_KINDS:
            unit.en_text = normalize_math(unit.en_text)
    exclude_references_section(units)

    document_context = build_document_context(units)
    try:
        run_translation(units, document_context)
    except TranslationBackendError as exc:
        raise SystemExit(f"DeepLでの翻訳に失敗しました: {exc}") from exc

    blocks = build_blocks(units, output_dir)
    pdf_paths = render_all_pdfs(blocks, output_dir, log=_log)

    for path in pdf_paths:
        _log(f"生成完了: {path}")
    return pdf_paths


def run_pipeline(
    pdf_path: str | Path,
    output_dir: str | Path,
    start_page: int | None = None,
    end_page: int | None = None,
) -> list[Path]:
    """PDF解析（ステップ1）から3種PDF生成（ステップ4）までを通しで実行する。

    Args:
        pdf_path: 入力PDFファイルのパス。
        output_dir: 出力先ディレクトリ。
        start_page: 処理対象の開始ページ番号（1始まり）。省略時は先頭ページから。
        end_page: 処理対象の終了ページ番号（1始まり・両端含む）。省略時は末尾ページまで。
    """
    _log(f"[PDF解析] {pdf_path} を解析しています...")
    process_pdf(pdf_path, output_dir, start_page=start_page, end_page=end_page)
    _log("[PDF解析] タグ付きMarkdownの生成が完了しました。")
    return translate_and_export(output_dir)


def resolve_page_range(
    pdf_path: str | Path,
    chapter: str | None,
    start: int | None,
    end: int | None,
    start_label: str | None = None,
    end_label: str | None = None,
) -> tuple[int | None, int | None]:
    """``--chapter``/``--start``/``--end``/``--start-label``/``--end-label``
    引数からページ範囲（物理ページ番号、1始まり）を決定する。

    ``--start``/``--end``（物理ページ番号）または``--start-label``/
    ``--end-label``（印刷ページラベル。"cov", "i"〜"xviii", "36"等）が
    明示的に指定された場合はそちらを優先し、``--chapter``は無視する
    （要件通り）。同じ境界（開始または終了）に対して物理ページ番号と
    印刷ページラベルの両方が指定された場合は、指定が矛盾するためエラーと
    する。``--chapter``のみが指定された場合は、PDFの目次から対応する
    ページ範囲を自動特定する。
    """
    if start is not None and start_label is not None:
        raise ValueError("--startと--start-labelは同時に指定できません。")
    if end is not None and end_label is not None:
        raise ValueError("--endと--end-labelは同時に指定できません。")

    if start_label is not None or end_label is not None:
        resolved_start, resolved_end = resolve_physical_page_range(pdf_path, start_label, end_label)
        if start_label is not None:
            _log(f"[印刷ページラベル] --start-label {start_label!r} を物理ページ {resolved_start} に解決しました。")
        if end_label is not None:
            _log(f"[印刷ページラベル] --end-label {end_label!r} を物理ページ {resolved_end} に解決しました。")
        start, end = resolved_start, resolved_end

    if start is not None or end is not None:
        if chapter is not None:
            _log("※ --chapterとページ範囲指定（--start/--end/--start-label/--end-label）が同時に指定されたため、ページ範囲指定を優先します。")
        return start, end

    if chapter is not None:
        start_page, end_page = resolve_chapter_page_range(pdf_path, chapter)
        _log(f"[章指定] --chapter {chapter} をページ範囲 {start_page}-{end_page} に解決しました。")
        return start_page, end_page

    return None, None


def main() -> None:
    if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="PDF学術論文を解析し、文脈付き日本語訳と対訳版／英語版／日本語版の3種のPDFを出力する。"
    )
    parser.add_argument("pdf_path", help="入力PDFファイルのパス")
    parser.add_argument("output_dir", help="出力先ディレクトリ（page_*_en.md / images/ / 生成PDFを格納する）")
    parser.add_argument(
        "--chapter", default=None,
        help="処理対象の章番号（PDFの目次から自動でページ範囲を特定する。例: --chapter 1,2 / --chapter 1-2）",
    )
    parser.add_argument("--start", type=int, default=None, help="処理対象の開始ページ番号（物理ページ、1始まり）")
    parser.add_argument("--end", type=int, default=None, help="処理対象の終了ページ番号（物理ページ、1始まり・両端含む）")
    parser.add_argument(
        "--start-label", default=None,
        help="処理対象の開始ページを印刷ページラベルで指定（例: --start-label cov / --start-label i / --start-label 36）",
    )
    parser.add_argument(
        "--end-label", default=None,
        help="処理対象の終了ページを印刷ページラベルで指定（両端含む。例: --end-label xviii / --end-label 41）",
    )
    args = parser.parse_args()

    try:
        start_page, end_page = resolve_page_range(
            args.pdf_path, args.chapter, args.start, args.end, args.start_label, args.end_label
        )
    except ChapterResolutionError as exc:
        raise SystemExit(f"章指定の解決に失敗しました: {exc}") from exc
    except PageLabelResolutionError as exc:
        raise SystemExit(f"印刷ページラベルの解決に失敗しました: {exc}") from exc

    run_pipeline(args.pdf_path, args.output_dir, start_page=start_page, end_page=end_page)


if __name__ == "__main__":
    main()
