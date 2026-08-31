"""工程(1)「ページ範囲の決定」モジュール。

``--chapter``/``--start-label``/``--end-label`` という、物理ページ番号
以外の指定方法を、``stage2.process_pdf`` が要求する物理ページ番号
（1始まり）へ変換する、という同じ種類の責務を持つ2つの独立したアルゴリズム
（印刷ページラベル系・章(目次)系）と、その2つを実際にCLI引数の優先順位に
従って呼び分ける入口関数（``resolve_page_range``）を1ファイルにまとめている。
進捗ログ出力には :func:`mainCode.shared.shared.log` を使う（``whole_pipeline``
側の実装との循環importを避けるため、ログ関数自体は他のどの``mainCode``
モジュールにも依存しない``shared/``に置かれている）。構成の詳細は
``doc/architecture/stage1.md`` を参照。

いずれも「``--start``/``--end``以外の方法で範囲を指定したい」という
同じ目的のための代替手段という位置づけであり、`shared/`データ型のような
「他の複数工程から呼ばれる」性質ではなく、あくまで工程(1)内部で完結する
実装のため、共通モジュールへは分離せずここに同居させる。

なお章(目次)系の``_is_body_page``は、印刷ページラベル系と同じ
``get_label()``を使って前付け判定を行っており、両者には元々わずかな
結合がある（詳細は各関数の定義前コメント参照）。
"""

from __future__ import annotations

import re
from pathlib import Path

import fitz  # PyMuPDF

from mainCode.shared.shared import log

# ============================================================================
# 印刷ページラベル系（--start-label/--end-label。fitzのPage.get_label()を扱う）
# ============================================================================


# 印刷ページラベルから物理ページ番号を求められなかった場合に送出する例外。
class PageLabelResolutionError(ValueError):
    pass


# 印刷ページラベル（例: "cov", "i", "xviii", "36"）に対応する物理ページ番号
# （1始まり）を返す（該当ページが無ければPageLabelResolutionError）。
def resolve_physical_page(pdf_path: str | Path, label: str) -> int:
    normalized = label.strip().lower()
    doc = fitz.open(pdf_path)
    try:
        # 全ページを先頭から走査し、印刷ページラベルが一致する最初の
        # ページを探す（ページラベルの無いページはget_label()が空文字列を
        # 返すため読み飛ばす）。
        for physical_page in range(1, doc.page_count + 1):
            page_label = doc[physical_page - 1].get_label().strip().lower()
            if not page_label:
                continue
            if page_label == normalized:
                return physical_page
            # 表紙ページはfitzが返す実際のラベルが"Cover"（フルワード）である
            # ことが多いため、利用者が短縮形"cov"を指定した場合はそれで始まる
            # ラベルにも一致させる。
            if normalized == "cov" and page_label.startswith("cov"):
                return physical_page
    finally:
        doc.close()

    raise PageLabelResolutionError(
        f"印刷ページラベル {label!r} に対応するページがPDF内に見つかりません。"
        "このPDFが印刷ページラベルの情報を持たない場合は、"
        "--start-label/--end-labelの代わりに--start/--end（物理ページ番号）を指定してください。"
    )


# --start-label/--end-label（印刷ページラベル）を物理ページ番号（1始まり）へ
# 変換する。片方だけ指定された場合は、そちら側だけ変換しもう片方はNoneのまま
# 返す（呼び出し側で先頭ページ/末尾ページとして扱われる）。
def resolve_physical_page_range(
    pdf_path: str | Path, start_label: str | None, end_label: str | None
) -> tuple[int | None, int | None]:
    start_page = resolve_physical_page(pdf_path, start_label) if start_label is not None else None
    end_page = resolve_physical_page(pdf_path, end_label) if end_label is not None else None
    return start_page, end_page


# ============================================================================
# 章(目次)系（--chapter。fitzのget_toc()を扱う）
# ============================================================================
#
# PDFの目次（TOC/Outline。PyMuPDFの``get_toc``で取得できるしおり構造）のうち
# 最も浅い階層（レベル最小値）の項目を、出現順に「第1章、第2章、...」と
# みなす。特定の見出し文言・番号付け規則（例: "Chapter 1"という文字列）には
# 依存しない汎用的な実装とすることで、目次の言い回しが異なる文書でも同じ
# ロジックで動作させる。
#
# 前付け（表紙・ローマ数字ページ）を持つ書籍PDFへの対応:
#     論文と異なり書籍PDFでは、表紙・目次・序文などの前付けにローマ数字の
#     印刷ページ番号（i, ii, ...）が振られ、本文の算用数字ページ番号
#     （1, 2, ...）はその後から始まる構造がよくある（本ツールの
#     ``get_toc``が返すページ番号自体は常にPDFの物理ページ番号＝絶対
#     インデックスであり、印刷ページ番号ではない点に注意）。
#     この場合、目次の最上位階層には前付け自体の見出し（例: "Preface",
#     "Introduction", "Contents"）も並んでしまい、これらを「第1章」等として
#     数えてしまうと、利用者が意図する実際の本文の章番号とズレる。
#     そこで、目次の各項目が指す物理ページの印刷ページラベル
#     （:meth:`fitz.Page.get_label`）を確認し、算用数字のラベルを持つ
#     項目（＝本文が始まった後の項目）だけを「章」として数える。印刷ページ
#     ラベルの情報が無い文書（学術論文PDF等）では全項目がそのまま「章」と
#     して扱われる。


# 章指定からページ範囲を求められなかった場合に送出する例外。
class ChapterResolutionError(ValueError):
    pass


_CHAPTER_SPEC_PART_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


# 章指定文字列（"1,2"、"1-2"、"1,3-4"等）を章番号（1始まり）のリストに変換する
# （不正な形式はChapterResolutionError）。
def parse_chapter_spec(spec: str) -> list[int]:
    numbers: set[int] = set()
    # カンマ区切りの各要素を、単独の数値（"2"）または範囲（"1-2"）として
    # 解釈し、章番号の集合へ展開する（集合を使うのは"1-3,2-4"のように
    # 範囲同士が重なった場合の重複を自然に排除するため）。
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
    # 呼び出し順に依存しない決定的な結果にするため昇順ソートして返す。
    return sorted(numbers)


# 物理ページ番号（1始まり）が本文（算用数字の印刷ページ番号）に該当するかを
# 判定する（前付け除外用。印刷ページラベルが無いPDFでは常にFalse）。
def _is_body_page(doc: fitz.Document, page_number_1based: int) -> bool:
    label = doc[page_number_1based - 1].get_label()
    return label.strip().isdigit()


# 章指定から、対象PDF内の開始・終了ページ（1始まり・両端含む）を求める
# （目次が無い、または章番号が範囲外の場合はChapterResolutionError）。
def resolve_chapter_page_range(pdf_path: str | Path, chapter_spec: str) -> tuple[int, int]:
    # 1. 章指定文字列（"1,2"等）を章番号のリストへ展開する。
    chapter_numbers = parse_chapter_spec(chapter_spec)

    doc = fitz.open(pdf_path)
    try:
        # 2. 目次(TOC)を取得する。目次が無いPDFでは章番号を解決しようが
        # ないため、ここで打ち切る。
        toc = doc.get_toc(simple=True)
        total_pages = doc.page_count

        if not toc:
            raise ChapterResolutionError(
                "このPDFには目次（TOC/Outline）情報が見つかりません。"
                "--chapterの代わりに--start/--endでページ範囲を指定してください。"
            )

        # 3. 目次の最上位階層（レベル最小値）の項目だけを「章」の候補とする
        # （節・小節等の下位階層は無視する）。
        top_level = min(level for level, _title, _page in toc)
        top_level_pages = [page for level, _title, page in toc if level == top_level]

        # 4. 前付け（ローマ数字ページ等）を除外し、本文（算用数字ページ）が
        # 始まった後の項目だけを「章」として数える。該当する項目が1つも
        # 無い文書（印刷ページラベルの情報が無い学術論文PDF等）では、
        # 全項目をそのまま使う。
        body_pages = [page for page in top_level_pages if _is_body_page(doc, page)]
        chapter_start_pages = body_pages if body_pages else top_level_pages
    finally:
        doc.close()

    # chapter_start_pagesは「章1の開始ページ、章2の開始ページ、…」を
    # TOCリストの登場順（物理ページ順ではない）に並べたものになる。
    # インデックス n-1 が章番号 n の開始ページに対応する。
    max_chapter = len(chapter_start_pages)

    # 5. 各章の終了ページを求める準備として、開始ページの集合を昇順に
    # 並べておく。章の終了ページは、TOCの並び順（次の項目）ではなく、
    # 全章の開始ページ集合の中でその章の開始ページより大きい最小値の
    # 直前までとする。ブックマークが物理的なページ順と一致しない文書
    # （LaTeX由来のPDFでしおりの並びが崩れているケース等）でも、開始
    # ページの大小関係だけで安定して終了ページを決められるようにするため。
    sorted_starts = sorted(set(chapter_start_pages))

    def _end_page_for(start_page: int) -> int:
        later_starts = [p for p in sorted_starts if p > start_page]
        return (min(later_starts) - 1) if later_starts else total_pages

    # 6. 指定された章番号ごとに開始・終了ページを求め、それらすべてを
    # 包含する最小の連続範囲（開始の最小値〜終了の最大値）を返す。
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


# ============================================================================
# 入口（印刷ページラベル系・章系のどちらを使うかをCLI引数の優先順位から判断）
# ============================================================================


# --start/--end（物理ページ番号）・--start-label/--end-label（印刷ページ
# ラベル）・--chapter（章指定）という5つのCLI引数の優先順位（物理ページ
# 指定 > 印刷ラベル指定 > 章指定）を判断し、resolve_physical_page_range／
# resolve_chapter_page_rangeのどちらか一方を呼ぶ（いずれも指定が無ければ
# (None, None)を返す）。
def resolve_page_range(
    pdf_path: str | Path,
    chapter: str | None,
    start: int | None,
    end: int | None,
    start_label: str | None = None,
    end_label: str | None = None,
) -> tuple[int | None, int | None]:
    # --start/--end・--start-label/--end-label・--chapterからページ範囲を
    # 決定する（物理ページ指定 > 印刷ラベル指定 > 章指定の優先順位）。
    # 同じ境界（開始または終了）に対して物理ページ番号と印刷ページラベルの
    # 両方が指定された場合は、どちらを優先すべきか一意に決まらないため
    # エラーにする（例: --start 5 --start-label "3"）。
    if start is not None and start_label is not None:
        raise ValueError("--startと--start-labelは同時に指定できません。")
    if end is not None and end_label is not None:
        raise ValueError("--endと--end-labelは同時に指定できません。")

    # start_label/end_labelが指定されていれば、まず物理ページ番号へ変換し、
    # start/endへ書き戻す（以降はstart/endだけを見ればよい状態にする）。
    # 片方だけ指定された場合はそちら側だけが上書きされ、もう片方
    # （resolve_physical_page_rangeがNoneを返す側）はそのまま保たれる。
    if start_label is not None or end_label is not None:
        resolved_start, resolved_end = resolve_physical_page_range(pdf_path, start_label, end_label)
        if start_label is not None:
            log(f"[印刷ページラベル] --start-label {start_label!r} を物理ページ {resolved_start} に解決しました。")
            start = resolved_start
        if end_label is not None:
            log(f"[印刷ページラベル] --end-label {end_label!r} を物理ページ {resolved_end} に解決しました。")
            end = resolved_end

    # ここまでの時点でstart/endのどちらかが値を持っていれば、物理ページ
    # 指定または印刷ラベル指定（変換済み）が確定しているということなので、
    # --chapterが指定されていても無視してそちらを優先する。
    if start is not None or end is not None:
        if chapter is not None:
            log("※ --chapterとページ範囲指定（--start/--end/--start-label/--end-label）が同時に指定されたため、ページ範囲指定を優先します。")
        return start, end

    # 物理ページ・印刷ラベルのどちらも指定が無く、--chapterのみが
    # 指定されている場合は、目次(TOC)から対応するページ範囲を自動解決する。
    if chapter is not None:
        start_page, end_page = resolve_chapter_page_range(pdf_path, chapter)
        log(f"[章指定] --chapter {chapter} をページ範囲 {start_page}-{end_page} に解決しました。")
        return start_page, end_page

    # 5つの引数のいずれも指定が無い場合は、範囲を確定させず(None, None)を
    # 返す（呼び出し側で「全ページを処理対象とする」の意味で扱われる）。
    return None, None
