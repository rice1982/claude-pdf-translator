"""HTML/CSSを経由したPDFレンダリングモジュール（Playwright使用）。

翻訳済みの :class:`translation_models.DocUnit` 列を表示単位（見出し・段落・
図表）にまとめ、①対訳版 ②英語版のみ ③日本語版のみ の3種類のHTMLを組み立てて
Playwright（Chromium）でPDF化する。対訳版は文ペアをTable Rowで結合し、
CSSの `break-inside: avoid` を各行に適用することで、左右の高さズレと
文の途中でのページ跨ぎを防止する。日本語の文字化け対策として、CSSの
font-familyにNoto Sans JP系のフォントスタックを指定する。
"""

from __future__ import annotations

import base64
import html
import re
from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import sync_playwright

from katex_assets import load_katex_assets
from translation_models import Block, DocUnit

_FONT_STACK = "'Noto Sans JP', 'Yu Gothic UI', 'Yu Gothic', Meiryo, 'Hiragino Sans', sans-serif"

_CAPTION_KEY_RE = re.compile(r"^(.*)-S\d+$")
_BODY_TAG_RE = re.compile(r"^(P\d+)-S\d+-(.+)-S\d+$")
_HEADING_SUFFIX_RE = re.compile(r"^P\d+-HEADING-(.+)$")


def _heading_level(tag: str) -> int:
    m = _HEADING_SUFFIX_RE.match(tag)
    if not m:
        return 2
    numeric_depth = 0
    for part in m.group(1).split("."):
        if part.isdigit():
            numeric_depth += 1
        else:
            break
    return min(2 + max(numeric_depth - 1, 0), 4)


def _paragraph_key(unit: DocUnit) -> str:
    if unit.kind == "caption_sentence":
        m = _CAPTION_KEY_RE.match(unit.tag)
        return m.group(1) if m else unit.tag
    m = _BODY_TAG_RE.match(unit.tag)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return unit.tag


def _image_data_uri(output_dir: Path, rel_path: str) -> str:
    data = (output_dir / rel_path).read_bytes()
    return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"


def build_blocks(units: list[DocUnit], output_dir: Path) -> list[Block]:
    """DocUnit列をPDFレンダリング用のBlock列へまとめる。"""
    blocks: list[Block] = []
    current: list[DocUnit] = []
    current_key: str | None = None

    def flush() -> None:
        nonlocal current, current_key
        if current:
            blocks.append(Block(kind="paragraph", sentences=current))
        current = []
        current_key = None

    for unit in units:
        if unit.kind == "title":
            flush()
            blocks.append(Block(kind="title", sentences=[unit]))
        elif unit.kind in ("authors", "affil"):
            flush()
            blocks.append(Block(kind="meta", role=unit.kind, sentences=[unit]))
        elif unit.kind == "heading":
            flush()
            blocks.append(Block(kind="heading", level=_heading_level(unit.tag), sentences=[unit]))
        elif unit.kind in ("body_sentence", "caption_sentence"):
            key = _paragraph_key(unit)
            if key != current_key:
                flush()
                current_key = key
            current.append(unit)
        elif unit.kind in ("figure_image", "equation_image"):
            flush()
            image_uri = _image_data_uri(output_dir, unit.image_rel_path) if unit.image_rel_path else None
            blocks.append(Block(kind="figure", sentences=[unit], image_data_uri=image_uri))
        elif unit.kind == "equation_latex":
            continue  # 対応するfigureブロックの画像で表示済みのため出力しない
        else:  # unknown
            flush()
            blocks.append(Block(kind="paragraph", sentences=[unit]))

    flush()
    return blocks


def _esc(text: str) -> str:
    return html.escape(text)


# --- 対訳版 -------------------------------------------------------------


def _bilingual_row(en_html: str, ja_html: str, row_class: str = "") -> str:
    cls = f' class="{row_class}"' if row_class else ""
    return f'<tr{cls}><td class="en">{en_html}</td><td class="ja">{ja_html}</td></tr>'


def _render_bilingual_block(block: Block) -> str:
    if block.kind == "title":
        unit = block.sentences[0]
        return (
            '<table class="bilingual title-block">'
            + _bilingual_row(f"<strong>{_esc(unit.en_text)}</strong>", f"<strong>{_esc(unit.ja_text)}</strong>")
            + "</table>"
        )
    if block.kind == "meta":
        unit = block.sentences[0]
        return f'<div class="meta">{unit.en_text}</div>'
    if block.kind == "heading":
        unit = block.sentences[0]
        return (
            f'<table class="bilingual heading-block level-{block.level}">'
            + _bilingual_row(_esc(unit.en_text), _esc(unit.ja_text))
            + "</table>"
        )
    if block.kind == "paragraph":
        rows = "".join(_bilingual_row(_esc(u.en_text), _esc(u.ja_text)) for u in block.sentences)
        return f'<table class="bilingual">{rows}</table>'
    if block.kind == "figure":
        parts = [f'<img src="{block.image_data_uri}" alt="{_esc(block.sentences[0].tag)}">']
        return f'<figure>{"".join(parts)}</figure>'
    return ""


def render_bilingual_html(blocks: list[Block]) -> str:
    body_parts: list[str] = []
    for block in blocks:
        rendered = _render_bilingual_block(block)
        if rendered:
            body_parts.append(rendered)
        # 図の直後に続くキャプション段落は figure ブロックとして分離されないため、
        # 通常の paragraph ブロックとしてそのまま対訳テーブルで出力される。
    return _wrap_html("対訳版", "".join(body_parts), bilingual=True)


# --- 単一言語版（英語のみ／日本語のみ） -----------------------------------


def _sentence_text(unit: DocUnit, lang: str) -> str:
    return unit.en_text if lang == "en" else unit.ja_text


def _render_mono_block(block: Block, lang: str) -> str:
    if block.kind == "title":
        unit = block.sentences[0]
        return f"<h1>{_esc(_sentence_text(unit, lang))}</h1>"
    if block.kind == "meta":
        unit = block.sentences[0]
        return f'<div class="meta">{unit.en_text}</div>'
    if block.kind == "heading":
        unit = block.sentences[0]
        return f"<h{block.level}>{_esc(_sentence_text(unit, lang))}</h{block.level}>"
    if block.kind == "paragraph":
        text = " ".join(_esc(_sentence_text(u, lang)) for u in block.sentences)
        return f"<p>{text}</p>"
    if block.kind == "figure":
        return f'<figure><img src="{block.image_data_uri}" alt="{_esc(block.sentences[0].tag)}"></figure>'
    return ""


def render_mono_html(blocks: list[Block], lang: str) -> str:
    title = "英語版" if lang == "en" else "日本語版"
    body = "".join(_render_mono_block(block, lang) for block in blocks)
    return _wrap_html(title, body, bilingual=False, lang="en" if lang == "en" else "ja")


# --- HTML/CSSテンプレート -------------------------------------------------


def _wrap_html(title: str, body: str, bilingual: bool, lang: str = "ja") -> str:
    bilingual_css = """
        table.bilingual { width: 100%; border-collapse: collapse; table-layout: fixed; margin: 4pt 0; }
        table.bilingual td { width: 50%; vertical-align: top; padding: 4pt 8pt; font-size: 10pt; }
        table.bilingual td.en { border-right: 1px solid #ddd; }
        table.bilingual tr { break-inside: avoid; page-break-inside: avoid; }
        table.title-block td { text-align: center; font-size: 15pt; padding: 10pt 8pt; }
        table.heading-block td { font-weight: bold; }
        table.heading-block.level-2 td { font-size: 13pt; }
        table.heading-block.level-3 td { font-size: 11.5pt; }
        table.heading-block.level-4 td { font-size: 10.5pt; }
        table.heading-block { break-after: avoid; margin-top: 14pt; }
    """
    mono_css = """
        h1 { font-size: 16pt; text-align: center; break-after: avoid; }
        h2 { font-size: 13pt; margin-top: 16pt; break-after: avoid; border-bottom: 1px solid #ccc; padding-bottom: 2pt; }
        h3 { font-size: 11.5pt; margin-top: 12pt; break-after: avoid; }
        h4 { font-size: 10.5pt; margin-top: 10pt; break-after: avoid; }
        p { margin: 6pt 0; text-align: justify; }
    """
    katex_css, katex_js, autorender_js = load_katex_assets()
    return f"""<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<title>{_esc(title)}</title>
<style>{katex_css}</style>
<style>
  @page {{ size: A4; margin: 18mm 14mm; }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: {_FONT_STACK};
    font-size: 10.5pt;
    line-height: 1.7;
    color: #1a1a1a;
  }}
  .meta {{ text-align: center; font-size: 9.5pt; color: #444; margin: 2pt 0; }}
  figure {{ text-align: center; margin: 12pt 0; break-inside: avoid; page-break-inside: avoid; }}
  figure img {{ max-width: 85%; max-height: 210pt; }}
  {bilingual_css if bilingual else mono_css}
</style>
</head>
<body>
{body}
<script>{katex_js}</script>
<script>{autorender_js}</script>
<script>
  renderMathInElement(document.body, {{
    delimiters: [
      {{left: "$$", right: "$$", display: true}},
      {{left: "$", right: "$", display: false}}
    ],
    throwOnError: false,
    strict: false
  }});
</script>
</body>
</html>
"""


# --- PDF出力 ---------------------------------------------------------------


def render_all_pdfs(
    blocks: list[Block],
    output_dir: Path,
    log: Callable[[str], None] = print,
) -> list[Path]:
    """対訳版・英語版・日本語版の3種類のPDFを生成し、生成物のパス一覧を返す。"""
    output_dir = Path(output_dir)
    targets = [
        ("対訳版", "paper_bilingual.pdf", render_bilingual_html(blocks)),
        ("英語版", "paper_en.pdf", render_mono_html(blocks, "en")),
        ("日本語版", "paper_ja.pdf", render_mono_html(blocks, "ja")),
    ]

    paths: list[Path] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            for label, filename, html_content in targets:
                log(f"[PDF] {label}を生成中...")
                page.set_content(html_content, wait_until="load")
                page.emulate_media(media="print")
                pdf_path = output_dir / filename
                page.pdf(path=str(pdf_path), print_background=True, prefer_css_page_size=True)
                paths.append(pdf_path)
            page.close()
        finally:
            browser.close()
    return paths
