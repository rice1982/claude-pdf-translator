"""印刷ページラベル（cov, i〜xviii, 1, 2, ...等）を物理ページ番号へ変換するモジュール。

書籍PDFは、表紙（cov）やローマ数字の前付けページの後に、算用数字の本文
ページが始まる、という「印刷ページ番号（本の見た目のページ数字。fitzの
``Page.get_label``で取得できる）」と「物理ページ番号（PDFファイル内での
1始まりの連番。``--start``/``--end``が受け取る値）」が一致しない構造を
持つことがある（詳細は :mod:`pdf_chapter_resolver` のモジュールdocstring
も参照）。

利用者が本の見た目のページ番号（印刷ラベル）で範囲を指定したい場合は、
``--start-label``/``--end-label``でこのモジュールを経由し、印刷ラベルを
物理ページ番号へ変換してから ``pdf_processor.process_pdf`` の
``start_page``/``end_page``（物理ページ番号）に渡す。
"""

from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF


class PageLabelResolutionError(ValueError):
    """印刷ページラベルから物理ページ番号を求められなかった場合に送出する例外。"""


def resolve_physical_page(pdf_path: str | Path, label: str) -> int:
    """印刷ページラベル（例: "cov", "i", "xviii", "36"）に対応する物理ページ
    番号（1始まり）を返す。

    大文字・小文字は区別しない（"COV"と"cov"は同一視する）。表紙ページは
    fitzが返す実際のラベルが"Cover"（フルワード）であることが多いため、
    利用者が短縮形"cov"を指定した場合は"cov"で始まるラベル（"Cover"等）
    にも一致させる。

    Raises:
        PageLabelResolutionError: 該当するラベルを持つページがPDF内に
            見つからない場合（印刷ページラベルの情報自体を持たないPDF
            を含む）。
    """
    normalized = label.strip().lower()
    doc = fitz.open(pdf_path)
    try:
        for physical_page in range(1, doc.page_count + 1):
            page_label = doc[physical_page - 1].get_label().strip().lower()
            if not page_label:
                continue
            if page_label == normalized:
                return physical_page
            if normalized == "cov" and page_label.startswith("cov"):
                return physical_page
    finally:
        doc.close()

    raise PageLabelResolutionError(
        f"印刷ページラベル {label!r} に対応するページがPDF内に見つかりません。"
        "このPDFが印刷ページラベルの情報を持たない場合は、"
        "--start-label/--end-labelの代わりに--start/--end（物理ページ番号）を指定してください。"
    )


def resolve_physical_page_range(
    pdf_path: str | Path, start_label: str | None, end_label: str | None
) -> tuple[int | None, int | None]:
    """``--start-label``/``--end-label``（印刷ページラベル）を物理ページ番号
    （1始まり）へ変換する。

    片方だけ指定された場合は、そちら側だけ変換し、もう片方は``None``のまま
    返す（呼び出し側で先頭ページ/末尾ページとして扱われる）。
    """
    start_page = resolve_physical_page(pdf_path, start_label) if start_label is not None else None
    end_page = resolve_physical_page(pdf_path, end_label) if end_label is not None else None
    return start_page, end_page
