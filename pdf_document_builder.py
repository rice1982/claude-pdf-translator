"""Step 4: 成果物結合モジュール。

Step 2が構築したStructuredDocument（ページ別の要素列）と、Step 3が返した
翻訳結果（unit_id -> 翻訳後テキストの辞書）を組み合わせて、最終的な
ページ別Markdownを書き出す。翻訳結果に存在しない文（翻訳ステップ未実行、
または対象外の要素）は原文をそのまま使う。画像の保存もこのステップの責務。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from pdf_models import (
    CaptionElement,
    EquationElement,
    FigureElement,
    HeadingElement,
    LabeledElement,
    PageContent,
    StructuredDocument,
    TextBlockElement,
    UnknownElement,
)


def _figure_label(fig_kind: str, number: int, labeled: bool) -> str:
    """図・表・数式のラベル文字列を生成する（例: "FIG3", "TABLE1", "EQ2"）。"""
    prefix = {"figure": "FIG", "table": "TABLE", "equation": "EQ"}[fig_kind]
    if labeled:
        return f"{prefix}{number}"
    return f"{prefix}-UNLABELED{number}"


def _save_element_image(element, images_dir: Path, page_number: int) -> str:
    """FigureElement/EquationElementの画像をPNGとして保存し、Markdownから
    参照する相対パスを返す。"""
    if isinstance(element, FigureElement):
        prefix = "fig" if element.fig_kind == "figure" else "table"
    else:
        prefix = "eq"
    if element.labeled:
        filename = f"{prefix}_p{page_number}_{element.number}.png"
    else:
        filename = f"{prefix}_p{page_number}_unlabeled{element.number}.png"
    with Image.open(element.image_path) as img:
        img.convert("RGB").save(images_dir / filename)
    return f"images/{filename}"


def _translated_sentences(sentences: list[str], sentence_ids: list[str], translations: dict[str, str]) -> list[str]:
    """各文について、翻訳結果があればそれを、無ければ原文を返す
    （Step 3が失敗・未実行だった文への安全なフォールバック）。"""
    return [translations.get(sent_id, sentence) for sentence, sent_id in zip(sentences, sentence_ids)]


def _render_page_markdown(page: PageContent, images_dir: Path, translations: dict[str, str]) -> str:
    """1ページ分の要素列をMarkdownテキストへレンダリングする（画像の保存も行う）。"""
    lines: list[str] = []
    for element in page.elements:
        if isinstance(element, (FigureElement, EquationElement)):
            path = _save_element_image(element, images_dir, page.page_number)
            fig_kind = element.fig_kind if isinstance(element, FigureElement) else "equation"
            label = _figure_label(fig_kind, element.number, element.labeled)
            elem_id = f"P{page.page_number}-{label}"
            lines.append(f"![{elem_id}]({path}) [{elem_id}]")
            lines.append("")
            if isinstance(element, EquationElement):
                lines.append(f"[{elem_id}-LATEX] $${element.latex}$$")
                lines.append("")
        elif isinstance(element, LabeledElement):
            label_id = f"P{page.page_number}-{element.label}"
            lines.append(f"[{label_id}] {element.text}")
        elif isinstance(element, HeadingElement):
            heading_id = f"P{page.page_number}-HEADING-{element.section_id}"
            lines.append(f"[{heading_id}] {element.text}")
        elif isinstance(element, CaptionElement):
            sentences = _translated_sentences(element.sentences, element.sentence_ids, translations)
            for sentence, sent_id in zip(sentences, element.sentence_ids):
                lines.append(f"[{sent_id}] {sentence}")
        elif isinstance(element, UnknownElement):
            unknown_id = f"P{page.page_number}-UNKNOWN-{element.raw_type}"
            lines.append(f"[{unknown_id}] {element.text}")
        elif isinstance(element, TextBlockElement):
            sentences = _translated_sentences(element.sentences, element.sentence_ids, translations)
            for sentence, sent_id in zip(sentences, element.sentence_ids):
                lines.append(f"[{sent_id}] {sentence}")
    return "\n".join(lines) + "\n"


def build_document(
    doc: StructuredDocument,
    translations: dict[str, str],
    output_dir: Path,
    first_page: int,
    last_page: int,
) -> list[Path]:
    """StructuredDocumentと翻訳結果からページ別Markdownを書き出す。

    Args:
        doc: Step 2の出力（ページ別要素列＋翻訳対象文一覧）。
        translations: Step 3の出力（unit_id -> 翻訳後テキスト）。
        output_dir: 出力先ディレクトリ（``images/`` サブディレクトリに画像を保存する）。
        first_page: 出力対象の先頭ページ番号（1始まり）。通常は1だが、
            ``--start``指定時はその値になる。
        last_page: 出力対象の末尾ページ番号（1始まり・両端含む）。要素が
            1つも無いページも含め、``first_page``から``last_page``まで
            すべてのMarkdownファイルを出力する。

    Returns:
        生成されたMarkdownファイルパスのリスト（ページ順）。
    """
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    pages_by_number = {p.page_number: p for p in doc.pages}
    all_pages = [
        pages_by_number.get(n, PageContent(page_number=n)) for n in range(first_page, last_page + 1)
    ]

    md_paths: list[Path] = []
    for page in all_pages:
        markdown = _render_page_markdown(page, images_dir, translations)
        md_path = output_dir / f"page_{page.page_number:02d}_en.md"
        md_path.write_text(markdown, encoding="utf-8")
        md_paths.append(md_path)
    return md_paths
