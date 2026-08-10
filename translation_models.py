"""翻訳・PDF出力パイプラインで共有するデータ構造。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DocUnit:
    """タグ付きMarkdownの1行から解析した最小単位。

    翻訳対象の文だけでなく、画像参照・LaTeX・メタ情報など非翻訳要素も
    同じ型で表現し、元の行順を保ったまま後続の翻訳・PDF生成ステップへ渡す。
    """

    tag: str
    kind: str
    """"title" | "authors" | "affil" | "heading" | "body_sentence"
    | "caption_sentence" | "equation_latex" | "figure_image"
    | "equation_image" | "unknown" """
    page: int
    en_text: str = ""
    ja_text: str = ""
    image_rel_path: str | None = None
    """figure_image / equation_image の場合の画像相対パス（例: "images/fig_p2_1.png"）。"""
    translatable: bool = False


@dataclass
class Block:
    """PDFレンダリング用に文をまとめた表示単位（段落・見出し・図表など）。"""

    kind: str  # "title" | "meta" | "heading" | "paragraph" | "figure"
    level: int = 2
    """heading の見出しレベル（h2〜h4）。"""
    role: str = ""
    """meta の場合の種別（"authors" | "affil"）。"""
    sentences: list[DocUnit] = field(default_factory=list)
    image_data_uri: str | None = None
