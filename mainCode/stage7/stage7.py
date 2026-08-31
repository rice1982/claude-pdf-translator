"""工程(7)「PDF生成」モジュール。

翻訳済みの :class:`shared.DocUnit` 列を表示単位（見出し・段落・図表）に
まとめ直し、対訳版・英語版・日本語版の3種類のHTMLを組み立てて Playwright
（Chromium）でPDF化する（``build_blocks``→``render_all_pdfs``、工程(7)全体の
入口は ``render_units_to_pdfs``）。対訳版は文ペアをTable Rowで結合し
``break-inside: avoid`` で左右の高さズレと文中でのページ跨ぎを防ぎ、日本語の
文字化け対策にNoto Sans JP系フォントスタックを指定する。インライン数式は
ブラウザ上のKaTeXで描画するため、``vendor/katex/`` のKaTeX一式をフォントごと
base64のdata URIへインライン化して埋め込み（``load_katex_assets``）、実行時に
CDNへアクセスせずオフラインで完結させる。他のどの ``mainCode`` モジュールにも
依存しない。構成の詳細は ``doc/architecture/stage7.md`` を参照。
"""

from __future__ import annotations

import base64
import html
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from playwright.sync_api import sync_playwright

from mainCode.shared.shared import DocUnit
from mainCode.shared.shared import log as _log


# ============================================================================
# Block型
# ============================================================================


# PDFレンダリング用に文をまとめた表示単位（段落・見出し・図表など）。
#
# 工程(7)内だけで完結する表示単位のため、mainCode/shared/shared.py
# ではなくここに置く。
@dataclass
class Block:
    kind: str  # "title" | "meta" | "heading" | "paragraph" | "figure" | "equation"
    level: int = 2
    """heading の見出しレベル（h2〜h4）。"""
    role: str = ""
    """meta の場合の種別（"authors" | "affil"）。"""
    sentences: list[DocUnit] = field(default_factory=list)
    image_data_uri: str | None = None


# ============================================================================
# 本体共通
# ============================================================================


# タグ末尾の見出し番号部分（P{page}-HEADING- 以降）を取り出す。
_HEADING_SUFFIX_RE = re.compile(r"^P\d+-HEADING-(.+)$")


# タグ末尾の見出し番号（例: P3-HEADING-2.1.4）から章番号階層の深さを数え、
# HTML見出しレベル（h2〜h4）へ対応付ける。非数字パートが現れた時点で打ち切る。
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


# タグ末尾の -S{n} を除いた部分（同一キャプション段落の識別子）を取り出す。
_CAPTION_KEY_RE = re.compile(r"^(.*)-S\d+$")
# 本文タグ（P{page}-S{文}-{段落識別子}-S{文} 形式）から P{page} と段落識別子を取り出す。
_BODY_TAG_RE = re.compile(r"^(P\d+)-S\d+-(.+)-S\d+$")


# unitのタグから「同じ段落の文をまとめるキー」を取り出す。caption_sentence は
# 末尾 -S{n} を除いた部分、本文は P{page}-{段落識別子}、いずれのパターンにも
# 合わなければタグそのもの。
def _paragraph_key(unit: DocUnit) -> str:
    if unit.kind == "caption_sentence":
        m = _CAPTION_KEY_RE.match(unit.tag)
        return m.group(1) if m else unit.tag
    m = _BODY_TAG_RE.match(unit.tag)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return unit.tag


# output_dir 基準の相対パスにあるPNG画像を読み、data:image/png;base64,... 形式の
# data URIへ変換する（HTMLへ外部ファイル参照なしで画像を埋め込むため）。
def _image_data_uri(output_dir: Path, rel_path: str) -> str:
    data = (output_dir / rel_path).read_bytes()
    return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"


# DocUnit列をPDFレンダリング用のBlock列へまとめる。
# 大半のkindは1 unit = 1 Blockだが、本文・キャプションの文（body_sentence／
# caption_sentence）だけは _paragraph_key が同じ連続範囲を1つの paragraph Block
# へまとめる（キーは実質「ページ×章」／「ページ×図表」単位。詳細は
# doc/architecture/stage7.md の _paragraph_key の項）。
def build_blocks(units: list[DocUnit], output_dir: Path) -> list[Block]:
    blocks: list[Block] = []          # 確定済みBlockの出力列（戻り値）
    current: list[DocUnit] = []       # 組み立て中の段落のバッファ（溜めている文）
    current_key: str | None = None    # そのバッファが属する段落キー（_paragraph_keyの値）

    # バッファ（current）に溜めた本文の文があれば1つの paragraph Block として確定し、
    # バッファ（current）と段落キー（current_key）を空へ戻す。段落の切れ目
    # （別種の要素・別段落の文・ループ末尾）ごとに呼ぶ。バッファが空なら何もしない。
    def flush() -> None:
        nonlocal current, current_key
        if current:
            blocks.append(Block(kind="paragraph", sentences=current))
        current = []
        current_key = None

    for unit in units:
        # --- 単独で1つのBlockになる種別 ---
        # いずれも組み立て中の段落を先に flush() で確定してから追加する
        # （そうしないとBlockの並びが本文順とずれる）。
        if unit.kind == "title":
            flush()
            blocks.append(Block(kind="title", sentences=[unit]))
        elif unit.kind in ("authors", "affil"):
            # 著者・所属はまとめて meta（対訳せず英語表記のまま出す）
            flush()
            blocks.append(Block(kind="meta", role=unit.kind, sentences=[unit]))
        elif unit.kind == "heading":
            # level = 見出し番号の階層の深さ（h2〜h4）
            flush()
            blocks.append(Block(kind="heading", level=_heading_level(unit.tag), sentences=[unit]))
        elif unit.kind == "figure_image":
            flush()
            # 相対パスのPNGを data URI に変換して埋め込む（画像パスが無ければNone）
            image_uri = _image_data_uri(output_dir, unit.image_rel_path) if unit.image_rel_path else None
            blocks.append(Block(kind="figure", sentences=[unit], image_data_uri=image_uri))
        elif unit.kind == "equation_latex":
            flush()
            blocks.append(Block(kind="equation", sentences=[unit]))

        # --- 段落へ溜め込む種別 ---
        elif unit.kind in ("body_sentence", "caption_sentence"):
            # キーが変わった＝別の段落（章・ページ・図表が変わった）に入ったので、
            # 手前の段落を確定してから新しいキーでバッファに溜め直す。同じキーなら溜めるだけ。
            key = _paragraph_key(unit)
            if key != current_key:
                flush()
                current_key = key
            current.append(unit)

        # --- Blockを作らない種別 ---
        elif unit.kind == "equation_image":
            # MinerUの画像切り出し範囲には \tag{n} の式番号が含まれないため、
            # 番号を含む正しいLaTeX（equation_latex）側でKaTeX描画する。
            continue

        # --- 想定外のkind（unknown） ---
        else:
            # 1文だけの paragraph Block として拾っておく（描画から取りこぼさない）。
            flush()
            blocks.append(Block(kind="paragraph", sentences=[unit]))

    flush()  # ループ終了時、最後に溜まっている段落を確定する
    return blocks


# html.escape の短縮エイリアス。対訳版・単一言語版の両レンダラが
# HTML本文へ値を差し込む際に使う。
def _esc(text: str) -> str:
    return html.escape(text)


# ============================================================================
# KaTeXアセット: オフライン埋め込み用アセットの読み込み
# ============================================================================

# vendor/katex/（KaTeX本体・auto-render拡張・woff2フォント一式のベンダリング先）。
_VENDOR_DIR = Path(__file__).parent.parent.parent / "vendor" / "katex"
# KaTeX CSS中の各 @font-face{...} ブロックにマッチする。
_FONTFACE_RE = re.compile(r"@font-face\{([^}]*)\}")
# @font-face ブロック内の url(fonts/xxx.woff2) 参照にマッチする。
_SRC_WOFF2_RE = re.compile(r"url\(fonts/([\w.-]+\.woff2)\)")


# KaTeXのCSS中の各 @font-face ブロックの url(fonts/xxx.woff2) 参照を、
# fonts_dir から読んだwoff2ファイルのbase64 data URIへ置き換える
# （オフラインでフォントを埋め込むため）。
def _inline_css(css: str, fonts_dir: Path) -> str:
    # 1つの @font-face ブロック（正規表現マッチ）を受け取り、内部のwoff2参照を
    # data URIへ差し替えた @font-face{...} 文字列を返す。参照が無ければ元のまま返す。
    def _inline_block(match: re.Match[str]) -> str:
        block = match.group(1)
        font_match = _SRC_WOFF2_RE.search(block)
        if not font_match:
            return match.group(0)
        font_bytes = (fonts_dir / font_match.group(1)).read_bytes()
        data_uri = f"data:font/woff2;base64,{base64.b64encode(font_bytes).decode('ascii')}"
        new_block = re.sub(r"src:.*", f'src:url({data_uri}) format("woff2")', block)
        return "@font-face{" + new_block + "}"

    return _FONTFACE_RE.sub(_inline_block, css)


# vendor/katex/ のKaTeX一式（CSS・本体JS・auto-render拡張JS）を読み込み、
# CSS内のwoff2フォント参照を_inline_cssでdata URIへ埋め込んだうえで
# (インライン化済みCSS, katex.min.jsの内容, auto-render.min.jsの内容) を返す。
# @lru_cacheによりプロセス内で実質1回だけディスクI/Oを行う。
@lru_cache(maxsize=1)
def load_katex_assets() -> tuple[str, str, str]:
    css = (_VENDOR_DIR / "katex.min.css").read_text(encoding="utf-8")
    inline_css = _inline_css(css, _VENDOR_DIR / "fonts")
    katex_js = (_VENDOR_DIR / "katex.min.js").read_text(encoding="utf-8")
    autorender_js = (_VENDOR_DIR / "auto-render.min.js").read_text(encoding="utf-8")
    return inline_css, katex_js, autorender_js


# ============================================================================
# HTML/CSSテンプレート
# ============================================================================

# 日本語の文字化け対策。Noto Sans JP系を先頭に置いた font-family スタック。
_FONT_STACK = "'Noto Sans JP', 'Yu Gothic UI', 'Yu Gothic', Meiryo, 'Hiragino Sans', sans-serif"


# body（レンダリング済みHTML断片の連結）を、@page・フォントスタック・
# KaTeX CSS/JS・対訳版／単一言語版それぞれのCSSを含む完全なHTML文書で包む。
# KaTeXアセットは load_katex_assets から取得する。
def _wrap_html(title: str, body: str, bilingual: bool, lang: str = "ja") -> str:
    bilingual_css = """
        table.bilingual { width: 100%; border-collapse: collapse; table-layout: fixed; margin: 4pt 0; }
        table.bilingual td { width: 50%; vertical-align: top; padding: 4pt 8pt; font-size: 10pt; }
        table.bilingual td.en { border-right: 1px solid #ddd; }
        table.bilingual tr { break-inside: avoid; page-break-inside: avoid; }
        .equation .katex-display > .katex > .katex-html > .tag {
            position: static;
            margin-left: 0.5em;
        }
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
  .equation {{ text-align: center; margin: 10pt 0; break-inside: avoid; page-break-inside: avoid; }}
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


# ============================================================================
# 対訳版HTML
# ============================================================================


# 対訳テーブルの1行（<tr><td class="en">…</td><td class="ja">…</td></tr>）を
# 組み立てる。row_class があれば <tr> に付与する。
def _bilingual_row(en_html: str, ja_html: str, row_class: str = "") -> str:
    cls = f' class="{row_class}"' if row_class else ""
    return f'<tr{cls}><td class="en">{en_html}</td><td class="ja">{ja_html}</td></tr>'


# 1つの Block を対訳版HTMLの断片へ変換する。block.kind
# （title/meta/heading/paragraph/figure/equation）ごとにテーブル・<div>・<figure>
# を出し分ける。未知の kind は空文字列。
def _render_bilingual_block(block: Block) -> str:
    # title: 英・日を1行の表にして両方を太字。title-block クラスで中央寄せ・大きめ表示（CSSは_wrap_html）。
    if block.kind == "title":
        unit = block.sentences[0]  # タイトルは1文のみ
        return (
            '<table class="bilingual title-block">'
            + _bilingual_row(f"<strong>{_esc(unit.en_text)}</strong>", f"<strong>{_esc(unit.ja_text)}</strong>")
            + "</table>"
        )
    # meta: 著者・所属。対訳せず英語表記のみを1カラムの <div> で出す（表にしない）。
    if block.kind == "meta":
        unit = block.sentences[0]
        return f'<div class="meta">{_esc(unit.en_text)}</div>'
    # heading: 英・日を1行の表に。level-{2..4} クラスで見出しの大きさをCSS側で切り替える。
    if block.kind == "heading":
        unit = block.sentences[0]  # 見出しは1文のみ
        return (
            f'<table class="bilingual heading-block level-{block.level}">'
            + _bilingual_row(_esc(unit.en_text), _esc(unit.ja_text))
            + "</table>"
        )
    # paragraph: 段落内の各文を「英｜日」の表の1行ずつにし、1つの表にまとめる。
    # 左右で対応する文が必ず隣り合い、CSSの break-inside:avoid が行単位のページ跨ぎを防ぐ。
    if block.kind == "paragraph":
        rows = "".join(_bilingual_row(_esc(u.en_text), _esc(u.ja_text)) for u in block.sentences)
        return f'<table class="bilingual">{rows}</table>'
    # figure: data URI の画像を <figure><img> で1カラム表示。alt にはタグ文字列を入れる。
    if block.kind == "figure":
        parts = [f'<img src="{block.image_data_uri}" alt="{_esc(block.sentences[0].tag)}">']
        return f'<figure>{"".join(parts)}</figure>'
    # equation: LaTeX 文字列（en_text）を <div class="equation"> に入れる。実際の数式描画は
    # ブラウザ上で KaTeX が行う。数式は言語非依存なので対訳の左右には分けない。
    if block.kind == "equation":
        unit = block.sentences[0]
        return f'<div class="equation">{_esc(unit.en_text)}</div>'
    return ""  # 未知の kind は何も出力しない（呼び出し元で空文字は捨てられる）


# Block 列全体を _render_bilingual_block で断片化して連結し、_wrap_html で
# 対訳版の完全なHTML文書に仕上げる。
def render_bilingual_html(blocks: list[Block]) -> str:
    body_parts: list[str] = []
    for block in blocks:
        rendered = _render_bilingual_block(block)
        if rendered:
            body_parts.append(rendered)
        # 図の直後に続くキャプション段落は figure ブロックとして分離されないため、
        # 通常の paragraph ブロックとしてそのまま対訳テーブルで出力される。
    return _wrap_html("対訳版", "".join(body_parts), bilingual=True)


# ============================================================================
# 単一言語版HTML
# ============================================================================


# lang（"en"/"ja"）に応じて unit.en_text または unit.ja_text を返す。
# 単一言語版レンダラ用。
def _sentence_text(unit: DocUnit, lang: str) -> str:
    return unit.en_text if lang == "en" else unit.ja_text


# 1つの Block を単一言語版（英語のみ／日本語のみ）HTMLの断片へ変換する。
# block.kind ごとに <h1>/<h{level}>/<p>/<figure>/<div class="equation"> を
# 出し分ける。meta は常に英語表記。未知の kind は空文字列。
def _render_mono_block(block: Block, lang: str) -> str:
    # title: 選択言語のタイトルを <h1> に（対訳版と違い表は使わない）。
    if block.kind == "title":
        unit = block.sentences[0]  # タイトルは1文のみ
        return f"<h1>{_esc(_sentence_text(unit, lang))}</h1>"
    # meta: 著者・所属。lang に関わらず常に英語表記（日本語版でも英語のまま）。
    if block.kind == "meta":
        unit = block.sentences[0]
        return f'<div class="meta">{_esc(unit.en_text)}</div>'
    # heading: block.level（2〜4）に応じて <h2>〜<h4>。本文は選択言語。
    if block.kind == "heading":
        unit = block.sentences[0]  # 見出しは1文のみ
        return f"<h{block.level}>{_esc(_sentence_text(unit, lang))}</h{block.level}>"
    # paragraph: 段落内の各文を1つずつ <p> にして連結する（1文＝1<p>。対訳版の表とは違う）。
    if block.kind == "paragraph":
        return "".join(f"<p>{_esc(_sentence_text(u, lang))}</p>" for u in block.sentences)
    # figure: data URI の画像を <figure><img> で表示（対訳版と同じ）。
    if block.kind == "figure":
        return f'<figure><img src="{block.image_data_uri}" alt="{_esc(block.sentences[0].tag)}"></figure>'
    # equation: LaTeX 文字列を <div class="equation"> に入れる（描画はブラウザ上の KaTeX）。
    if block.kind == "equation":
        unit = block.sentences[0]
        return f'<div class="equation">{_esc(_sentence_text(unit, lang))}</div>'
    return ""  # 未知の kind は何も出力しない（呼び出し元で空文字は捨てられる）


# Block 列全体を _render_mono_block で断片化して連結し、_wrap_html で
# 英語版または日本語版の完全なHTML文書に仕上げる。
def render_mono_html(blocks: list[Block], lang: str) -> str:
    title = "英語版" if lang == "en" else "日本語版"
    body = "".join(_render_mono_block(block, lang) for block in blocks)
    return _wrap_html(title, body, bilingual=False, lang="en" if lang == "en" else "ja")


# ============================================================================
# PDF出力
# ============================================================================


# 対訳版・英語版・日本語版の3種類のPDFを生成し、生成物のパス一覧を返す。
#
# PDF化は「HTMLをブラウザで開いて『PDFに印刷』する」方式で行う。
# Playwright は、ブラウザ（ここでは Chromium＝Google Chrome のオープン
# ソース版）を人手ではなくプログラムから操作するためのライブラリで、
# 画面を出さないヘッドレスモードで別プロセスとして起動する。ブラウザは
# HTML/CSS のレイアウト計算・改ページ・フォント描画・「印刷用PDF出力」を
# すべて備えているため、そこに HTML 文字列を渡すだけで、自前でページ分割
# やフォント処理を書かずに高品質なPDFへ変換できる。KaTeX による数式描画も
# ブラウザ上で JavaScript を実行させることで実現している（_wrap_html が
# 生成する HTML 末尾のスクリプト参照）。
#
# Chromium 本体（数百MB）はライブラリとは別で、初回のみ
# `python -m playwright install chromium` での導入が必要
# （README「インストール手順」参照）。
def render_all_pdfs(
    blocks: list[Block],
    output_dir: Path,
    log: Callable[[str], None] = print,
) -> list[Path]:
    output_dir = Path(output_dir)  # str で渡されても Path に正規化する
    # (画面表示ラベル, 出力ファイル名, レンダリング済みHTML文字列) の3組。
    # HTML はここで先に組み立てる（Playwright 起動前に確定させる）。
    targets = [
        ("対訳版", "paper_bilingual.pdf", render_bilingual_html(blocks)),
        ("英語版", "paper_en.pdf", render_mono_html(blocks, "en")),
        ("日本語版", "paper_ja.pdf", render_mono_html(blocks, "ja")),
    ]

    paths: list[Path] = []
    # Playwright（Chromium）を1回だけ起動し、1枚のページを3PDFで使い回す。
    with sync_playwright() as p:
        browser = p.chromium.launch()  # ヘッドレス Chromium
        try:
            page = browser.new_page()
            for label, filename, html_content in targets:
                log(f"[PDF] {label}を生成中...")
                # wait_until="load": インライン化した KaTeX スクリプトが走り、
                # 数式描画が終わるまで待ってから PDF 化する。
                page.set_content(html_content, wait_until="load")
                # print メディアをエミュレートして CSS の @page ルールを有効化する。
                page.emulate_media(media="print")
                pdf_path = output_dir / filename
                # print_background: CSS の背景色・罫線を出力に含める。
                # prefer_css_page_size: @page の size:A4 を用紙サイズとして使う。
                page.pdf(path=str(pdf_path), print_background=True, prefer_css_page_size=True)
                paths.append(pdf_path)
            page.close()
        finally:
            browser.close()  # 例外時も含め、ブラウザプロセスを必ず終了する
    return paths


# ============================================================================
# 入口
# ============================================================================


# 工程7「PDF生成」の統括関数。build_blocks（DocUnit列→表示単位への
# グルーピング）とrender_all_pdfs（Playwright経由のPDF化）を、この順で
# まとめて実行する。戻り値はrender_all_pdfsが生成したPDFファイルパスの
# 一覧（paper_bilingual.pdf / paper_en.pdf / paper_ja.pdf）。
#
# 翻訳・PDF生成の一連の流れの中で間を置かず連続して呼ばれていた2つの
# 関数をまとめただけであり、build_blocks/render_all_pdfsはそれぞれ
# 引き続き個別に呼び出し・単体テストできる。特にbuild_blocksは
# Playwrightを必要としない軽量な純粋関数のため、グルーピングロジック
# だけを高速に検証したい場合は個別呼び出しの方を使うこと。
def render_units_to_pdfs(
    units: list[DocUnit], output_dir: str | Path, log: Callable[[str], None] = _log
) -> list[Path]:
    # 1. DocUnit列を表示単位（見出し・段落・図表）へグルーピングする
    #    （Playwright不使用の軽量な純粋関数）。
    blocks = build_blocks(units, output_dir)
    # 2. グルーピング結果から対訳版・英語版・日本語版の3PDFをレンダリングする
    #    （Playwrightを起動する重い処理はここに閉じ込められている）。
    return render_all_pdfs(blocks, output_dir, log=log)
