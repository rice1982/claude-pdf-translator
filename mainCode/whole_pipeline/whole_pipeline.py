"""PDF学術論文の解析からPDF翻訳成果物の生成までを1コマンドで実行する
エントリポイント。

処理の流れ（工程(1)〜(7)、詳細は ``doc/architecture.md`` の「パイプラインの7工程」を参照）:
    1. ページ範囲決定（工程(1)、:func:`stage1.resolve_page_range`）
       CLI引数（``--chapter``/``--start``/``--end``/``--start-label``/
       ``--end-label``）から、処理対象の物理ページ範囲を確定する。
    2. PDF解析（工程(2)、:func:`stage2.process_pdf`）
       前半でPDFをMinerUで解析し、生の構造化JSON（content_list）と画像群を
       得る。後半でcontent_listを文単位ID付きのページ別タグ付きMarkdown
       （page_XX_en.md）へ変換する。
    3. 構造化・タグ処理（工程(3)、:mod:`stage3`）
       タグ付きMarkdownを読み込んで :class:`shared.DocUnit` の順序付き
       リストと文書文脈へ組み立てる（参考文献セクションは翻訳対象から
       除外する）。
    4. 翻訳実行（工程(4)、:mod:`stage4`）
       DeepL（文脈パラメータ付き）で1文ずつ翻訳する（要DEEPL_API_KEY・
       実課金）。キー未設定・上限到達・通信エラーなど翻訳エンジン側の
       障害を検知した場合はエラーとして終了する。
    5. 翻訳後処理（工程(5)、:mod:`stage5`）
       数式プレースホルダを元の数式へ復元する（``apply_restore``）。
    6. 数式保護（工程(6)、:mod:`stage6`）
       未保護の数式らしき断片の検出・自動保護を行った上で、翻訳済み
       タグ付きMarkdownを書き出す（``stage6.write_translated_pages``）。
    7. PDF生成（工程(7)、:mod:`stage7`）
       対訳版／英語版／日本語版の3種類のPDFを生成する。インライン数式は
       KaTeXにより実際の数式として描画される。

入口は ``main()``。他の関数はCLI起動処理・出力先パスの決定・実行記録の
スナップショット書き出しを担い、いずれも ``main()`` からのみ呼ばれる。
構成の詳細は ``doc/architecture/whole_pipeline.md`` を参照。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# mainCode配下は工程(1)〜(7)の順に並べ、複数工程から横断的に使うshared.sharedを最後に置く。
from mainCode.stage1.stage1 import (
    ChapterResolutionError,
    PageLabelResolutionError,
    resolve_page_range,
)
from mainCode.stage2.stage2 import SUPPORTED_MINERU_BACKENDS, process_pdf
from mainCode.stage3.stage3 import prepare_translation_input
from mainCode.stage4.stage4 import RawTranslationResult, TranslationBackendError, translate_units
from mainCode.stage5.stage5 import apply_restore
from mainCode.stage6.stage6 import postprocess as stage6_postprocess, write_translated_pages
from mainCode.stage7.stage7 import render_units_to_pdfs
from mainCode.shared.shared import DocUnit
from mainCode.shared.shared import log as _log


# ============================================================================
# CLI起動処理
# ============================================================================


# main()のCLI引数パーサーを組み立てる。
def _build_arg_parser() -> argparse.ArgumentParser:
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
    parser.add_argument(
        "--mineru-backend", choices=SUPPORTED_MINERU_BACKENDS, default="pipeline",
        help="使用するMinerUバックエンド。pipeline: 軽量・高速（既定）。"
        "vlm-engine: CPUのみの環境でも動くが大幅に低速な代わりに数式・OCRの認識精度が高い。",
    )
    return parser


# 入力PDFファイルが実在するかを確認する（存在しなければSystemExit）。
def _require_pdf_exists(pdf_path: str | Path) -> None:
    if not Path(pdf_path).is_file():
        raise SystemExit(f"入力PDFファイルが見つかりません: {pdf_path}")


# ============================================================================
# 出力先の決定（output_dir・snapshot_dirの"パス"を決めるだけで、書き込みは
# 行わない。ただし対応は非対称: snapshot_dirへの書き込みは次の「実行記録の
# 書き出し」グループがこのファイル内で担うが、output_dirへの実際の書き込み
# （page_XX_en.md・生成PDF等）はstage2/stage6/stage7側の責務であり、この
# ファイルには存在しない）
# ============================================================================

# describe_page_range・default_output_dirの2つだけ、この節の他の関数と違い
# 先頭にアンダースコアの無い公開名にしている。引数が素朴な値のみで、args
# オブジェクトやcache/の命名規則などこのファイル固有の事情に依存しない、
# 独立した計算だから（＝どこにコピペしても意味が通じる）。


# CLI引数から、cache/配下のフォルダ名に使う人間可読な範囲記述子を組み立てる。
def describe_page_range(
    chapter: str | None,
    start: int | None,
    end: int | None,
    start_label: str | None = None,
    end_label: str | None = None,
) -> str:
    # 優先順位はresolve_page_rangeと揃える
    # （印刷ページラベル・物理ページ番号 > 章指定 > 指定なし＝全体）。
    if start_label is not None or end_label is not None:
        parts = [str(v) for v in (start_label, end_label) if v is not None]
        return "label" + "-".join(parts)
    if start is not None or end is not None:
        parts = [str(v) for v in (start, end) if v is not None]
        return "p" + "-".join(parts)
    if chapter is not None:
        return "chapter" + chapter.replace(",", "_")
    return "full"


# output_dir省略時に使う自動生成パスを組み立てる。
def default_output_dir(
    pdf_path: str | Path,
    range_label: str,
    timestamp: str | None = None,
) -> Path:
    if timestamp is None:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path("output") / f"manual_{Path(pdf_path).stem}_{range_label}_{timestamp}"


# --output_dir省略時、自動生成した出力先を返す（指定済みの場合はそのまま返す）。
def _resolve_output_dir(args: argparse.Namespace, range_label: str) -> str | Path:
    if args.output_dir is not None:
        return args.output_dir
    output_dir = default_output_dir(args.pdf_path, range_label)
    _log(f"[出力先] --output_dirが省略されたため、自動生成した出力先を使用します: {output_dir}")
    return output_dir


# pdf_path・range_labelが両方指定されている場合のみ、スナップショット保存先のパスを決める。
def _resolve_snapshot_dir(
    pdf_path: str | Path | None,
    range_label: str | None,
) -> Path | None:
    if pdf_path is None or range_label is None:
        return None
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return (
        # mainCode/whole_pipeline/whole_pipeline.pyから3階層上がプロジェクトルート
        Path(__file__).resolve().parent.parent.parent
        / "cache"
        / f"{Path(pdf_path).stem}_{range_label}"
        / f"real_deepl_output_{timestamp}"
    )


# ============================================================================
# 実行記録の書き出し（snapshot_dir決定後、対応する工程の完了直後にmain()が
# 呼ぶ。cache/配下への記録保存という仕組み自体をこのファイルに一貫して
# まとめてある）
# ============================================================================


# 工程(3)完了時点の状態（document_context・page_*_en.md）をsnapshot_dir/03_structured/へ書き出す。
def _write_structured_snapshot(output_dir: Path, snapshot_dir: Path, document_context: str) -> None:
    structured_dir = snapshot_dir / "03_structured"
    structured_dir.mkdir(parents=True, exist_ok=True)
    (structured_dir / "document_context.txt").write_text(document_context, encoding="utf-8")
    for md_path in sorted(output_dir.glob("page_*_en.md")):
        (structured_dir / md_path.name).write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")


# 翻訳エンジンからの生の応答をsnapshot_dir/04_deepl_output/へ記録として保存する。
def _write_translation_snapshot(
    snapshot_dir: Path, raw_results: dict[str, RawTranslationResult]
) -> None:
    target_dir = snapshot_dir / "04_deepl_output"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "raw_deepl_results.json").write_text(
        json.dumps({tag: asdict(raw) for tag, raw in raw_results.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# protect_confirmed_single_letter_leaks適用前（apply_restore直後）のDocUnitスナップショットをsnapshot_dir/05_restored/へ書き出す。
def _write_restore_snapshot(units: list[DocUnit], snapshot_dir: Path) -> None:
    restored_dir = snapshot_dir / "05_restored"
    restored_dir.mkdir(parents=True, exist_ok=True)
    (restored_dir / "units_raw.json").write_text(
        json.dumps([asdict(u) for u in units], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # output_dir側の同名page_XX_en.md/page_XX_ja.mdはこの後、工程(6)の
    # write_translated_pagesにより保護後の内容で上書きされるため、保護前の
    # 状態を確認したい場合はこちら（05_restored）を参照する。
    write_translated_pages(units, restored_dir)


# ============================================================================
# 入口（CLI起動処理・出力先の決定・実行記録の書き出しの3グループは、
# いずれもここからのみ呼ばれる）
# ============================================================================


# CLI引数の解析からPDF生成までの7工程を順に実行するエントリポイント。
def main() -> None:
    # 実行環境の準備。標準出力がUTF-8でない環境（Windowsのコンソール等）でも、
    # 日本語ログ・エラーメッセージが文字化け/UnicodeEncodeErrorにならない
    # ようにする。.envはDEEPL_API_KEY等の秘密情報の読み込み用。
    if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()

    # CLI引数の解釈
    args = _build_arg_parser().parse_args()

    # 入力PDFの存在確認（resolve_page_range/process_pdf内部の生の
    # FileNotFoundErrorより先に、分かりやすいSystemExitで止める）
    _require_pdf_exists(args.pdf_path)

    # ページ範囲・範囲記述子・出力先の決定（工程(1)）。
    # resolve_page_rangeが送出する2種類の例外（章指定・印刷ページラベルの
    # 解決失敗）は、_require_pdf_existsと同じ方針でSystemExitへ正規化する。
    # range_label・output_dirの決定は、解決済みのCLI引数（args.chapter等、
    # 解決前の生の値）を使うためresolve_page_range成功後に行う。
    try:
        start_page, end_page = resolve_page_range(
            args.pdf_path, args.chapter, args.start, args.end, args.start_label, args.end_label
        )
    except ChapterResolutionError as exc:
        raise SystemExit(f"章指定の解決に失敗しました: {exc}") from exc
    except PageLabelResolutionError as exc:
        raise SystemExit(f"印刷ページラベルの解決に失敗しました: {exc}") from exc
    range_label = describe_page_range(args.chapter, args.start, args.end, args.start_label, args.end_label)
    output_dir = Path(_resolve_output_dir(args, range_label))

    # 工程(2): PDF解析。MinerU実行（重い外部処理）とタグ付きMarkdown
    # （page_XX_en.md）への変換をprocess_pdf内部でまとめて行う。
    _log(f"[PDF解析] {args.pdf_path} を解析しています...")
    process_pdf(
        args.pdf_path, output_dir, start_page=start_page, end_page=end_page,
        range_label=range_label, mineru_backend=args.mineru_backend,
    )
    _log("[PDF解析] タグ付きMarkdownの生成が完了しました。")

    # 工程(3): タグ付きMarkdownの解析・数式正規化・参考文献除外・
    # 文書文脈の組み立て・数式保護（工程(3)の仕上げ）
    units, document_context = prepare_translation_input(output_dir)

    # 翻訳エンジンとの送受信内容を記録する実行スナップショットの保存先を決める
    # （pdf_path・range_labelが揃う通常実行時のみ有効。以降、工程(3)〜(5)の
    # 完了直後にそれぞれ書き出す）。ここでNoneになった場合（タグ付き
    # Markdownからの再開等、対象PDFが不明な場合）は以降の書き出しも
    # すべてスキップされる。
    snapshot_dir = _resolve_snapshot_dir(args.pdf_path, range_label)
    if snapshot_dir is not None:
        _write_structured_snapshot(output_dir, snapshot_dir, document_context)

    # 工程(4): 翻訳実行。DeepLを実際に呼ぶ、7工程中(2)と並ぶ実行必須の重い
    # 工程。キー未設定・上限到達・通信エラー等は分かりやすいSystemExitへ
    # 正規化する。
    try:
        raw_results = translate_units(units, document_context, log=_log)
    except TranslationBackendError as exc:
        raise SystemExit(f"翻訳に失敗しました: {exc}") from exc

    # 実行記録の保存（工程(4)の結果）
    if snapshot_dir is not None:
        _write_translation_snapshot(snapshot_dir, raw_results)

    # 工程(5): 翻訳後処理（数式復元）。翻訳エンジンへの再通信を伴わない
    # 決定的な処理のため、他の工程と違いlogコールバックを取らない。
    apply_restore(units, raw_results)
    # 実行記録の保存（工程(5)の結果）
    if snapshot_dir is not None:
        _write_restore_snapshot(units, snapshot_dir)

    # 工程(6): 数式保護。翻訳結果に残った未保護の数式らしき断片を検出・
    # 自動保護した上で、保護後の内容でoutput_dir配下のpage_XX_en.md/
    # page_XX_ja.mdを上書きする。
    stage6_postprocess(units, output_dir, log=_log)

    # 工程(7): PDF生成。対訳版・英語版・日本語版の3種類をoutput_dir配下へ
    # 出力する。
    pdf_paths = render_units_to_pdfs(units, output_dir, log=_log)

    # render_units_to_pdfsが生成したPDFパスをユーザーへ一覧表示する
    # （工程(7)自体の処理ではなく、main()のCLI完了報告）。
    for path in pdf_paths:
        _log(f"生成完了: {path}")


if __name__ == "__main__":
    main()
