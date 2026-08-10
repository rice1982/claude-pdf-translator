"""パイプライン全体で共有するデータ構造。

このモジュールはロジックを持たず、各ステップ（MinerU実行 → 構造解析 →
翻訳 → 成果物結合）の間で受け渡されるデータの「形」だけを定義する。
ステップ間の結合を、具体的な処理関数ではなくこれらの型に対して行うことで、
各ステップを独立に差し替え・テストできるようにする。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TextBlockElement:
    """本文の段落（複数文に分割済み）。翻訳対象の本文テキスト。"""

    sentences: list[str]
    sentence_ids: list[str] = field(default_factory=list)
    """各文のID（例: "P1-S3-1.introduction-S2"）。翻訳ステップではこのIDを
    TranslationUnit.unit_id としてそのまま使う。"""
    kind: str = "text_block"


@dataclass
class FigureElement:
    """図・表。元論文のFig./Table番号はキャプションから読み取る。翻訳対象外
    （画像そのものは翻訳できない。テキストはCaptionElement側が担当する）。"""

    image_path: Path
    fig_kind: str = "figure"  # "figure" | "table"
    number: int | None = None
    labeled: bool = True
    """Falseの場合、numberはキャプションから読み取れなかったためのフォールバック連番。"""
    kind: str = "figure"


@dataclass
class EquationElement:
    """数式。MinerUが生成した構文的に正しいLaTeXテキストをそのまま保持する。
    翻訳対象外（LaTeXは翻訳しない）。"""

    image_path: Path
    latex: str
    number: int | None = None
    labeled: bool = True
    kind: str = "equation"


@dataclass
class LabeledElement:
    """タイトル・著者名・所属など、文としてナンバリングしない前付け要素。

    著者名・所属は固有名詞であり翻訳対象に含めない。タイトルも同様に、
    デフォルトでは翻訳対象外として扱う（将来的に翻訳したい場合は
    unit_idを付与する形で拡張できる）。
    """

    text: str
    label: str  # "TITLE" | "AUTHORS" | "AFFIL"
    kind: str = "labeled"


@dataclass
class HeadingElement:
    """章・節見出し。本文の文カウントには含めず、単体のラベルとして扱う。
    翻訳対象外（章番号の構造を崩さないよう、デフォルトでは原文のまま出力する）。
    """

    text: str
    section_num: str  # 例: "1", "2.1"
    section_name: str  # 例: "introduction", "preliminaries"
    kind: str = "heading"

    @property
    def section_id(self) -> str:
        return f"{self.section_num}.{self.section_name}"


@dataclass
class CaptionElement:
    """図表のキャプション。本文の文カウント（章・節ラベル）には含めず、
    キャプション文自体から読み取った元論文のFig./Table番号に紐づけて
    文単位でナンバリングする。翻訳対象の本文テキスト。"""

    sentences: list[str]
    number: int
    fig_kind: str = "figure"  # "figure" | "table"
    sentence_ids: list[str] = field(default_factory=list)
    kind: str = "caption"


@dataclass
class UnknownElement:
    """構造解析ステップが解釈できなかった要素のフォールバック表現。

    未知の要素種別（例: 疑似コードブロック）や、既知の種別でも解析中に
    例外が発生した項目は、無理に構造化しようとせずこの型に落とし込み、
    取得できた生のテキストをそのまま安全に出力する。これにより1要素の
    解釈失敗がドキュメント全体の処理停止につながらないようにする。
    翻訳対象外（構造が不明なため安全側に倒す）。
    """

    raw_type: str
    """MinerUが付与した元の要素種別（例: "code"）。"""
    text: str
    """フォールバック時に出力する生テキスト。取得できなければ空文字列。"""
    reason: str = ""
    """フォールバックに至った理由（デバッグ用。例外メッセージ等）。"""
    kind: str = "unknown"


PageElement = (
    "TextBlockElement | FigureElement | EquationElement | LabeledElement "
    "| HeadingElement | CaptionElement | UnknownElement"
)


@dataclass
class PageContent:
    """1ページ分の要素列（読み順）。"""

    page_number: int
    elements: list = field(default_factory=list)


@dataclass
class TranslationUnit:
    """翻訳対象となる最小単位（1文）。

    翻訳ステップはunit_idとtextだけを見ればよく、元の要素がTextBlockElement
    なのかCaptionElementなのかを意識する必要はない。unit_idには、その文の
    最終的な表示ID（例: "P2-S6-2.1.preliminaries-S1"）をそのまま使うため、
    翻訳結果を成果物結合ステップで元の位置へ一意に戻すことができる。
    """

    unit_id: str
    text: str


@dataclass
class StructuredDocument:
    """構造解析ステップ（Step 2）の出力。翻訳ステップ（Step 3）はpagesの
    構造を一切知らず、translation_unitsだけを入力として受け取ればよい。"""

    pages: list[PageContent]
    translation_units: list[TranslationUnit] = field(default_factory=list)
