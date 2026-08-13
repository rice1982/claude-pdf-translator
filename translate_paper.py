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
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from deepl_translator import TranslationBackendError, apply_restore, build_deepl_requests, call_deepl
from math_protection import (
    check_unprotected_math_survival,
    protect_confirmed_single_letter_leaks,
    report_untranslated_fragment_candidates,
)
from math_protection import normalize as normalize_math
from md_tag_parser import build_document_context, exclude_references_section, parse_output_dir, write_translated_pages
from pdf_chapter_resolver import ChapterResolutionError, resolve_chapter_page_range
from pdf_page_label_resolver import PageLabelResolutionError, resolve_physical_page_range
from pdf_processor import process_pdf
from pdf_renderer import build_blocks, render_all_pdfs
from translation_models import DocUnit

_MATH_NORMALIZE_KINDS = {"title", "heading", "body_sentence", "caption_sentence"}


def _log(message: str) -> None:
    print(message, flush=True)


def run_translation(units: list[DocUnit], document_context: str, snapshot_dir: Path | None = None) -> None:
    """DeepLで翻訳を行う。

    Args:
        units: 翻訳対象を含むDocUnitの列。
        document_context: DeepLへ渡す文書全体の文脈。
        snapshot_dir: 指定された場合、DeepLとの実際の送受信内容を
            04_deepl_input/deepl_input.json・04_deepl_output/
            raw_deepl_results.json・05_postprocess/（units_raw.json、
            および同じ状態のpage_XX_en.md/page_XX_ja.md）として
            このディレクトリ配下に保存する（実DeepLの実行結果を凍結し、
            人間による目視確認やオフライン回帰テストの入力に使うため。
            translate_and_export参照）。Noneの場合は保存しない。

    Raises:
        TranslationBackendError: キー未設定、上限到達、通信エラーなど、
            DeepLでの翻訳継続が不可能な場合。
    """
    api_key = os.environ.get("DEEPL_API_KEY")
    _log("[DeepL] 文脈付き翻訳を開始します...")

    if snapshot_dir is not None:
        deepl_input_dir = snapshot_dir / "04_deepl_input"
        deepl_input_dir.mkdir(parents=True, exist_ok=True)
        requests = build_deepl_requests(units, document_context)
        (deepl_input_dir / "deepl_input.json").write_text(
            json.dumps({tag: asdict(req) for tag, req in requests.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    raw_results = call_deepl(units, api_key, document_context, log=_log)

    if snapshot_dir is not None:
        deepl_output_dir = snapshot_dir / "04_deepl_output"
        deepl_output_dir.mkdir(parents=True, exist_ok=True)
        (deepl_output_dir / "raw_deepl_results.json").write_text(
            json.dumps({tag: asdict(raw) for tag, raw in raw_results.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    apply_restore(units, raw_results)

    if snapshot_dir is not None:
        # protect_confirmed_single_letter_leaks適用前（restore直後）の
        # DocUnitスナップショット。オフライン回帰テストが、この後に続く
        # 後処理関数（apply_restore自体・check_unprotected_math_survival等）
        # を独立して検証するための入力になる。
        postprocess_dir = snapshot_dir / "05_postprocess"
        postprocess_dir.mkdir(parents=True, exist_ok=True)
        (postprocess_dir / "units_raw.json").write_text(
            json.dumps([asdict(u) for u in units], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # units_raw.jsonと同じ状態（protect_confirmed_single_letter_leaks
        # 適用前）のpage_XX_en.md/page_XX_ja.mdも、人間が目視確認しやすい
        # 形式として書き出す。output_dir側の同名ファイルはこの後
        # write_translated_pagesにより保護後の内容で上書きされるため、
        # 保護前の状態を確認したい場合はこちらを参照する。
        write_translated_pages(units, postprocess_dir)

    _log("[DeepL] 翻訳が完了しました。")


def translate_and_export(
    output_dir: str | Path,
    pdf_path: str | Path | None = None,
    range_label: str | None = None,
) -> list[Path]:
    """既にタグ付きMarkdownが生成済みの ``output_dir`` を対象に、翻訳と
    PDF生成（ステップ2〜4）のみを実行する。

    Args:
        output_dir: page_*_en.md が格納されたディレクトリ。
        pdf_path: 対象の入力PDFファイルのパス。``range_label``と両方指定
            された場合のみ、DeepLとの実際の送受信内容を
            cache/<pdf_pathのstem>_<range_label>/real_deepl_output_
            <実行日時>/へスナップショットとして保存する（mineru_cache.py
            が使うキャッシュフォルダと同じ親ディレクトリ。詳細は
            run_translationのsnapshot_dir引数を参照）。省略時（``None``。
            タグ付きMarkdownからの再開等、対象PDFが不明な場合）は保存
            しない。
        range_label: pdf_pathと組み合わせてスナップショット保存先の
            フォルダ名を決める、人間可読な範囲記述子（describe_page_range
            参照）。
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

    snapshot_dir = None
    if pdf_path is not None and range_label is not None:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        snapshot_dir = (
            Path(__file__).resolve().parent
            / "cache"
            / f"{Path(pdf_path).stem}_{range_label}"
            / f"real_deepl_output_{timestamp}"
        )
        structured_dir = snapshot_dir / "03_structured"
        structured_dir.mkdir(parents=True, exist_ok=True)
        (structured_dir / "document_context.txt").write_text(document_context, encoding="utf-8")
        for md_path in sorted(output_dir.glob("page_*_en.md")):
            (structured_dir / md_path.name).write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")

    try:
        run_translation(units, document_context, snapshot_dir=snapshot_dir)
    except TranslationBackendError as exc:
        raise SystemExit(f"DeepLでの翻訳に失敗しました: {exc}") from exc

    warnings = check_unprotected_math_survival(units, log=_log)
    if warnings:
        _log(f"[数式チェック] 未保護の数式らしき文字列に関する警告が{len(warnings)}件あります（上記参照）。")

    protected_letters = protect_confirmed_single_letter_leaks(units, log=_log)
    if protected_letters:
        _log(f"[数式チェック] 単体アルファベットの数式変数を{len(protected_letters)}件、自動的に$...$で保護しました（上記参照）。")

    candidates = report_untranslated_fragment_candidates(units, log=_log)
    if candidates:
        _log(f"[数式チェック] 未検出の数式らしき候補が{len(candidates)}件あります（誤検知を含む可能性があります。上記参照）。")

    write_translated_pages(units, output_dir)

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
    range_label: str | None = None,
    save_deepl_snapshot: bool = True,
) -> list[Path]:
    """PDF解析（ステップ1）から3種PDF生成（ステップ4）までを通しで実行する。

    Args:
        pdf_path: 入力PDFファイルのパス。
        output_dir: 出力先ディレクトリ。
        start_page: 処理対象の開始ページ番号（1始まり）。省略時は先頭ページから。
        end_page: 処理対象の終了ページ番号（1始まり・両端含む）。省略時は末尾ページまで。
        range_label: cache/配下のフォルダ名を人間可読にするための任意の
            範囲記述子（:func:`describe_page_range` 参照）。省略時は
            :mod:`mineru_cache` の従来の命名を使う。キャッシュの正当性判定
            には影響しない（process_pdf経由でmineru_cacheのフォルダ名にのみ
            使われる）。
        save_deepl_snapshot: Trueの場合（デフォルト）、実際のDeepL送受信
            内容をtranslate_and_export経由でcache/<pdf_pathのstem>_
            <range_label>/real_deepl_output_<実行日時>/へ保存する
            （range_labelがNoneの場合は保存しない）。DeepLをモックして
            呼び出す場合（call_deeplを差し替えるテスト等）は、モックの
            訳文が実DeepLの凍結データとして誤って混入するのを防ぐため、
            必ずFalseを指定すること。
    """
    _log(f"[PDF解析] {pdf_path} を解析しています...")
    process_pdf(pdf_path, output_dir, start_page=start_page, end_page=end_page, range_label=range_label)
    _log("[PDF解析] タグ付きMarkdownの生成が完了しました。")
    snapshot_pdf_path = pdf_path if save_deepl_snapshot else None
    snapshot_range_label = range_label if save_deepl_snapshot else None
    return translate_and_export(output_dir, pdf_path=snapshot_pdf_path, range_label=snapshot_range_label)


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


def describe_page_range(
    chapter: str | None,
    start: int | None,
    end: int | None,
    start_label: str | None = None,
    end_label: str | None = None,
) -> str:
    """``--chapter``/``--start``/``--end``/``--start-label``/``--end-label``
    引数（:func:`resolve_page_range` に渡すのと同じ、解決前のCLI引数）から、
    ``cache/``配下のフォルダ名に使う人間可読な範囲記述子を組み立てる。

    ``resolve_page_range``とは責務を分離している（あちらは物理ページ番号への
    "解決"、こちらは表示用の"命名"のみを行う）。優先順位は
    ``resolve_page_range``と揃えてある（印刷ページラベル・物理ページ番号 >
    章指定 > 指定なし＝全体）。

    Returns:
        例:"full"（範囲指定なし）、"chapter1-2"、"p66-71"、"label55-60"。
    """
    if start_label is not None or end_label is not None:
        parts = [str(v) for v in (start_label, end_label) if v is not None]
        return "label" + "-".join(parts)
    if start is not None or end is not None:
        parts = [str(v) for v in (start, end) if v is not None]
        return "p" + "-".join(parts)
    if chapter is not None:
        return "chapter" + chapter.replace(",", "_")
    return "full"


def _require_pdf_exists(pdf_path: str | Path) -> None:
    """入力PDFファイルが実在するかを確認する。

    resolve_page_range/run_pipeline内部（fitzでのPDF読み込み等）は
    存在しないパスに対して生の例外（FileNotFoundError等）を送出するが、
    他の入力エラー（章指定・印刷ページラベルの解決失敗等）と同様に、
    分かりやすい日本語メッセージでSystemExitとして終了させるための
    事前チェック。

    Raises:
        SystemExit: pdf_pathが存在しない、またはファイルでない場合。
    """
    if not Path(pdf_path).is_file():
        raise SystemExit(f"入力PDFファイルが見つかりません: {pdf_path}")


def default_output_dir(pdf_path: str | Path, range_label: str, timestamp: str | None = None) -> Path:
    """``output_dir``省略時に使う自動生成パスを組み立てる。

    testExplain.txtで定義している人間による手動実行（「本実行」）の
    命名規則`output/manual_{PDF名}_{範囲記述子}_{実行日時}`と同じ形式。
    タイムスタンプは呼び出し時刻（省略時）で、テストからは固定値を注入
    できるようにしてある。
    """
    if timestamp is None:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path("output") / f"manual_{Path(pdf_path).stem}_{range_label}_{timestamp}"


def main() -> None:
    if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="PDF学術論文を解析し、文脈付き日本語訳と対訳版／英語版／日本語版の3種のPDFを出力する。"
    )
    parser.add_argument("pdf_path", help="入力PDFファイルのパス")
    parser.add_argument(
        "output_dir", nargs="?", default=None,
        help="出力先ディレクトリ（page_*_en.md / images/ / 生成PDFを格納する）。"
        "省略時はoutput/manual_{PDF名}_{範囲記述子}_{実行日時}を自動生成する。",
    )
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
    _require_pdf_exists(args.pdf_path)

    try:
        start_page, end_page = resolve_page_range(
            args.pdf_path, args.chapter, args.start, args.end, args.start_label, args.end_label
        )
    except ChapterResolutionError as exc:
        raise SystemExit(f"章指定の解決に失敗しました: {exc}") from exc
    except PageLabelResolutionError as exc:
        raise SystemExit(f"印刷ページラベルの解決に失敗しました: {exc}") from exc

    range_label = describe_page_range(args.chapter, args.start, args.end, args.start_label, args.end_label)

    output_dir = args.output_dir
    if output_dir is None:
        output_dir = default_output_dir(args.pdf_path, range_label)
        _log(f"[出力先] --output_dirが省略されたため、自動生成した出力先を使用します: {output_dir}")

    run_pipeline(args.pdf_path, output_dir, start_page=start_page, end_page=end_page, range_label=range_label)


if __name__ == "__main__":
    main()
