"""工程(2)「PDF解析」モジュール。

MinerU実行という唯一の重い外部処理を含むため、性質の異なる複数種類のコード
（末端の文字列ユーティリティ・MinerU実行本体・実行結果キャッシュ・構造解析・
成果物結合・入口オーケストレーター）を1ファイルにまとめている。責務は
「翻訳対象の本文テキスト」と「非翻訳要素（数式・図表・その他）」を分離し、
それぞれに独自ID体系（例: ``[P1-S1-abstract-S1]``）を振ってページ別タグ付き
Markdownとして書き出すことに限定し、実際の翻訳（工程(4)）はここでは行わない
（タグ付きMarkdown生成後に文書全体を見てからまとめて行う。DeepLの文脈
パラメータを活用するための意図的な分離）。要素の判定はMinerUが付与する
``type`` フィールドによる汎用的な分岐のみで行い、想定外の ``type`` や解析中の
例外は :class:`UnknownElement` にフォールバックさせて全体を止めない。一方
MinerUのプロセス異常終了や出力欠落は、後続へ渡す生データが無い以上
フォールバックせず``MinerURunError``で即座に呼び出し元へ伝える。キャッシュ
（``load_cached_items``/``save_cache``等）はテスト・開発ループの高速化のみを
目的とし、読み書きの失敗は常に通常実行へ静かにフォールバックする。CLI
エントリポイント（``python mainCode/stage2/stage2.py <pdf> <output_dir>``）も
提供する。構成の詳細は ``doc/architecture/stage2.md`` を参照。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF（総ページ数の取得のみに使用）
from PIL import Image
from spellchecker import SpellChecker

logger = logging.getLogger(__name__)


# ============================================================================
# PDF解析側の中間表現（MinerU実行→構造解析の間だけで使う型。工程(2)内で
# 原文Markdownへ書き出された時点で役目を終え、他工程からは参照されない
# ため :mod:`mainCode.shared.shared` ではなくここに置く）
# ============================================================================


# 本文の段落（複数文に分割済み）。翻訳対象の本文テキスト。
@dataclass
class TextBlockElement:
    sentences: list[str]
    sentence_ids: list[str] = field(default_factory=list)
    """各文のID（例: "P1-S3-1.introduction-S2"）。"""
    kind: str = "text_block"


# 図・表。元論文のFig./Table番号はキャプションから読み取る。翻訳対象外
# （画像そのものは翻訳できない。テキストはCaptionElement側が担当する）。
@dataclass
class FigureElement:
    image_path: Path
    fig_kind: str = "figure"  # "figure" | "table"
    number: int | None = None
    labeled: bool = True
    """Falseの場合、numberはキャプションから読み取れなかったためのフォールバック連番。"""
    kind: str = "figure"


# 数式。MinerUが生成した構文的に正しいLaTeXテキストをそのまま保持する。
# 翻訳対象外（LaTeXは翻訳しない）。
@dataclass
class EquationElement:
    latex: str
    image_path: Path | None = None
    """MinerUが数式を切り出した画像へのパス。バックエンドによっては
    （例: vlm-engine）数式が画像として切り出されず、LaTeXテキストのみが
    得られる場合があり、その際はNoneになる。最終PDFの数式描画は常にlatex側
    （KaTeX）で行うため、Noneでも表示上の欠落は起きない
    （:mod:`stage2` 参照）。"""
    number: int | None = None
    labeled: bool = True
    kind: str = "equation"


# タイトル・著者名・所属など、文としてナンバリングしない前付け要素。
# 著者名・所属は固有名詞であり翻訳対象に含めない。タイトルも同様に、
# デフォルトでは翻訳対象外として扱う（将来的に翻訳したい場合は
# unit_idを付与する形で拡張できる）。
@dataclass
class LabeledElement:
    text: str
    label: str  # "TITLE" | "AUTHORS" | "AFFIL"
    kind: str = "labeled"


# 章・節見出し。本文の文カウントには含めず、単体のラベルとして扱う。
# 翻訳対象外（章番号の構造を崩さないよう、デフォルトでは原文のまま出力する）。
@dataclass
class HeadingElement:
    text: str
    section_num: str  # 例: "1", "2.1"
    section_name: str  # 例: "introduction", "preliminaries"
    kind: str = "heading"

    @property
    def section_id(self) -> str:
        return f"{self.section_num}.{self.section_name}"


# 図表のキャプション。本文の文カウント（章・節ラベル）には含めず、
# キャプション文自体から読み取った元論文のFig./Table番号に紐づけて
# 文単位でナンバリングする。翻訳対象の本文テキスト。
@dataclass
class CaptionElement:
    sentences: list[str]
    number: int
    fig_kind: str = "figure"  # "figure" | "table"
    sentence_ids: list[str] = field(default_factory=list)
    kind: str = "caption"


# 構造解析ステップが解釈できなかった要素のフォールバック表現。
# 未知の要素種別（例: 疑似コードブロック）や、既知の種別でも解析中に
# 例外が発生した項目は、無理に構造化しようとせずこの型に落とし込み、
# 取得できた生のテキストをそのまま安全に出力する。これにより1要素の
# 解釈失敗がドキュメント全体の処理停止につながらないようにする。
# 翻訳対象外（構造が不明なため安全側に倒す）。
@dataclass
class UnknownElement:
    raw_type: str
    """MinerUが付与した元の要素種別（例: "code"）。"""
    text: str
    """フォールバック時に出力する生テキスト。取得できなければ空文字列。"""
    reason: str = ""
    """フォールバックに至った理由（デバッグ用。例外メッセージ等）。"""
    kind: str = "unknown"


# 1ページ分の要素列（読み順）。
@dataclass
class PageContent:
    page_number: int
    elements: list = field(default_factory=list)


# 構造解析（analyze_structure）の出力。ページ別の要素列を保持する。
@dataclass
class StructuredDocument:
    pages: list[PageContent]

SUPPORTED_MINERU_BACKENDS = ("pipeline", "vlm-engine")
"""本ツールがサポートするMinerUバックエンド。

MinerU自体はこの他にも複数のバックエンド（hybrid-engine等）を持つが、
このプロジェクトでは「軽量・高速だが数式等の認識精度が相対的に低い
既定のpipeline」と「CPUのみの環境でも動くが低速な代わりに認識精度が
高いvlm-engine」の2択のみをサポート対象とする。
"""

_DEFAULT_BACKEND = "pipeline"


# ============================================================================
# テキストユーティリティ（末端モジュール。他のどのパイプラインモジュールにも
# 依存しない、純粋な文字列変換関数群）
# ============================================================================

_SPELL = SpellChecker()

# --- 辞書判定 ------------------------------------------------------------------


# wordが英単語として辞書に存在するかどうかを返す。
# 注意: pyspellcheckerはアルファベット1文字を常に「既知」と判定する
# （編集距離1以内の候補が必ず見つかるため）。1文字の実在単語判定には
# 使わないこと。
def is_known_word(word: str) -> bool:
    return bool(word) and word.lower() in _SPELL


# --- 数式スパンを避けた変換の共通ヘルパー ---------------------------------------
#
# 以下の複数の変換（"1文字の変数 = 値"の保護、ギリシャ文字の保護、結合語の
# ハイフン復元）はいずれも、既に$...$/$$...$$で保護済みの数式スパンには手を
# 触れず、それ以外の地の文にだけ正規表現置換を適用するという同じ構造を持つ。

_INLINE_MATH_RE = re.compile(r"\$[^$]*\$")


# text中の$...$で囲まれた数式スパンを除いた部分にのみpattern.sub(repl, ...)
# を適用する。数式スパン自体は変更しない。
def _apply_outside_math_spans(text: str, pattern: re.Pattern[str], repl) -> str:
    parts: list[str] = []
    last = 0
    for math_match in _INLINE_MATH_RE.finditer(text):
        parts.append(pattern.sub(repl, text[last:math_match.start()]))
        parts.append(math_match.group(0))
        last = math_match.end()
    parts.append(pattern.sub(repl, text[last:]))
    return "".join(parts)


# --- 単体アルファベット=値 の数式保護 --------------------------------------
#
# MinerUの数式検出は完全ではなく、"t = 1"のように地の文に単独で出現する
# 「1文字の変数 = 値」という数式的表現が$...$で囲まれないことがある
# （実データ: sample0.pdf page_02, P2-S7 の積分区間 "t = 1 to t = 0"）。
# "a = b"のような表現が実在の英文として自然に使われることは無いため、
# 自動的に数式とみなして翻訳前に$...$へ包んで保護してよいと判断できる。
# この翻訳前保護により、翻訳後にこの形が壊れていないかを見る事後チェック
# は不要（stage6.pyはこの形を扱わない）。

_BARE_LETTER_EQUALS_RE = re.compile(r"\b[a-zA-Z]\s*=\s*[a-zA-Z0-9]+\b")


# $...$で保護されていない「1文字の変数 = 値」形式の数式的表現（例:"t = 1"）
# を$...$に包み、翻訳エンジンから保護する。
# 既に$...$/$$...$$で保護済みの数式スパン内は対象外とし、二重にラップしない
# （wrap_bare_greek_lettersと同じ、_INLINE_MATH_REで数式スパンを避けながら
# 処理する方式）。text=変換対象の文字列（MinerUのtext_levelなし本文や
# キャプション等）。戻り値: 該当パターンを$...$に包んだ後の文字列（数式
# スパン部分は元のまま）。
def wrap_bare_letter_equals_expressions(text: str) -> str:
    def run_repl(m: re.Match) -> str:
        return f"${m.group(0)}$"

    return _apply_outside_math_spans(text, _BARE_LETTER_EQUALS_RE, run_repl)


# --- ギリシャ文字の数式保護 ----------------------------------------------------
#
# MinerUの数式検出は完全ではなく、"scale γ"のように地の文に単独で出現する
# ギリシャ文字が$...$で囲まれないことがある（stage6.py参照）。
# 英語の地の文にギリシャ文字が実在の単語として使われることは無い
# （"x"や"K"のような半角英数字1文字とは異なり、実在の英単語・略語との
# 区別に悩む必要が無い）ため、$...$で保護されていないギリシャ文字は
# 自動的に数式とみなして保護してよい、と判断できる。

_GREEK_LETTER_RE = re.compile(r"[Α-Ωα-ω]")
_GREEK_RUN_RE = re.compile(r"[Α-Ωα-ω]+")

# Unicodeのギリシャ文字1文字 → 対応するTeXコマンド名（バックスラッシュ無し）。
# vendor/katex/katex.min.js（KaTeX 0.16.11）内の記号定義
# （例: se(...,"γ","\\gamma",...)）から抽出した対応表であり、KaTeXが
# 生のUnicodeギリシャ文字を扱う際に内部的にエイリアスとして解決している
# のと同じコマンド名を使う。生の文字のままでもKaTeX上は等価に描画される
# ことを確認済みだが、他の数式スパン（MinerU由来）がすべてTeXコマンド
# 形式であることとの一貫性のため、明示的にコマンド形式へ変換する。
#
# 一部の大文字（Α,Β,Ε,Ζ,Η,Ι,Κ,Μ,Ν,Ο,Ρ,Τ,Χ）はラテン文字と同形のため
# KaTeXに専用コマンドが無い（未定義のTeXコマンドはKaTeXがエラーにする
# ため、この表に無い文字は変換せず元の文字のまま$...$で囲む）。
_GREEK_TO_TEX_COMMAND = {
    "Γ": "Gamma", "Δ": "Delta", "Θ": "Theta", "Λ": "Lambda", "Ξ": "Xi",
    "Π": "Pi", "Σ": "Sigma", "Υ": "Upsilon", "Φ": "Phi", "Ψ": "Psi", "Ω": "Omega",
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "varepsilon",
    "ζ": "zeta", "η": "eta", "θ": "theta", "ι": "iota", "κ": "kappa",
    "λ": "lambda", "μ": "mu", "ν": "nu", "ξ": "xi", "ο": "omicron",
    "π": "pi", "ρ": "rho", "ς": "varsigma", "σ": "sigma", "τ": "tau",
    "υ": "upsilon", "φ": "varphi", "χ": "chi", "ψ": "psi", "ω": "omega",
}


# $...$で保護されていないギリシャ文字をTeXコマンド形式（例:"γ"→"\gamma"）
# に変換した上で$...$に包み、翻訳エンジンから保護する（DeepLに地の文として
# 渡って表記が変質するリスクを防ぐ）。
# 既に$...$/$$...$$で保護済みの数式スパン内は対象外とし、二重にラップしない
# （restore_merged_hyphensと同じ、_INLINE_MATH_REで数式スパンを避けながら
# 処理する方式）。連続するギリシャ文字（例:"αβ"）は、それぞれを個別の
# コマンドに変換して連結する（"\alpha\beta"。TeXコマンド名はバックスラッシュ
# で区切られるため、間に空白は不要）。
# text=変換対象の文字列（MinerUのtext_levelなし本文やキャプション等）。
# 戻り値: ギリシャ文字をTeXコマンド形式で$...$に包んだ後の文字列（数式
# スパン部分は元のまま）。
def wrap_bare_greek_letters(text: str) -> str:
    def char_repl(m: re.Match) -> str:
        ch = m.group(0)
        command = _GREEK_TO_TEX_COMMAND.get(ch)
        return f"\\{command}" if command is not None else ch

    def run_repl(m: re.Match) -> str:
        converted = _GREEK_LETTER_RE.sub(char_repl, m.group(0))
        return f"${converted}$"

    return _apply_outside_math_spans(text, _GREEK_RUN_RE, run_repl)


# --- 結合語の分割（MinerUの改行ハイフン処理の補正） ---------------------------
#
# MinerUは改行時のハイフンを無条件に除去するため、"data-efficient"が
# "dataefficient"に、"well-localized"が"welllocalized"になるなど、複合語の
# ハイフンが失われることがある。辞書に無い語のうち、両側とも辞書に載っている
# 分割点が見つかった場合のみハイフンを復元する。

_MERGED_WORD_RE = re.compile(r"\b[a-z]{6,}\b")

MERGED_WORD_MIN_HALF_LEN = 3
"""結合語を分割する際の各半分の最小文字数。短すぎる分割による誤爆を防ぐ。"""


# 辞書に無い単語を対象に、両側とも辞書に載っている分割点を探してハイフンを
# 復元する。
# 実在する1語や固有名詞・モデル名（大文字を含む語）はそのまま返す（Noneを
# 返し、呼び出し側で元の語を維持する）。分割候補が複数見つかる場合は、
# 最初に見つかったもの（最も左側の分割点）を採用する。
def split_merged_compound(word: str) -> str | None:
    if is_known_word(word):
        return None
    for split_at in range(MERGED_WORD_MIN_HALF_LEN, len(word) - MERGED_WORD_MIN_HALF_LEN + 1):
        left, right = word[:split_at], word[split_at:]
        if is_known_word(left) and is_known_word(right):
            return f"{left}-{right}"
    return None


# テキスト中の誤って連結された複合語（小文字のみの語に限定し、固有名詞や
# モデル名を誤爆しないようにする）にハイフンを復元する。
# $...$で囲まれたインライン数式部分は対象外とする。数式中のLaTeXコマンド
# （例: "\mathcal", "\textless", "\emptyset"）は英字が6文字以上連続するため
# 誤って複合語と判定され、"\math-cal"のように壊れてしまうことがある。
# text=変換対象の文字列（MinerUのtext_levelなし本文やキャプション等）。
# 戻り値: ハイフン復元後の文字列（数式部分は元のまま保持される）。
def restore_merged_hyphens(text: str) -> str:
    def repl(m: re.Match) -> str:
        fixed = split_merged_compound(m.group(0))
        return fixed if fixed is not None else m.group(0)

    return _apply_outside_math_spans(text, _MERGED_WORD_RE, repl)


# --- 文分割 ------------------------------------------------------------------

_ABBREVIATIONS = {
    "e.g.", "i.e.", "et al.", "etc.", "cf.", "vs.",
    "fig.", "figs.", "eq.", "eqs.", "sec.", "secs.",
    "no.", "nos.", "dr.", "mr.", "mrs.", "ms.", "prof.",
    "pp.", "vol.", "ed.", "eds.", "approx.",
}
_SENTENCE_BOUNDARY_RE = re.compile(r"([.!?]+[\"'”\)\]]*)(\s+)(?=[A-Z0-9\"“(])")
_TRAILING_TOKEN_RE = re.compile(r"(\S+)$")
_INITIAL_RE = re.compile(r"^[A-Z]\.$")


# 英文テキストを文単位に分割する（略語による誤分割を抑制）。
# text=1段落分程度の英文テキスト。
# 戻り値: 文単位に分割された文字列のリスト（空文字列は除外される）。
def split_sentences(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    sentences: list[str] = []
    last = 0
    for m in _SENTENCE_BOUNDARY_RE.finditer(text):
        end = m.end(1)
        candidate = text[last:end]
        token_match = _TRAILING_TOKEN_RE.search(candidate)
        raw_token = token_match.group(1) if token_match else ""
        token = re.sub(r"^[^A-Za-z0-9]+", "", raw_token)
        if token.lower() in _ABBREVIATIONS or _INITIAL_RE.match(token):
            continue
        sentence = text[last:end].strip()
        if sentence:
            sentences.append(sentence)
        last = m.end()
    tail = text[last:].strip()
    if tail:
        sentences.append(tail)
    return sentences


# --- 見出し・キャプション判定 -------------------------------------------------

HEADING_RE = re.compile(r"^([A-Z0-9]+(?:\.\d+)*)\.\s+(\S.*)$")
"""章・節見出しの判定（例: "1. INTRODUCTION", "2.1. Preliminaries"、
付録の英字見出し "A. EXPERIMENTAL SETUP", "A.1. Experimental Setup"）。"""

CAPTION_RE = re.compile(r"^(fig(?:ure)?|table)\.?\s*(\d+)\s*[:.]", re.IGNORECASE)
"""図表キャプションの判定・番号抽出（例: "Fig. 1: Overview of..." -> ("Fig", "1")）。"""

EQUATION_TAG_RE = re.compile(r"\\tag\{(\d+)\}")
"""MinerUが出力する数式LaTeX中の \\tag{n} から元論文の式番号を読み取る。"""


# 見出しテキストから章ラベル用の短い英字スラッグを作る（例: "Preliminaries" -> "preliminaries"）。
def slugify_section_name(title_text: str) -> str:
    m = re.match(r"[A-Za-z]+", title_text)
    return m.group(0).lower() if m else "section"


# キャプション文から種別（"figure"|"table"）と元論文の番号を読み取る。
# 戻り値: (種別, 番号)のタプル。キャプション形式と判定できない場合はNone。
def parse_caption_label(text: str) -> tuple[str, int] | None:
    m = CAPTION_RE.match(text.strip())
    if not m:
        return None
    fig_kind = "figure" if m.group(1).lower().startswith("fig") else "table"
    return fig_kind, int(m.group(2))


# ============================================================================
# キャッシュ: 同一条件での再実行を高速化するだけの、速度最適化専用のコード
#   （読み込み・保存いずれの失敗も、常に「キャッシュ不使用の通常実行」へ
#   静かにフォールバックする。本番の正しさには一切関与しない）
# ============================================================================


# MinerUのバージョンが取得できない場合に送出する例外。
class MinerUVersionError(RuntimeError):
    pass


# インストール済みMinerUパッケージのバージョン文字列を返す。
# キャッシュキーの構成要素として使うため、mineruパッケージが正しく
# インストールされていない環境では例外にしてキャッシュを無効化する
# （誤って別バージョンのキャッシュを使い回すことを防ぐ）。
def get_mineru_version() -> str:
    try:
        return importlib.metadata.version("mineru")
    except importlib.metadata.PackageNotFoundError as exc:
        raise MinerUVersionError(
            "mineruパッケージのバージョンが取得できません（未インストールの可能性）。"
        ) from exc


# キャッシュ機構を使ってよいか（環境変数`MINERU_CACHE_DISABLE`が`1`でないか）を返す。
def _cache_enabled() -> bool:
    # 呼び出しのたびに環境変数を読む（モジュール読み込み時に固定すると、
    # テストでの`monkeypatch.setenv`が反映されずテストしにくくなるため）。
    return os.environ.get("MINERU_CACHE_DISABLE", "") != "1"


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CACHE_ROOT = _REPO_ROOT / "cache"

# キャッシュの保存形式自体を将来変更した際に、古い形式のキャッシュを
# 機械的に無効化するためのスキーマバージョン。
_CACHE_SCHEMA_VERSION = 1


# キャッシュフォルダ名（`cache/<この値>/`）を組み立てる。range_labelがあれば
# 人間可読な範囲記述子を、無ければPDF名＋ページ番号ベースの名前を返す。
def _run_id(
    pdf_path: Path, start_page: int | None, end_page: int | None, range_label: str | None = None
) -> str:
    stem = pdf_path.stem
    if range_label is not None:
        # range_labelは呼び出し側（whole_pipeline.describe_page_range）が
        # 元のCLIオプション（--chapter/--start-label/--start等）から組み立てる
        # 人間可読な範囲記述子（例:"full","label55-60"）。指定があれば、
        # 生のページ番号ベースの命名より優先する（cache/配下のフォルダ名を
        # 人間が見て分かりやすくするため）。キャッシュの正当性チェック自体は
        # 引き続きstart_page/end_page（meta.json）で行うため、フォルダ名を
        # 変えても正しさには影響しない。
        return f"{stem}_{range_label}"
    if start_page is None and end_page is None:
        return stem
    return f"{stem}_p{start_page}_{end_page}"


# MinerU実行結果を格納するサブフォルダ名を返す。
# 既定のbackend（"pipeline"）は"mineru_cache"のままとし、それ以外の
# backendを選んだ場合のみ"mineru_cache_<backend>"として区別する
# （バックエンドが異なればcontent_listの形式・認識精度も異なるため、
# 同じフォルダで混在させない）。
def _mineru_cache_subdir_name(backend: str) -> str:
    if backend == _DEFAULT_BACKEND:
        return "mineru_cache"
    return f"mineru_cache_{backend}"


# 対象PDF・ページ範囲・バックエンドに対応するMinerUキャッシュフォルダの
# 絶対パスを返す（`_run_id`と`_mineru_cache_subdir_name`の連結）。
def _cache_dir(
    pdf_path: Path,
    start_page: int | None,
    end_page: int | None,
    range_label: str | None = None,
    backend: str = _DEFAULT_BACKEND,
) -> Path:
    return _CACHE_ROOT / _run_id(pdf_path, start_page, end_page, range_label) / _mineru_cache_subdir_name(backend)


# PDFファイルの内容のSHA-256ハッシュ文字列を返す（メモリを圧迫しないよう
# 1MBずつ読む）。キャッシュの正当性判定（meta.jsonとの突合）に使う。
def _compute_pdf_hash(pdf_path: Path) -> str:
    hasher = hashlib.sha256()
    with pdf_path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


# キャッシュがあり、かつPDFの中身・MinerUバージョンが一致すれば
# (items, images_base)を返す。キャッシュが無い・古い・壊れている場合は
# Noneを返し、呼び出し側に通常実行を促す。
# range_labelはフォルダ名の組み立てにのみ使う（save_cache参照）。キャッシュ
# の正当性判定自体はstart_page/end_page（とpdf_sha256・mineru_version・
# backend）で行う。backendが異なるキャッシュは別フォルダ（_cache_dir参照）
# に保存されるため、フォルダの時点で自然に混在しないが、念のためmeta.json
# でも突合する。
def load_cached_items(
    pdf_path: Path,
    start_page: int | None,
    end_page: int | None,
    range_label: str | None = None,
    backend: str = _DEFAULT_BACKEND,
) -> tuple[list[dict], Path] | None:
    if not _cache_enabled():
        return None

    cache_dir = _cache_dir(pdf_path, start_page, end_page, range_label, backend=backend)
    meta_path = cache_dir / "meta.json"
    content_list_path = cache_dir / "content_list.json"
    # cache_dir自体を images_base 相当として返す（cache_dir/"images"/<file>が
    # item["img_path"]（"images/<file>"形式）の解決先になる）。

    if not meta_path.exists() or not content_list_path.exists():
        return None

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        current_hash = _compute_pdf_hash(pdf_path)
        current_version = get_mineru_version()
    except (OSError, json.JSONDecodeError, MinerUVersionError):
        return None

    if (
        meta.get("cache_schema_version") != _CACHE_SCHEMA_VERSION
        or meta.get("pdf_sha256") != current_hash
        or meta.get("mineru_version") != current_version
        or meta.get("start_page") != start_page
        or meta.get("end_page") != end_page
        # 旧形式（backendキー無し）のキャッシュは"pipeline"実行時のものと
        # みなす（後方互換）。
        or meta.get("backend", _DEFAULT_BACKEND) != backend
    ):
        return None

    try:
        items = json.loads(content_list_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    return items, cache_dir


# MinerUの実行結果をキャッシュへ保存する。
# 一時ディレクトリへ書き込んでからrenameでアトミックに差し替えることで、
# 書き込み途中の中断による破損キャッシュの混入を防ぐ。
# range_labelを指定すると、フォルダ名が{stem}_{range_label}
# （例:"sample3_label55-60"）になり、生のページ番号ベースの命名
# （{stem}_p{開始}_{終了}）より人間が読みやすくなる。省略時は生のページ
# 番号ベースの命名になる。
# backend（既定"pipeline"）が既定値以外の場合、MinerU実行結果を格納する
# サブフォルダ名がmineru_cache_<backend>になり、既定のmineru_cache
# （pipeline用）とは別に保存される（_cache_dir参照）。
def save_cache(
    pdf_path: Path,
    start_page: int | None,
    end_page: int | None,
    items: list[dict],
    images_base: Path,
    range_label: str | None = None,
    backend: str = _DEFAULT_BACKEND,
) -> None:
    if not _cache_enabled():
        return

    try:
        pdf_hash = _compute_pdf_hash(pdf_path)
        version = get_mineru_version()
    except (OSError, MinerUVersionError):
        return

    cache_dir = _cache_dir(pdf_path, start_page, end_page, range_label, backend=backend)
    tmp_dir = cache_dir.with_name(cache_dir.name + ".tmp")

    try:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        (tmp_dir / "content_list.json").write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8"
        )
        # content_list.json中のimg_pathは"images/<file>"形式（images_base相対）
        # なので、images_base直下の"images/"サブフォルダだけをコピーする。
        # images_base（MinerUの{stem}/auto/）にはレイアウト検出結果や元PDFの
        # コピー等、翻訳パイプラインで使わない大きな中間ファイルも同居して
        # いるため、丸ごとコピーするとキャッシュが不必要に肥大化する。
        images_src = images_base / "images"
        if images_src.exists():
            shutil.copytree(images_src, tmp_dir / "images", dirs_exist_ok=True)

        # meta.jsonは「このキャッシュがどんな条件で作られたか」の指紋。
        # content_list.json・images/はMinerU由来の実データで、meta.jsonだけが
        # このプロジェクトが添える判定用データ（MinerUの出力ではない）。
        # load_cached_itemsが読み込み時にこの指紋（PDFの内容ハッシュ・ページ
        # 範囲・MinerUバージョン・backend・キャッシュ形式バージョン）を現在の
        # 要求と突き合わせ、全一致したときだけキャッシュを再利用する。1つでも
        # 変わっていれば不一致＝MinerU再実行。
        meta = {
            "cache_schema_version": _CACHE_SCHEMA_VERSION,
            "pdf_sha256": pdf_hash,
            "mineru_version": version,
            "start_page": start_page,
            "end_page": end_page,
            "backend": backend,
        }
        (tmp_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir.rename(cache_dir)
    except OSError:
        # キャッシュ保存の失敗（パス長超過等）はMinerU実行自体の成功結果には
        # 影響させない。中途半端な一時ディレクトリが残っても次回の保存時に
        # 上書きされるだけなので無害。
        return


# ============================================================================
# 本体: MinerUの実際の実行
# ============================================================================


# MinerUの実行に失敗した場合に送出する例外。
# このステップが失敗すると後続のステップに渡す生データが一切無いため、
# ここでの失敗はフォールバックせず明確なエラーとして呼び出し元に伝える。
class MinerURunError(RuntimeError):
    pass


# MinerU実行結果の受け渡し用データ。
@dataclass
class MinerUOutput:
    items: list[dict]
    """content_list.json の中身（ページ横断のフラットな要素リスト）。"""
    images_base: Path
    """items中の img_path（相対パス）を解決するための基準ディレクトリ。"""


# MinerUをサブプロセスとして実行し、content_list.jsonと画像群を取得する。
#
# 引数: pdf_path=入力PDFファイルのパス。work_dir=MinerUの生出力を書き込む
# 作業用ディレクトリ（呼び出し側が後片付けする想定。通常は一時ディレクトリ）。
# start_page/end_page=処理対象のページ範囲（0始まり・両端含む。Noneなら
# 先頭/末尾まで）。range_label=cache/配下のフォルダ名を人間可読にするための
# 任意の範囲記述子（省略時はページ番号ベースの命名。キャッシュの正当性判定
# には影響しない）。backend=使用するMinerUバックエンド
# （SUPPORTED_MINERU_BACKENDSのいずれか。既定は軽量・高速な"pipeline"。
# "vlm-engine"はCPUのみの環境でも動くが大幅に低速な代わりに数式・OCRの
# 認識精度が高い）。
#
# 戻り値: MinerUOutput（content_list要素列と画像の基準ディレクトリ）。
# ページ範囲を指定した場合、itemsのpage_idxは指定範囲内での相対値
# （先頭が常に0）になる点に注意（呼び出し側でオフセットを加算する責務を持つ）。
#
# 例外: backendがSUPPORTED_MINERU_BACKENDSに含まれない場合はValueError、
# MinerUのプロセスが異常終了した場合または期待した出力ファイルが生成
# されなかった場合はMinerURunError。
#
# 内部でローカルキャッシュ（本ファイル下部のload_cached_items/save_cache）を
# 利用する場合がある。同一PDF・同一ページ範囲・同一MinerUバージョン・同一
# backendでの再実行はキャッシュから返るため、work_dirにMinerUの生出力が
# 書き込まれないことがある。呼び出し側の型・戻り値仕様はキャッシュの有無に
# よらず不変。
def run_mineru(
    pdf_path: Path,
    work_dir: Path,
    start_page: int | None = None,
    end_page: int | None = None,
    range_label: str | None = None,
    backend: str = _DEFAULT_BACKEND,
) -> MinerUOutput:
    # 早期バリデーション: サポート外のbackendは、コマンド組み立て・
    # キャッシュ参照より前にここで弾く。
    if backend not in SUPPORTED_MINERU_BACKENDS:
        raise ValueError(
            f"サポートされていないMinerUバックエンドです: {backend!r}"
            f"（サポート対象: {', '.join(SUPPORTED_MINERU_BACKENDS)}）"
        )

    # キャッシュ参照: 同一PDF・同一ページ範囲・同一MinerUバージョン・同一
    # backendでの再実行なら、MinerU本体を起動せずここで結果を返す（この場合
    # work_dirには何も書かれない）。判定・読み込みの詳細はload_cached_items。
    cached = load_cached_items(pdf_path, start_page, end_page, range_label=range_label, backend=backend)
    if cached is not None:
        items, images_base = cached
        return MinerUOutput(items=items, images_base=images_base)

    # --- ここから下はキャッシュ不命中時のみ。実際にMinerUを実行する ---

    # MinerU CLIの起動コマンドを組み立てる。
    command = [
        sys.executable, "-m", "mineru.cli.client",
        "--path", str(pdf_path),
        "--output", str(work_dir),
        "--backend", backend,
    ]
    # --methodはpipeline/hybrid-*バックエンドにのみ有効なオプション
    # （MinerU CLIの仕様。vlm-engineに渡すと無視されるだけだが、
    # 意味の無い引数を渡さないよう明示的に絞る）。
    if backend == "pipeline":
        command += ["--method", "auto"]
    # ページ範囲は指定された端だけを渡す（両方Noneなら--start/--end自体を
    # 付けず、MinerU側の既定＝PDF全ページになる）。
    if start_page is not None:
        command += ["--start", str(start_page)]
    if end_page is not None:
        command += ["--end", str(end_page)]

    # MinerUを子プロセスとして実行する。生出力（content_list.json・切り出し
    # 画像・レイアウト検出結果等）はwork_dir配下に書かれる。異常終了は
    # フォールバックせずMinerURunErrorへ（後続へ渡す生データが無いため。
    # stderrはメッセージに含める）。
    try:
        subprocess.run(command, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        raise MinerURunError(f"MinerUの実行に失敗しました（終了コード {exc.returncode}）: {stderr}") from exc

    # MinerUが書き出したcontent_list.jsonを探す。ファイルは
    # work_dir/<stem>/<サブフォルダ>/<stem>_content_list.json に置かれるが、
    # <サブフォルダ>の名前はバックエンドで異なる（pipelineは"auto"、
    # vlm-engineは"vlm"等）。そこを`*`にしたglobで検索し、サブフォルダ名の
    # 違いを吸収する。
    stem = pdf_path.stem
    matches = sorted(work_dir.glob(f"{stem}/*/{stem}_content_list.json"))
    if not matches:
        raise MinerURunError(f"MinerUの出力が見つかりません: {work_dir / stem}/*/{stem}_content_list.json")
    content_list_path = matches[0]

    # 見つけたcontent_list.jsonを読み込む。中のimg_path（"images/<file>"形式の
    # 相対パス）はこのファイルと同じフォルダを基準に解決するため、その親
    # フォルダをimages_baseとして戻り値に含める。
    items = json.loads(content_list_path.read_text(encoding="utf-8"))
    images_base = content_list_path.parent

    # 同一条件での次回の再実行を高速化するため、結果をキャッシュへ保存してから
    # 返す（save_cacheの失敗は中で握りつぶされ、今回の戻り値には影響しない）。
    save_cache(pdf_path, start_page, end_page, items, images_base, range_label=range_label, backend=backend)
    return MinerUOutput(items=items, images_base=images_base)


# ============================================================================
# 構造解析
# ============================================================================


# 本文・キャプション由来の生テキストへ、未保護の数式的表現の保護と結合語
# ハイフンの復元をまとめて適用する（"1文字の変数 = 値"→ギリシャ文字→結合語
# ハイフンの順で適用。この順序で最終的にwrap_bare_letter_equals_expressions/
# wrap_bare_greek_lettersが挿入する$記号がrestore_merged_hyphensの対象
# （数式スパンの除外判定）に正しく反映される）。
def _normalize_math_text(text: str) -> str:
    return restore_merged_hyphens(wrap_bare_greek_letters(wrap_bare_letter_equals_expressions(text)))


FRONT_MATTER_LABELS = ["TITLE", "AUTHORS", "AFFIL"]
"""ページ1冒頭、最初の見出しが現れるまでのブロックに出現順で割り当てるラベル。"""

ABSTRACT_SECTION_ID = "abstract"
"""最初の章番号付き見出しが現れるまでの文（ABSTRACT見出し・本文含む）に用いる章ラベル。"""

# MinerUが本文として意味を持たせている（構造解析の対象とする）要素種別。
# これ以外の種別（例: "discarded"等、将来MinerU側に追加される可能性のある
# 種別）は個別のif分岐を増やすのではなく、_handle_unknown_item の
# フォールバックに任せる。
_NOISE_TYPES = {"aside_text", "header", "footer", "page_number"}
"""意味のあるコンテンツを含まないと判断してよい種別。

"aside_text"（arXiv ID等）に加え、"header"（実行見出しの繰り返し）・
"footer"（著作権表示等の繰り返し）・"page_number"（ノンブル）を含む。
いずれもMinerUの`BlockType`（pipeline/vlm-engine両バックエンド共通）で
定義された、ページごとに機械的に繰り返される紙面要素であり、実データ
（sample2.pdf・sample3.pdf）で目視確認した限り実質的な本文を含まない
（例外: "page_footnote"は本物の脚注本文を含みうるため、ここに含めず
_handle_text_itemへ回している。下記参照）。"""


# 1ページ分の要素リストを構築する際に必要な状態（前付けラベルの割当状況、
# 未ラベル画像の連番）をまとめて保持する小さなヘルパー。
class _PageBuilder:
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


# 要素からベストエフォートでテキストらしきものを取り出す。UnknownElement
# フォールバック用。既知のテキスト系フィールドを優先順に試し、何も
# 見つからなければ空文字列を返す。
def _extract_raw_text(item: dict) -> str:
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


# 箇条書き・参考文献リスト等の要素を処理する。
# MinerUが既にlist_itemsとして項目単位に分割済みのため、独自の文分割
# （split_sentences）は行わず、各項目をそのまま1つの文として扱う
# （参考文献のように文中にピリオドを含む短い項目が多く、文分割ロジックに
# 通すとかえって誤分割を起こしやすいため）。
def _handle_list_item(item: dict, builder: _PageBuilder) -> None:
    raw_items = item.get("list_items") or []
    sentences = [_normalize_math_text(s.strip()) for s in raw_items if s.strip()]
    if sentences:
        builder.elements.append(TextBlockElement(sentences=sentences))


# 図・表・チャート要素を処理する。MinerUのimage_caption/table_caption/
# chart_captionは通常1件だが、レイアウト検出の都合で隣接する図表の
# キャプションが1つのブロックへ誤って結合されることがある（例: Fig.8と
# Fig.9のキャプションが、Fig.9側の画像ブロックにまとめて付与される）。
# 独立した複数のFig./Table番号を検出した場合は、直前に追加した未ラベルの
# 画像へ遡ってキャプションを割り当て直し、最後のキャプションを今回の
# 画像に割り当てる。
# "chart"（グラフ画像）は構造上は"image"と同じ（img_path + キャプション）
# であり、キャプションの実際のラベル（Fig./Table）はキャプション文自体
# から判定するため、種別ごとに分岐する必要はない。
def _handle_image_or_table_item(item: dict, item_type: str, images_base, builder: _PageBuilder) -> None:
    elements = builder.elements
    raw_captions = (
        item.get("image_caption") or item.get("table_caption") or item.get("chart_caption") or []
    )
    captions = [_normalize_math_text(c.strip()) for c in raw_captions if c.strip()]

    # MinerUはtable_caption/image_captionに、キャプション文と脚注（例: 表の
    # 下にある"*Our own implementation..."等の補足説明）を別要素として並べて
    # 返すことがある。脚注自体はFig./Tableラベルを持たないため、直前に現れた
    # ラベル付きキャプションへの継続として結合する（まだ一度もラベルが
    # 現れていない場合のみ、独立した未ラベル要素として扱う（無視する）。
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


# 数式要素を処理する。MinerUが生成したLaTeXから\tag{n}を探し、見つかれば
# それを元論文の式番号として使う（ページをまたいでもリセットしない）。
# 見つからない場合はフォールバック連番で出力する。
# img_path（切り出し画像）はバックエンドによっては存在しない（例:
# vlm-engineは数式をLaTeXテキストのみで返す）。その場合はEquationElement.
# image_pathをNoneのままにする。最終PDFの描画は常にlatex側（KaTeX）で
# 行うため、画像が無くても表示上の欠落は起きない（文書組み立て節参照）。
# latex自体が空の場合のみ、描画できる情報が無いため要素ごと捨てる。
def _handle_equation_item(item: dict, images_base, builder: _PageBuilder) -> None:
    latex = item.get("text", "").strip()
    latex_stripped = latex.removeprefix("$$").removesuffix("$$").strip()
    if not latex_stripped:
        return
    tag_match = EQUATION_TAG_RE.search(latex)
    img_path = images_base / item["img_path"] if item.get("img_path") else None
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


# 通常のテキストブロックを処理する。ページ1冒頭は前付け（タイトル・著者・
# 所属）として、見出しレベル（text_level 1か2）であればHeadingElementとして、
# それ以外は本文段落として文分割する。
# unnumbered_heading_seq: 文書全体で共有する、番号無し見出し用の連番カウンタ
# （1要素のリストに包んだ可変な整数）。書籍PDF等、見出しに章番号が付いて
# いない場合の合成章番号（"u1", "u2", ...）の採番に使う。
def _handle_text_item(item: dict, builder: _PageBuilder, unnumbered_heading_seq: list[int]) -> None:
    text = _normalize_math_text(item.get("text", "").strip())
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
    # なので、独立したHeadingElementにはせず、本文の1文として
    # 扱う（そうしないと[P1-S1-abstract-S1] ABSTRACTという文IDが
    # [P1-HEADING-u1.abstract]に変わってしまい、ID体系の一貫性が崩れる）。
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


# 未対応の要素種別、または既知種別の解析中に例外が発生した場合の
# フォールバック。生テキストをそのまま保持して処理を継続する。
def _handle_unknown_item(item: dict, item_type: str, builder: _PageBuilder, reason: str = "") -> None:
    text = _extract_raw_text(item)
    logger.warning("未対応の要素種別 %r を検出しました（フォールバックで出力）: %s", item_type, reason or "unknown type")
    builder.elements.append(UnknownElement(raw_type=str(item_type), text=text, reason=reason))


# content_list要素列をページ単位のPageContentへ変換する。
# 1要素の解析に失敗しても他の要素・ページの処理は継続する（UnknownElement
# へフォールバックする）。
# page_offset: items内のpage_idx（常に0始まりの相対値）に加算する絶対ページ
# オフセット（0始まり）。--startでPDFの途中から処理した場合に、実際の
# ページ番号を復元するために使う。
def _build_pages(items: list[dict], images_base, page_offset: int = 0) -> list[PageContent]:
    # page_idx（0始まりの相対ページ番号）→ そのページの組み立て状態（_PageBuilder）。
    builders: dict[int, _PageBuilder] = {}
    # 番号無し見出しの合成番号（"u1","u2",...）用の連番。全ページ横断で共有する
    # ため、1要素のリストに包んで_handle_text_itemへ参照渡しする。
    unnumbered_heading_seq = [0]

    for item in items:
        # この要素が属するページの_PageBuilderを取り出す（無ければ作る）。
        page_idx = item.get("page_idx", 0)
        builder = builders.setdefault(page_idx, _PageBuilder(page_idx, page_offset))
        item_type = item.get("type")

        if item_type in _NOISE_TYPES:
            continue  # arXiv IDなどのノイズ（意図的に捨てる）

        # item_typeごとに専用ハンドラへ振り分ける。未知のitem_typeとハンドラ内の
        # 例外は、どちらも_handle_unknown_itemへ集約する（下のexcept節）。
        try:
            if item_type in ("image", "table", "chart"):
                _handle_image_or_table_item(item, item_type, images_base, builder)
            elif item_type == "equation":
                _handle_equation_item(item, images_base, builder)
            elif item_type == "list":
                _handle_list_item(item, builder)
            elif item_type in ("text", "page_footnote"):
                # "page_footnote"は紙面上は脚注欄に配置されるが、実データ
                # （sample3.pdf）で目視確認した限り、単なる紙面ノイズでは
                # なく本物の学術脚注（引用・補足説明）を含むことがある。
                # _NOISE_TYPESで捨てると内容が失われるため、通常の本文と
                # 同じ扱いで翻訳対象にする（前付け判定はis_first_pageかつ
                # 見出し未出現の場合のみ発動するため、見出しの後に現れる
                # 脚注が誤って前付けラベル扱いになることはない）。
                _handle_text_item(item, builder, unnumbered_heading_seq)
            else:
                _handle_unknown_item(item, item_type, builder, reason="未知の要素種別")
        except Exception as exc:  # noqa: BLE001 - 1要素の失敗で全体を止めないためのフォールバック
            _handle_unknown_item(item, item_type, builder, reason=f"{type(exc).__name__}: {exc}")

    # page_idx順に並べ、page_offsetを足した1始まりの絶対ページ番号でPageContentにする。
    return [
        PageContent(page_number=idx + 1 + page_offset, elements=b.elements)
        for idx, b in sorted(builders.items())
    ]


# 本文の文（ABSTRACTを含む）とキャプションの文に、表示用IDを付与する。
#
# 本文ID形式: [P{page}-S{ページ内通し番号}-{章ラベル}-S{章内通し番号}]
# - ページ内通し番号は各ページの先頭で1にリセットする。
# - 章内通し番号は、最初の章番号付き見出し（例: "1. INTRODUCTION"）が現れる
#   まではabstractを章ラベルとして使う。同じ章が次ページに続く場合、章内
#   通し番号はページをまたいでも継続する（見出しが変わった時のみリセット）。
# - タイトル・著者・所属・図・見出し自体はこのカウントに含めない。
#
# キャプションID形式: [P{page}-{FIG|TABLE}{n}-CAPTION-S{通し番号}]
def _assign_sentence_ids(pages: list[PageContent]) -> None:
    # section_id/section_seqはページループの外で初期化する。こうすることで、
    # 同じ章が次ページに続いても章内通し番号がリセットされず継続する
    # （リセットは見出しが変わった時だけ。下記のHeadingElement分岐）。
    section_id = ABSTRACT_SECTION_ID
    section_seq = 0
    for page in pages:
        page_seq = 0  # ページ内通し番号は各ページ先頭で1へ戻す
        for element in page.elements:
            if isinstance(element, HeadingElement):
                # 章が切り替わった: 章ラベルを差し替え、章内通し番号を振り直す
                section_id = element.section_id
                section_seq = 0
            elif isinstance(element, TextBlockElement):
                # 本文の各文へ [P{page}-S{ページ内通し}-{章ラベル}-S{章内通し}]。
                # 1文ごとにページ内・章内の両カウンタを進める。
                element.sentence_ids = []
                for _ in element.sentences:
                    page_seq += 1
                    section_seq += 1
                    element.sentence_ids.append(f"P{page.page_number}-S{page_seq}-{section_id}-S{section_seq}")
            elif isinstance(element, CaptionElement):
                # キャプションは本文とは独立した体系: 図表番号ごとにS1..Snを振る。
                # ページ内・章内カウンタには一切関与しない（＝本文の通し番号は
                # キャプションを挟んでもずれない）。
                prefix = "FIG" if element.fig_kind == "figure" else "TABLE"
                label = f"{prefix}{element.number}"
                element.sentence_ids = [
                    f"P{page.page_number}-{label}-CAPTION-S{seq}" for seq in range(1, len(element.sentences) + 1)
                ]
            # 他の種別（LabeledElement/FigureElement/EquationElement/UnknownElement）は
            # どの分岐にも入らず、文カウントに含めない（前付け・図・数式・見出しは
            # 文IDを持たない）。


# MinerUのcontent_list（本文・数式・図表キャプション等が種別だけ付いて
# フラットに並んだ生ブロック列。意味づけはされていない）を、翻訳対象の各文へ
# 一意で安定したID（例: "P1-S4-1.introduction-S1"）を振り、ページ別に並べ直した
# StructuredDocumentへ変換する。このIDが、翻訳後に訳文を元の文へ突き合わせて
# 正しい位置へ戻すための鍵になる。
#
# 内部は2ステップ: _build_pagesがpage_idxごとに要素を種別判定して中間表現
# （TextBlockElement等）へ振り分け、_assign_sentence_idsがその本文・
# キャプション文へ表示用IDを付ける。
#
# 引数: items=MinerUOutput.items（content_list.jsonの中身）。images_base=
# img_path（相対パス）を解決するための基準ディレクトリ。page_offset=
# --startでPDFの途中から処理した場合の絶対ページオフセット（0始まり。
# 省略時は0＝PDFの先頭から処理）。
# 戻り値: ページ別の要素列を保持するStructuredDocument。
def analyze_structure(items: list[dict], images_base, page_offset: int = 0) -> StructuredDocument:
    # 1. 要素をpage_idxごとに種別判定して中間表現（TextBlockElement等）へ振り分け、
    #    ページ単位のPageContentにまとめる。
    pages = _build_pages(items, images_base, page_offset)
    # 2. 各PageContent内の本文・キャプション文へ表示用IDを付ける（pagesを直接書き換え）。
    _assign_sentence_ids(pages)
    return StructuredDocument(pages=pages)


# ============================================================================
# 文書組み立て
# ============================================================================


# 図・表・数式のラベル文字列を生成する（例: "FIG3", "TABLE1", "EQ2"）。
def _figure_label(fig_kind: str, number: int, labeled: bool) -> str:
    prefix = {"figure": "FIG", "table": "TABLE", "equation": "EQ"}[fig_kind]
    if labeled:
        return f"{prefix}{number}"
    return f"{prefix}-UNLABELED{number}"


# FigureElement/EquationElementの画像をPNGとして保存し、Markdownから
# 参照する相対パスを返す。
def _save_element_image(element, images_dir: Path, page_number: int) -> str:
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


# 1ページ分の要素列をMarkdownテキストへレンダリングする（画像の保存も行う）。
def _render_page_markdown(page: PageContent, images_dir: Path) -> str:
    lines: list[str] = []
    # 同一ページ内に同じraw_typeのUnknownElementが複数出現した場合に、タグが
    # 完全に重複してしまう（例: 同一ページに"code"種別が2つあるとどちらも
    # [P8-UNKNOWN-code]になる）のを防ぐためのページ内連番カウンタ。タグの
    # 一意性自体はページ番号で既に担保されているため、ページ単位（セクション
    # 単位ではなく）で連番を振れば十分（実データのsample1.pdf、Appendix A.1の
    # プロンプト例文2件で重複を確認した回帰）。
    unknown_seq: dict[str, int] = {}
    for element in page.elements:
        if isinstance(element, FigureElement):
            path = _save_element_image(element, images_dir, page.page_number)
            label = _figure_label(element.fig_kind, element.number, element.labeled)
            elem_id = f"P{page.page_number}-{label}"
            lines.append(f"![{elem_id}]({path}) [{elem_id}]")
            lines.append("")
        elif isinstance(element, EquationElement):
            label = _figure_label("equation", element.number, element.labeled)
            elem_id = f"P{page.page_number}-{label}"
            # image_pathが無いバックエンド（例: vlm-engine）では画像行を
            # 省略する。最終PDFの描画はLATEX行（KaTeX）側だけで完結する
            # ため、画像行が無くても表示上の欠落は起きない。
            if element.image_path is not None:
                path = _save_element_image(element, images_dir, page.page_number)
                lines.append(f"![{elem_id}]({path}) [{elem_id}]")
                lines.append("")
            lines.append(f"[{elem_id}-LATEX] $${element.latex}$$")
            lines.append("")
        elif isinstance(element, LabeledElement):
            label_id = f"P{page.page_number}-{element.label}"
            lines.append(f"[{label_id}] {element.text}")
        elif isinstance(element, HeadingElement):
            heading_id = f"P{page.page_number}-HEADING-{element.section_id}"
            lines.append(f"[{heading_id}] {element.text}")
        elif isinstance(element, CaptionElement):
            for sentence, sent_id in zip(element.sentences, element.sentence_ids):
                lines.append(f"[{sent_id}] {sentence}")
        elif isinstance(element, UnknownElement):
            unknown_seq[element.raw_type] = unknown_seq.get(element.raw_type, 0) + 1
            unknown_id = f"P{page.page_number}-UNKNOWN-{element.raw_type}-{unknown_seq[element.raw_type]}"
            lines.append(f"[{unknown_id}] {element.text}")
        elif isinstance(element, TextBlockElement):
            for sentence, sent_id in zip(element.sentences, element.sentence_ids):
                lines.append(f"[{sent_id}] {sentence}")
    return "\n".join(lines) + "\n"


# StructuredDocumentから原文のままのページ別Markdownを書き出す。
# 引数: doc=構造解析（analyze_structure）の出力（ページ別要素列）。
# output_dir=出力先ディレクトリ（images/サブディレクトリに画像を保存する）。
# first_page=出力対象の先頭ページ番号（1始まり。通常は1だが、--start指定時
# はその値になる）。last_page=出力対象の末尾ページ番号（1始まり・両端含む。
# 要素が1つも無いページも含め、first_pageからlast_pageまですべての
# Markdownファイルを出力する）。
# 戻り値: 生成されたMarkdownファイルパスのリスト（ページ順）。
def build_document(
    doc: StructuredDocument,
    output_dir: Path,
    first_page: int,
    last_page: int,
) -> list[Path]:
    # 図表・数式の切り出しPNGの保存先。この後 _render_page_markdown の中から
    # _save_element_image がここへ書き込む。
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    # first_page〜last_pageの全ページ分のPageContentを用意する。docに要素が
    # 1つも無いページ（構造解析で何も生成されなかった空白ページ等）は
    # pages_by_numberに存在しないため、空のPageContentで埋める（そうしないと
    # そのページのpage_XX_en.mdが欠番になり、後続工程のページ対応がずれる）。
    pages_by_number = {p.page_number: p for p in doc.pages}
    all_pages = [
        pages_by_number.get(n, PageContent(page_number=n)) for n in range(first_page, last_page + 1)
    ]

    # 各ページをMarkdownテキストへレンダリングし（図表・数式画像の保存もこの
    # 中で行われる）、page_XX_en.md（ページ番号は2桁ゼロ埋め）として書き出す。
    md_paths: list[Path] = []
    for page in all_pages:
        markdown = _render_page_markdown(page, images_dir)
        md_path = output_dir / f"page_{page.page_number:02d}_en.md"
        md_path.write_text(markdown, encoding="utf-8")
        md_paths.append(md_path)
    return md_paths


# ============================================================================
# 入口（MinerU実行→構造解析→成果物結合をこの順で実行。工程(2)全体を代表する）
# ============================================================================


# PDFを解析し、ページ別Markdown（文ID・画像切り出し付き）を出力する。
# 内部では「MinerU実行(run_mineru)→構造解析(analyze_structure)→成果物結合
# (build_document)」の3つをこの順に実行する（詳細はモジュールdocstring参照）。
#
# 引数: pdf_path=入力PDFファイルのパス。output_dir=出力先ディレクトリ
# （images/サブディレクトリに画像を保存する）。start_page/end_page=処理対象
# のページ番号範囲（1始まり・両端含む。省略時はPDFの先頭/末尾まで）。
# range_label=cache/配下のフォルダ名を人間可読にするための任意の範囲記述子
# （例:"full","label55-60"。省略時はページ番号ベースの命名。キャッシュの
# 正当性判定には影響しない）。mineru_backend=使用するMinerUバックエンド
# （SUPPORTED_MINERU_BACKENDSのいずれか。既定は軽量・高速な"pipeline"。
# "vlm-engine"はCPUのみの環境でも動くが大幅に低速な代わりに数式・OCRの
# 認識精度が高い）。
#
# 戻り値: 生成されたMarkdownファイルパスのリスト（ページ順）。
#
# 例外: start_page/end_pageがPDFの実際のページ数に対して不正な範囲を
# 指定している場合はValueError。MinerUの実行に失敗した場合はMinerURunError
# （後続のステップに渡す生データが得られないため、この失敗のみはフォール
# バックせず呼び出し元に伝播する）。
def process_pdf(
    pdf_path: str | Path,
    output_dir: str | Path,
    start_page: int | None = None,
    end_page: int | None = None,
    range_label: str | None = None,
    mineru_backend: str = "pipeline",
) -> list[Path]:
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)

    # 1. 範囲バリデーション用に、PDFの総ページ数だけを先に取得する
    #    （fitzはこの後MinerUには渡さず、ここでしか使わない）。
    with fitz.open(pdf_path) as doc:
        total_pages = doc.page_count

    # 2. start_page/end_page（省略可）を、実際に処理する1始まりのページ範囲
    #    （first_page〜last_page）へ確定する。省略時はPDF全体になる。
    first_page = start_page if start_page is not None else 1
    last_page = end_page if end_page is not None else total_pages
    if not (1 <= first_page <= last_page <= total_pages):
        raise ValueError(
            f"不正なページ範囲です: start_page={start_page}, end_page={end_page}"
            f"（PDFの総ページ数: {total_pages}）"
        )
    # 3. 範囲指定の有無を後段（MinerU呼び出し・page_offset計算）で使い分けるため
    #    ここで判定しておく（--start/--endどちらか一方でも指定されていれば範囲指定扱い）。
    page_range_specified = start_page is not None or end_page is not None

    # 4. MinerUの生出力を書き込むための一時作業ディレクトリ。with終了時に自動削除される
    #    （content_list.json・画像は後続でrun_mineru内部が読み取り、呼び出し元へは
    #    Pythonオブジェクト（MinerUOutput）として返るため、work_dir自体は永続化不要）。
    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp)

        # 5. MinerU実行（工程(2)前半）。MinerUはページ範囲を0始まりで受け取るため、
        #    1始まりのfirst_page/last_pageから1を引いて渡す。範囲未指定の場合は
        #    start/end自体を渡さず、PDF全体を処理させる。
        if page_range_specified:
            mineru_output = run_mineru(
                pdf_path, work_dir, first_page - 1, last_page - 1,
                range_label=range_label, backend=mineru_backend,
            )
        else:
            mineru_output = run_mineru(pdf_path, work_dir, range_label=range_label, backend=mineru_backend)

        # 6. 構造解析（工程(2)後半）。MinerUはitems内のpage_idxを常に0始まりの
        #    「指定範囲内での相対値」で返すため、絶対ページ番号（first_page始まり）に
        #    戻すためのオフセットをここで計算して渡す（範囲未指定ならオフセット0）。
        page_offset = (first_page - 1) if page_range_specified else 0
        structured_doc = analyze_structure(mineru_output.items, mineru_output.images_base, page_offset)

        # 7. 成果物結合。構造解析済みのStructuredDocumentを、要素が1つも無い
        #    ページも含めfirst_page〜last_pageの全ページ分、原文のままの
        #    ページ別Markdownとして書き出す。
        return build_document(structured_doc, output_dir, first_page, last_page)


# process_pdf単体の動作確認・デバッグ用の簡易CLI。
# `python mainCode/stage2/stage2.py <pdf> <output_dir>`のように工程(2)単体を
# 独立実行できるようにするためのエントリポイントで、通常の実行経路である
# translate_paper.py→whole_pipeline.main()は経由しない。
# whole_pipeline.pyにも同名のmain()（7工程すべてを統括する本番用の本体）が
# あるが、それとは別の同名関数であり、whole_pipeline.main()からこちらの
# main()が呼ばれることは無い（whole_pipeline.main()はprocess_pdfを直接呼ぶ）。
def main() -> None:
    # 1. CLI引数を定義して解釈する（pdf_path・output_dirは必須、範囲指定と
    #    MinerUバックエンドは任意。各引数の説明はhelp文字列側に持たせている）。
    parser = argparse.ArgumentParser(
        description="PDF学術論文を解析し、文単位ID付きのページ別Markdownを出力する。"
    )
    parser.add_argument("pdf_path", help="入力PDFファイルのパス")
    parser.add_argument("output_dir", help="出力先ディレクトリ（images/ サブディレクトリに画像を保存する）")
    parser.add_argument("--start", type=int, default=None, help="処理対象の開始ページ番号（1始まり）")
    parser.add_argument("--end", type=int, default=None, help="処理対象の終了ページ番号（1始まり・両端含む）")
    parser.add_argument(
        "--mineru-backend", choices=SUPPORTED_MINERU_BACKENDS, default="pipeline",
        help="使用するMinerUバックエンド。pipeline: 軽量・高速（既定）。"
        "vlm-engine: CPUのみの環境でも動くが大幅に低速な代わりに数式・OCRの認識精度が高い。",
    )
    args = parser.parse_args()

    # 2. 工程(2)の入口（process_pdf）をそのまま呼ぶ。range_labelはCLIからは
    #    渡さない（cache/フォルダ名がページ番号ベースになるだけで正しさには影響しない）。
    md_paths = process_pdf(
        args.pdf_path, args.output_dir, start_page=args.start, end_page=args.end,
        mineru_backend=args.mineru_backend,
    )

    # 3. 生成されたページ別Markdownのパスを1行ずつ標準出力へ列挙する。
    for path in md_paths:
        print(path)


if __name__ == "__main__":
    main()
