"""Step 2: 構造解析モジュール。

MinerUのcontent_list（生のJSON要素列）を、本ツール独自のID体系
（例: ``[P1-S1-abstract-S1]``）を持つページ別の要素列（StructuredDocument）へ
変換する。責務は「翻訳対象の本文テキスト」と「非翻訳要素（数式・図表・
その他）」を分離し、それぞれに一意なIDを振ることに限定する。実際の翻訳
（Step 3）や最終的なMarkdown生成（Step 4）はここでは行わない。

要素の判定はMinerUが付与する ``type`` フィールド（"text"/"image"/"table"/
"chart"/"list"/"equation"/"aside_text"等）による汎用的な分岐のみで行い、
特定の論文の言い回しに依存するような分岐は避ける。想定外の ``type`` や、
解析中に例外が発生した項目は :class:`pdf_models.UnknownElement` に
フォールバックさせ、ドキュメント全体の処理を止めないようにする。その際も
既知の複数フィールド名（text/table_body/list_items/*_caption等）を
横断的に試すことで、未知の種別でもできる限り内容を保持する。
"""

from __future__ import annotations

import logging

from pdf_models import (
    CaptionElement,
    EquationElement,
    FigureElement,
    HeadingElement,
    LabeledElement,
    PageContent,
    StructuredDocument,
    TextBlockElement,
    TranslationUnit,
    UnknownElement,
)
from pdf_text_utils import (
    EQUATION_TAG_RE,
    HEADING_RE,
    parse_caption_label,
    restore_merged_hyphens,
    slugify_section_name,
    split_sentences,
)

logger = logging.getLogger(__name__)

FRONT_MATTER_LABELS = ["TITLE", "AUTHORS", "AFFIL"]
"""ページ1冒頭、最初の見出しが現れるまでのブロックに出現順で割り当てるラベル。"""

ABSTRACT_SECTION_ID = "abstract"
"""最初の章番号付き見出しが現れるまでの文（ABSTRACT見出し・本文含む）に用いる章ラベル。"""

# MinerUが本文として意味を持たせている（構造解析の対象とする）要素種別。
# これ以外の種別（例: "discarded"等、将来MinerU側に追加される可能性のある
# 種別）は個別のif分岐を増やすのではなく、_handle_unknown_item の
# フォールバックに任せる。
_NOISE_TYPES = {"aside_text"}
"""意味のあるコンテンツを含まないと判断してよい種別（arXiv IDなど）。"""


class _PageBuilder:
    """1ページ分の要素リストを構築する際に必要な状態（前付けラベルの割当
    状況、未ラベル画像の連番）をまとめて保持する小さなヘルパー。"""

    def __init__(self, page_idx: int, page_offset: int = 0) -> None:
        self.page_idx = page_idx
        self.page_offset = page_offset
        self.elements: list = []
        self.front_matter_count = 0
        self.unlabeled_seq = 0

    def has_heading(self) -> bool:
        return any(isinstance(e, HeadingElement) for e in self.elements)


_TEXT_FIELD_CANDIDATES = ("text", "table_body", "code_body")
"""要素がどの種別であっても、本文らしき内容を保持していそうなフィールド名の
候補（優先順）。MinerUの種別ごとにフィールド名が異なるため、未知の種別が
来ても諦めずに中身を拾えるよう複数試す。"""

_CAPTION_FIELD_CANDIDATES = ("image_caption", "table_caption", "chart_caption", "code_caption")
"""キャプションらしき内容を保持していそうなフィールド名の候補（優先順）。"""

_LIST_FIELD_CANDIDATES = ("list_items",)
"""箇条書き・参考文献リスト等、複数項目のテキストを保持していそうな
フィールド名の候補。"""


def _extract_raw_text(item: dict) -> str:
    """要素からベストエフォートでテキストらしきものを取り出す。
    UnknownElementフォールバック用。既知のテキスト系フィールドを
    優先順に試し、何も見つからなければ空文字列を返す。"""
    for field_name in _TEXT_FIELD_CANDIDATES:
        value = item.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for field_name in _LIST_FIELD_CANDIDATES:
        value = item.get(field_name)
        if isinstance(value, list) and value:
            return "\n".join(str(v) for v in value if v)
    for field_name in _CAPTION_FIELD_CANDIDATES:
        value = item.get(field_name)
        if isinstance(value, list) and value:
            return " ".join(str(v) for v in value if v)
    return ""


def _handle_list_item(item: dict, builder: _PageBuilder) -> None:
    """箇条書き・参考文献リスト等の要素を処理する。

    MinerUが既に ``list_items`` として項目単位に分割済みのため、独自の文
    分割（split_sentences）は行わず、各項目をそのまま1つの文として扱う
    （参考文献のように文中にピリオドを含む短い項目が多く、文分割ロジックに
    通すとかえって誤分割を起こしやすいため）。
    """
    raw_items = item.get("list_items") or []
    sentences = [restore_merged_hyphens(s.strip()) for s in raw_items if s.strip()]
    if sentences:
        builder.elements.append(TextBlockElement(sentences=sentences))


def _handle_image_or_table_item(item: dict, item_type: str, images_base, builder: _PageBuilder) -> None:
    """図・表・チャート要素を処理する。MinerUのimage_caption/table_caption/
    chart_captionは通常1件だが、レイアウト検出の都合で隣接する図表の
    キャプションが1つのブロックへ誤って結合されることがある（例: Fig.8と
    Fig.9のキャプションが、Fig.9側の画像ブロックにまとめて付与される）。
    独立した複数のFig./Table番号を検出した場合は、直前に追加した未ラベルの
    画像へ遡ってキャプションを割り当て直し、最後のキャプションを今回の
    画像に割り当てる。

    "chart"（グラフ画像）は構造上は"image"と同じ（img_path + キャプション）
    であり、キャプションの実際のラベル（Fig./Table）はキャプション文自体
    から判定するため、種別ごとに分岐する必要はない。
    """
    elements = builder.elements
    raw_captions = (
        item.get("image_caption") or item.get("table_caption") or item.get("chart_caption") or []
    )
    captions = [restore_merged_hyphens(c.strip()) for c in raw_captions if c.strip()]

    # MinerUはtable_caption/image_captionに、キャプション文と脚注（例: 表の
    # 下にある"*Our own implementation..."等の補足説明）を別要素として並べて
    # 返すことがある。脚注自体はFig./Tableラベルを持たないため、直前に現れた
    # ラベル付きキャプションへの継続として結合する（まだ一度もラベルが
    # 現れていない場合のみ、独立した未ラベル要素として扱う＝従来通り無視）。
    caption_groups: list[list] = []
    for c in captions:
        label = parse_caption_label(c)
        if label is not None or not caption_groups:
            caption_groups.append([c, label])
        else:
            caption_groups[-1][0] += " " + c
    labeled_captions = [(text, label) for text, label in caption_groups if label is not None]
    img_path = images_base / item["img_path"]

    if len(labeled_captions) >= 2:
        extra_count = len(labeled_captions) - 1
        backfill_targets: list[FigureElement] = []
        for el in reversed(elements):
            if len(backfill_targets) >= extra_count:
                break
            if isinstance(el, FigureElement) and not el.labeled:
                backfill_targets.append(el)
            else:
                break
        backfill_targets.reverse()
        for target, (cap_text, (fig_kind, number)) in zip(backfill_targets, labeled_captions):
            target.fig_kind = fig_kind
            target.number = number
            target.labeled = True
            cap_sentences = split_sentences(cap_text)
            if cap_sentences:
                insert_at = elements.index(target) + 1
                elements.insert(insert_at, CaptionElement(sentences=cap_sentences, number=number, fig_kind=fig_kind))

        own_text, (own_kind, own_number) = labeled_captions[len(backfill_targets)]
        elements.append(FigureElement(image_path=img_path, fig_kind=own_kind, number=own_number))
        own_sentences = split_sentences(own_text)
        if own_sentences:
            elements.append(CaptionElement(sentences=own_sentences, number=own_number, fig_kind=own_kind))
        return

    if labeled_captions:
        cap_text, (fig_kind, number) = labeled_captions[0]
        elements.append(FigureElement(image_path=img_path, fig_kind=fig_kind, number=number))
        cap_sentences = split_sentences(cap_text)
        if cap_sentences:
            elements.append(CaptionElement(sentences=cap_sentences, number=number, fig_kind=fig_kind))
    else:
        builder.unlabeled_seq += 1
        fallback_kind = "table" if item_type == "table" else "figure"
        elements.append(
            FigureElement(image_path=img_path, fig_kind=fallback_kind, number=builder.unlabeled_seq, labeled=False)
        )


def _handle_equation_item(item: dict, images_base, builder: _PageBuilder) -> None:
    """数式要素を処理する。MinerUが生成したLaTeXから \\tag{n} を探し、
    見つかればそれを元論文の式番号として使う（ページをまたいでもリセット
    しない）。見つからない場合は画像だけをフォールバック連番で出力する。
    """
    latex = item.get("text", "").strip()
    latex_stripped = latex.removeprefix("$$").removesuffix("$$").strip()
    tag_match = EQUATION_TAG_RE.search(latex)
    img_path = images_base / item["img_path"] if item.get("img_path") else None
    if img_path is None:
        return
    if tag_match is not None:
        builder.elements.append(
            EquationElement(image_path=img_path, latex=latex_stripped, number=int(tag_match.group(1)))
        )
    else:
        builder.unlabeled_seq += 1
        builder.elements.append(
            EquationElement(
                image_path=img_path, latex=latex_stripped, number=builder.unlabeled_seq, labeled=False
            )
        )


def _handle_text_item(item: dict, builder: _PageBuilder, unnumbered_heading_seq: list[int]) -> None:
    """通常のテキストブロックを処理する。ページ1冒頭は前付け（タイトル・
    著者・所属）として、見出しレベル（text_level 1か2）であれば
    HeadingElementとして、それ以外は本文段落として文分割する。

    Args:
        unnumbered_heading_seq: 文書全体で共有する、番号無し見出し用の
            連番カウンタ（1要素のリストに包んだ可変な整数）。書籍PDF等、
            見出しに章番号が付いていない場合の合成章番号（"u1", "u2", ...）
            の採番に使う。
    """
    text = restore_merged_hyphens(item.get("text", "").strip())
    if not text:
        return

    text_level = item.get("text_level")

    # PDF全体の1ページ目冒頭、最初の見出し（text_level==2）が現れるまでを
    # 前付けとして扱う（--start指定でPDFの途中から処理した場合、この
    # ページ範囲内での相対1ページ目は対象外。page_offsetを加えた絶対
    # ページ番号で判定する）。
    if builder.page_idx + builder.page_offset == 0 and text_level != 2:
        if builder.front_matter_count < len(FRONT_MATTER_LABELS) and not builder.has_heading():
            label = FRONT_MATTER_LABELS[builder.front_matter_count]
            builder.front_matter_count += 1
            builder.elements.append(LabeledElement(text=text, label=label))
            return

    # MinerUがtext_level 1（章タイトル相当）または2（節見出し相当）と
    # 判定したブロックは見出しとして扱う。論文特有の番号付き見出し
    # （"1. Introduction"）はHEADING_REで章番号・章名を取り出す。書籍の
    # 章・節見出しのように番号が付いていない場合も、本文に埋もれさせず、
    # 文書全体で一意な合成の章番号（"u1", "u2", ...）を振ってHeadingElement
    # として扱う（そうしないと、章の区切りが失われセクションIDが本文全体で
    # 単一のまま固定されてしまう）。
    #
    # 例外: "ABSTRACT"という見出し（論文特有、text_level==2で現れる）は、
    # ABSTRACT_SECTION_ID（既定の章ラベル"abstract"）が表す内容そのもの
    # なので、独立したHeadingElementにはせず、これまで通り本文の1文として
    # 扱う（そうしないと[P1-S1-abstract-S1] ABSTRACTという既存の文IDが
    # [P1-HEADING-u1.abstract]に変わってしまい、後方互換性が崩れる）。
    if text_level in (1, 2) and text.strip().upper() != "ABSTRACT":
        heading_match = HEADING_RE.match(text)
        if heading_match:
            section_num = heading_match.group(1)
            section_name = slugify_section_name(heading_match.group(2))
        else:
            unnumbered_heading_seq[0] += 1
            section_num = f"u{unnumbered_heading_seq[0]}"
            section_name = slugify_section_name(text)
        builder.elements.append(HeadingElement(text=text, section_num=section_num, section_name=section_name))
        return

    sentences = split_sentences(text)
    if sentences:
        builder.elements.append(TextBlockElement(sentences=sentences))


def _handle_unknown_item(item: dict, item_type: str, builder: _PageBuilder, reason: str = "") -> None:
    """未対応の要素種別、または既知種別の解析中に例外が発生した場合の
    フォールバック。生テキストをそのまま保持して処理を継続する。"""
    text = _extract_raw_text(item)
    logger.warning("未対応の要素種別 %r を検出しました（フォールバックで出力）: %s", item_type, reason or "unknown type")
    builder.elements.append(UnknownElement(raw_type=str(item_type), text=text, reason=reason))


def _build_pages(items: list[dict], images_base, page_offset: int = 0) -> list[PageContent]:
    """content_list要素列をページ単位のPageContentへ変換する。

    1要素の解析に失敗しても他の要素・ページの処理は継続する
    （UnknownElementへフォールバックする）。

    Args:
        page_offset: ``items``内の``page_idx``（常に0始まりの相対値）に
            加算する絶対ページオフセット（0始まり）。``--start``でPDFの
            途中から処理した場合に、実際のページ番号を復元するために使う。
    """
    builders: dict[int, _PageBuilder] = {}
    unnumbered_heading_seq = [0]

    for item in items:
        page_idx = item.get("page_idx", 0)
        builder = builders.setdefault(page_idx, _PageBuilder(page_idx, page_offset))
        item_type = item.get("type")

        if item_type in _NOISE_TYPES:
            continue  # arXiv IDなどのノイズ（意図的に捨てる）

        try:
            if item_type in ("image", "table", "chart"):
                _handle_image_or_table_item(item, item_type, images_base, builder)
            elif item_type == "equation":
                _handle_equation_item(item, images_base, builder)
            elif item_type == "list":
                _handle_list_item(item, builder)
            elif item_type == "text":
                _handle_text_item(item, builder, unnumbered_heading_seq)
            else:
                _handle_unknown_item(item, item_type, builder, reason="未知の要素種別")
        except Exception as exc:  # noqa: BLE001 - 1要素の失敗で全体を止めないためのフォールバック
            _handle_unknown_item(item, item_type, builder, reason=f"{type(exc).__name__}: {exc}")

    return [
        PageContent(page_number=idx + 1 + page_offset, elements=b.elements)
        for idx, b in sorted(builders.items())
    ]


def _assign_sentence_ids(pages: list[PageContent]) -> None:
    """本文の文（ABSTRACTを含む）とキャプションの文に、表示用IDを付与する。

    本文ID形式: [P{page}-S{ページ内通し番号}-{章ラベル}-S{章内通し番号}]
    - ページ内通し番号は各ページの先頭で1にリセットする。
    - 章内通し番号は、最初の章番号付き見出し（例: "1. INTRODUCTION"）が現れる
      までは ``abstract`` を章ラベルとして使う。同じ章が次ページに続く場合、
      章内通し番号はページをまたいでも継続する（見出しが変わった時のみリセット）。
    - タイトル・著者・所属・図・見出し自体はこのカウントに含めない。

    キャプションID形式: [P{page}-{FIG|TABLE}{n}-CAPTION-S{通し番号}]
    """
    section_id = ABSTRACT_SECTION_ID
    section_seq = 0
    for page in pages:
        page_seq = 0
        for element in page.elements:
            if isinstance(element, HeadingElement):
                section_id = element.section_id
                section_seq = 0
            elif isinstance(element, TextBlockElement):
                element.sentence_ids = []
                for _ in element.sentences:
                    page_seq += 1
                    section_seq += 1
                    element.sentence_ids.append(f"P{page.page_number}-S{page_seq}-{section_id}-S{section_seq}")
            elif isinstance(element, CaptionElement):
                prefix = "FIG" if element.fig_kind == "figure" else "TABLE"
                label = f"{prefix}{element.number}"
                element.sentence_ids = [
                    f"P{page.page_number}-{label}-CAPTION-S{seq}" for seq in range(1, len(element.sentences) + 1)
                ]


def _collect_translation_units(pages: list[PageContent]) -> list[TranslationUnit]:
    """本文テキスト（TextBlockElement・CaptionElement）だけを翻訳対象として
    フラットなリストにまとめる。翻訳ステップ（Step 3）はこのリストの
    unit_id/textだけを見ればよく、ページ構造やタイトル・見出し・図表画像
    といった非翻訳要素を一切意識しない。
    """
    units: list[TranslationUnit] = []
    for page in pages:
        for element in page.elements:
            if isinstance(element, (TextBlockElement, CaptionElement)):
                for sentence, unit_id in zip(element.sentences, element.sentence_ids):
                    units.append(TranslationUnit(unit_id=unit_id, text=sentence))
    return units


def analyze_structure(items: list[dict], images_base, page_offset: int = 0) -> StructuredDocument:
    """MinerUのcontent_list要素列から、ID付与済みのStructuredDocumentを構築する。

    Args:
        items: MinerUOutput.items（content_list.jsonの中身）。
        images_base: img_path（相対パス）を解決するための基準ディレクトリ。
        page_offset: ``--start``でPDFの途中から処理した場合の絶対ページ
            オフセット（0始まり）。省略時は0（PDFの先頭から処理）。

    Returns:
        ページ別の要素列と、翻訳対象文のフラットな一覧を保持するStructuredDocument。
    """
    pages = _build_pages(items, images_base, page_offset)
    _assign_sentence_ids(pages)
    translation_units = _collect_translation_units(pages)
    return StructuredDocument(pages=pages, translation_units=translation_units)
