"""インライン数式（`$...$` / `$$...$$`）を翻訳エンジンから保護するための
プレースホルダ置換モジュール。

DeepLは生のLaTeX断片を誤訳・破損させる可能性があるため、翻訳リクエスト前に
数式スパンを `__MATH0__` のような単純な
プレースホルダへ置き換え、翻訳結果に対してプレースホルダを元の数式へ
復元する。復元後のLaTeXはPDFレンダリング側（:mod:`pdf_renderer`）で
KaTeXにより実際の数式として描画される。

あわせて、MinerUが数式中に出力する `\\textless` / `\\textgreater`
（本来はテキストモード用の比較記号エスケープで、KaTeXは解釈できない）を
`<` / `>` へ正規化する。
"""

from __future__ import annotations

import re

_MATH_RE = re.compile(r"\$\$.*?\$\$|\$[^$\n]+\$", re.DOTALL)
_TOKEN_RE = re.compile(r"__MATH(\d+)__")


def _normalize(span: str) -> str:
    return span.replace(r"\textless", "<").replace(r"\textgreater", ">")


def protect(text: str) -> tuple[str, list[str]]:
    """本文中の数式スパンをプレースホルダへ置き換える。

    Returns:
        (置換後テキスト, 元の数式スパンのリスト（インデックス = プレースホルダ番号）)
    """
    spans: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        spans.append(_normalize(match.group(0)))
        return f"__MATH{len(spans) - 1}__"

    return _MATH_RE.sub(_replace, text), spans


def restore(text: str, spans: list[str]) -> str:
    """プレースホルダを元の数式スパンへ復元する。"""

    def _replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return spans[index] if index < len(spans) else match.group(0)

    return _TOKEN_RE.sub(_replace, text)


def normalize(text: str) -> str:
    """数式スパンだけを正規化し、それ以外の本文はそのまま返す。"""
    protected, spans = protect(text)
    return restore(protected, spans)
