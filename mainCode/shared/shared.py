"""パイプライン全体で共有するデータ構造、および全工程から参照される
末端ユーティリティ（数式プレースホルダ変換・ログ出力）。

データ構造の部分はロジックを持たず、各ステップの間で受け渡されるデータの
「形」だけを定義する。ステップ間の結合を、具体的な処理関数ではなくこれらの
型に対して行うことで、各ステップを独立に差し替え・テストできるようにする。

1ファイルに置くのは ``DocUnit``（工程(3)〜(7)を貫通する共有型）・数式保護の
退避/復元（``protect``/``restore``。インライン数式（``$...$``/``$$...$$``）を
プレースホルダ（``__MATHn__``）へ退避・復元する純粋な文字列変換で、工程(3)の
仕上げ・工程(5)・工程(6)の三者が互いをimportし合わずに使う）・``log``
（標準出力への進捗メッセージ、1行の極小関数）の3つ。いずれも他のどの
``mainCode`` モジュールにも依存しない末端のユーティリティで、ここに置くことで
複数の工程（例: :mod:`stage1` と :mod:`whole_pipeline`、:mod:`stage3` と
:mod:`stage6`）から循環importなしに共通して呼べる。PDF解析（工程(2)）専用の
中間表現（``TextBlockElement`` 等・``StructuredDocument``）や、PDF生成
（工程(7)）専用の表示単位（``Block``）のように、単一の工程内だけで完結し他工程
から一切参照されない型は、このファイルには置かずその工程のモジュール側
（:mod:`stage2`・:mod:`stage7`）で定義する。構成の詳細は
``doc/architecture/shared.md`` を参照。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ============================================================================
# DocUnit（工程(3)〜工程(7)を貫通する共有型）
# ============================================================================


# タグ付きMarkdownの1行から解析した最小単位。翻訳対象の文だけでなく、
# 画像参照・LaTeX・メタ情報など非翻訳要素も同じ型で表現し、元の行順を
# 保ったまま後続の翻訳・PDF生成ステップへ渡す。
@dataclass
class DocUnit:
    # 識別情報
    tag: str
    """タグ文字列（例: "P1-S1-1.introduction-S1"）。タグ付きMarkdown上の
    行を一意に識別し、翻訳結果の突き合わせやスナップショット保存時の
    キーとしても使う。"""
    kind: str
    """"title" | "authors" | "affil" | "heading" | "body_sentence"
    | "caption_sentence" | "equation_latex" | "figure_image"
    | "equation_image" | "unknown" """
    page: int
    """物理ページ番号（1始まり）。"""

    # 本文・成果物
    en_text: str = ""
    """原文（英語）。翻訳対象外のkind（figure_image等）では空文字列の
    ままか、LaTeXそのものが入る（kind次第）。"""
    ja_text: str = ""
    """訳文（日本語）。stage5.apply_restoreが書き込むまでは空文字列。"""
    image_rel_path: str | None = None
    """figure_image / equation_image の場合の画像相対パス（例: "images/fig_p2_1.png"）。"""

    # 翻訳パイプライン用の内部状態
    translatable: bool = False
    """翻訳エンジンへ送信する対象かどうか（kindに応じてstage3が設定する）。"""
    protected_en_text: str = ""
    """en_textの数式スパンをプレースホルダへ退避した状態（工程3の終わりに
    stage3.protect_unitsが設定する）。翻訳エンジンへ実際に送信する
    のはこちら。en_text自体は保護前の人間可読な形のまま変更しない。"""
    math_spans: list[str] = field(default_factory=list)
    """protected_en_textの__MATHn__プレースホルダを元の数式スパンへ復元する
    ためのリスト（stage3.protect_unitsが設定、shared.restore経由で
    stage5.apply_restoreが使用）。"""


# unitsのうち、translatable=Trueのものだけを列挙する（工程(4)〜(6)が
# 翻訳対象unitだけを走査する際の共通フィルタ）。
def filter_translatable_units(units: list[DocUnit]):
    return (u for u in units if u.translatable)


# ============================================================================
# 数式保護〔退避・復元。データ構造ではないが、工程(3)・工程(5)・工程(6)の
# 三者が互いをimportし合わずに使うための同居。上記docstring参照〕
# ============================================================================

MATH_RE = re.compile(r"\$\$.*?\$\$|\$[^$\n]+\$", re.DOTALL)
MATH_TOKEN_RE = re.compile(r"__MATH(\d+)__")


# MinerUが数式中に出力する\textless/\textgreater（テキストモード用の
# 比較記号エスケープで、KaTeXは解釈できない）を</>へ正規化する。
# 対応するのはこの2コマンドのみ。同種のテキストモード用エスケープ
# コマンド（例: `|`に対応する\textbar）が数式中に出力された場合は
# 対応表に無いため変換されず、そのままKaTeXへ渡って解釈できない
# 状態になる。
def _normalize_math_escape(span: str) -> str:
    return span.replace(r"\textless", "<").replace(r"\textgreater", ">")


# 本文中の数式スパン（$...$/$$...$$）をプレースホルダ（__MATHn__）へ置き換える。
def protect(text: str) -> tuple[str, list[str]]:
    spans: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        spans.append(_normalize_math_escape(match.group(0)))
        # spansへの追加直後のインデックス（=末尾の要素番号）がそのまま
        # プレースホルダ番号になる。
        return f"__MATH{len(spans) - 1}__"

    return MATH_RE.sub(_replace, text), spans


# プレースホルダを元の数式スパンへ復元する。
def restore(text: str, spans: list[str]) -> str:
    def _replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        # indexがspansの範囲外の場合はプレースホルダ文字列をそのまま残す
        # （翻訳エンジンがプレースホルダ番号を破損させた場合の安全策。
        # test_stage5.pyのTestApplyRestore参照）。
        return spans[index] if index < len(spans) else match.group(0)

    return MATH_TOKEN_RE.sub(_replace, text)


# ============================================================================
# ログ出力（データ構造ではないが、循環importなしに全工程から共有するための
# 同居。上記docstring参照）
# ============================================================================


# 標準出力へ進捗メッセージを1行出力する（print(..., flush=True)の薄い
# ラッパー）。print文の副作用は通常テスト対象としないため、ログ出力
# そのものを検証する専用テストは無い。
def log(message: str) -> None:
    print(message, flush=True)
