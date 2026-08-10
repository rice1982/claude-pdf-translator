"""章指定（``--chapter``）をページ範囲へ変換するモジュール。

PDFの目次（TOC/Outline。PyMuPDFの``get_toc``で取得できるしおり構造）のうち
最も浅い階層（レベル最小値）の項目を、出現順に「第1章、第2章、...」と
みなす。特定の見出し文言・番号付け規則（例: "Chapter 1"という文字列）には
依存しない汎用的な実装とすることで、目次の言い回しが異なる文書でも同じ
ロジックで動作させる。

前付け（表紙・ローマ数字ページ）を持つ書籍PDFへの対応:
    論文と異なり書籍PDFでは、表紙・目次・序文などの前付けにローマ数字の
    印刷ページ番号（i, ii, ...）が振られ、本文の算用数字ページ番号
    （1, 2, ...）はその後から始まる構造がよくある（本ツールの
    ``get_toc``が返すページ番号自体は常にPDFの物理ページ番号＝絶対
    インデックスであり、印刷ページ番号ではない点に注意）。
    この場合、目次の最上位階層には前付け自体の見出し（例: "Preface",
    "Introduction", "Contents"）も並んでしまい、これらを「第1章」等として
    数えてしまうと、利用者が意図する実際の本文の章番号とズレる。
    そこで、目次の各項目が指す物理ページの印刷ページラベル
    （:meth:`fitz.Page.get_label`）を確認し、算用数字のラベルを持つ
    項目（＝本文が始まった後の項目）だけを「章」として数える。印刷ページ
    ラベルの情報が無い文書（学術論文PDF等）では全項目がそのまま「章」と
    して扱われ、従来の挙動と変わらない。
"""

from __future__ import annotations

import re
from pathlib import Path

import fitz  # PyMuPDF


class ChapterResolutionError(ValueError):
    """章指定からページ範囲を求められなかった場合に送出する例外。"""


_CHAPTER_SPEC_PART_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


def _is_body_page(doc: fitz.Document, page_number_1based: int) -> bool:
    """物理ページ番号（1始まり）が「本文（算用数字の印刷ページ番号）」
    に該当するかを判定する。前付け（ローマ数字ページ等）を除外するために
    使う。印刷ページラベルの情報が無いPDF（学術論文PDF等）では、常に
    ``False``を返す（＝この判定自体を使わないよう呼び出し側で制御する）。
    """
    label = doc[page_number_1based - 1].get_label()
    return label.strip().isdigit()


def parse_chapter_spec(spec: str) -> list[int]:
    """章指定文字列を章番号（1始まり）のリストに変換する。

    "1,2"（カンマ区切り）、"1-2"（範囲指定）、およびその混在
    （例: "1,3-4"）に対応する。

    Raises:
        ChapterResolutionError: 空文字列や不正な形式が渡された場合。
    """
    numbers: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = _CHAPTER_SPEC_PART_RE.match(part)
        if not m:
            raise ChapterResolutionError(f"不正な章指定です: {part!r}")
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) is not None else start
        if start < 1 or end < start:
            raise ChapterResolutionError(f"不正な章範囲です: {part!r}")
        numbers.update(range(start, end + 1))

    if not numbers:
        raise ChapterResolutionError(f"章番号が1つも指定されていません: {spec!r}")
    return sorted(numbers)


def resolve_chapter_page_range(pdf_path: str | Path, chapter_spec: str) -> tuple[int, int]:
    """章指定から、対象PDF内の開始・終了ページ（1始まり・両端含む）を求める。

    Args:
        pdf_path: 入力PDFファイルのパス。
        chapter_spec: 章指定文字列（:func:`parse_chapter_spec` 参照）。

    Returns:
        (開始ページ, 終了ページ) のタプル（両端含む、1始まり）。複数章を
        指定した場合は、それらすべてを含む最小の連続ページ範囲を返す。

    Raises:
        ChapterResolutionError: PDFに目次情報が無い場合、または指定された
            章番号が目次の章数を超えている場合。
    """
    chapter_numbers = parse_chapter_spec(chapter_spec)

    doc = fitz.open(pdf_path)
    try:
        toc = doc.get_toc(simple=True)
        total_pages = doc.page_count

        if not toc:
            raise ChapterResolutionError(
                "このPDFには目次（TOC/Outline）情報が見つかりません。"
                "--chapterの代わりに--start/--endでページ範囲を指定してください。"
            )

        top_level = min(level for level, _title, _page in toc)
        top_level_pages = [page for level, _title, page in toc if level == top_level]

        # 前付け（ローマ数字ページ等）を除外し、本文（算用数字ページ）が
        # 始まった後の項目だけを「章」として数える。該当する項目が1つも
        # 無い文書（印刷ページラベルの情報が無い学術論文PDF等）では、
        # 全項目をそのまま使う（従来の挙動を維持）。
        body_pages = [page for page in top_level_pages if _is_body_page(doc, page)]
        chapter_start_pages = body_pages if body_pages else top_level_pages
    finally:
        doc.close()

    max_chapter = len(chapter_start_pages)

    # 章の終了ページは、TOCの並び順（次の項目）ではなく、全章の開始ページ
    # 集合の中でその章の開始ページより大きい最小値の直前までとする。
    # ブックマークが物理的なページ順と一致しない文書（LaTeX由来のPDFで
    # しおりの並びが崩れているケース等）でも、開始ページの大小関係だけで
    # 安定して終了ページを決められるようにするため。
    sorted_starts = sorted(set(chapter_start_pages))

    def _end_page_for(start_page: int) -> int:
        later_starts = [p for p in sorted_starts if p > start_page]
        return (min(later_starts) - 1) if later_starts else total_pages

    starts: list[int] = []
    ends: list[int] = []
    for n in chapter_numbers:
        if not (1 <= n <= max_chapter):
            raise ChapterResolutionError(
                f"章番号 {n} はPDFの目次に存在しません（有効範囲: 1-{max_chapter}）。"
            )
        start_page = chapter_start_pages[n - 1]
        starts.append(start_page)
        ends.append(_end_page_for(start_page))

    return min(starts), max(ends)
