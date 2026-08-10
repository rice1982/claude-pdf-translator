"""PDF内でインライン数式をKaTeXにより実際の数式として描画するための、
オフライン埋め込み用アセット読み込みモジュール。

`vendor/katex/` にベンダリング済みのKaTeX本体・auto-render拡張・
woff2フォント一式を読み込み、フォントをbase64のdata URIへ変換した
自己完結なCSSを組み立てる。実行時にCDNへアクセスする必要がないため、
オフラインでもPDF生成が失敗しない。
"""

from __future__ import annotations

import base64
import re
from functools import lru_cache
from pathlib import Path

_VENDOR_DIR = Path(__file__).parent / "vendor" / "katex"
_FONTFACE_RE = re.compile(r"@font-face\{([^}]*)\}")
_SRC_WOFF2_RE = re.compile(r"url\(fonts/([\w.-]+\.woff2)\)")


def _inline_css(css: str, fonts_dir: Path) -> str:
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


@lru_cache(maxsize=1)
def load_katex_assets() -> tuple[str, str, str]:
    """(インライン化済みCSS, katex.min.jsの内容, auto-render.min.jsの内容) を返す。"""
    css = (_VENDOR_DIR / "katex.min.css").read_text(encoding="utf-8")
    inline_css = _inline_css(css, _VENDOR_DIR / "fonts")
    katex_js = (_VENDOR_DIR / "katex.min.js").read_text(encoding="utf-8")
    autorender_js = (_VENDOR_DIR / "auto-render.min.js").read_text(encoding="utf-8")
    return inline_css, katex_js, autorender_js
