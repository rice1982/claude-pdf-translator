"""Step 1: MinerU実行モジュール。

責務はPDFをMinerU（レイアウト検出・OCR・数式認識・表認識のパイプライン）に
通し、生の構造化JSON（content_list）と画像群を取得することのみに限定する。
論文の意味的な構造解釈（本文/非翻訳要素の分離、章立ての判定等）は一切行わない
（それはStep 2 の責務）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from mineru_cache import load_cached_items, save_cache


class MinerURunError(RuntimeError):
    """MinerUの実行に失敗した場合に送出する例外。

    このステップが失敗すると後続のステップに渡す生データが一切無いため、
    ここでの失敗はフォールバックせず明確なエラーとして呼び出し元に伝える。
    """


@dataclass
class MinerUOutput:
    """MinerU実行結果の受け渡し用データ。"""

    items: list[dict]
    """content_list.json の中身（ページ横断のフラットな要素リスト）。"""
    images_base: Path
    """items中の img_path（相対パス）を解決するための基準ディレクトリ。"""


def run_mineru(
    pdf_path: Path,
    work_dir: Path,
    start_page: int | None = None,
    end_page: int | None = None,
    range_label: str | None = None,
) -> MinerUOutput:
    """MinerUをサブプロセスとして実行し、content_list.jsonと画像群を取得する。

    Args:
        pdf_path: 入力PDFファイルのパス。
        work_dir: MinerUの生出力を書き込む作業用ディレクトリ（呼び出し側が
            後片付けする想定。通常は一時ディレクトリ）。
        start_page: 処理対象の開始ページ（0始まり・両端含む）。``None``なら
            先頭ページから処理する。
        end_page: 処理対象の終了ページ（0始まり・両端含む）。``None``なら
            末尾ページまで処理する。
        range_label: cache/配下のフォルダ名を人間可読にするための任意の
            範囲記述子（例:"full","label55-60"）。省略時は
            :mod:`mineru_cache` の従来の命名（ページ番号ベース）を使う。
            キャッシュの正当性判定には影響しない（start_page/end_pageで
            行う）。

    Returns:
        MinerUOutput（content_list要素列と画像の基準ディレクトリ）。
        ページ範囲を指定した場合、``items``の``page_idx``は指定範囲内での
        相対値（先頭が常に0）になる点に注意（呼び出し側でオフセットを
        加算する責務を持つ）。

    Raises:
        MinerURunError: MinerUのプロセスが異常終了した場合、または期待した
            出力ファイルが生成されなかった場合。

    Note:
        内部でローカルキャッシュ（:mod:`mineru_cache`）を利用する場合がある。
        同一PDF・同一ページ範囲・同一MinerUバージョンでの再実行はキャッシュ
        から返るため、``work_dir``にMinerUの生出力が書き込まれないことが
        ある。呼び出し側の型・戻り値仕様はキャッシュの有無によらず不変。
    """
    cached = load_cached_items(pdf_path, start_page, end_page, range_label=range_label)
    if cached is not None:
        items, images_base = cached
        return MinerUOutput(items=items, images_base=images_base)

    command = [
        sys.executable, "-m", "mineru.cli.client",
        "--path", str(pdf_path),
        "--output", str(work_dir),
        "--backend", "pipeline",
        "--method", "auto",
    ]
    if start_page is not None:
        command += ["--start", str(start_page)]
    if end_page is not None:
        command += ["--end", str(end_page)]

    try:
        subprocess.run(command, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        raise MinerURunError(f"MinerUの実行に失敗しました（終了コード {exc.returncode}）: {stderr}") from exc

    stem = pdf_path.stem
    content_list_path = work_dir / stem / "auto" / f"{stem}_content_list.json"
    if not content_list_path.exists():
        raise MinerURunError(f"MinerUの出力が見つかりません: {content_list_path}")

    items = json.loads(content_list_path.read_text(encoding="utf-8"))
    images_base = content_list_path.parent
    save_cache(pdf_path, start_page, end_page, items, images_base, range_label=range_label)
    return MinerUOutput(items=items, images_base=images_base)
