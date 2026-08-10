"""テキスト処理ユーティリティ。

PDFやMinerUの出力形式に関する知識を一切持たない、純粋な文字列変換関数群。
「英文を文単位に分割する」「複合語のハイフンを復元する」「見出し・キャプション
文字列から構造情報を読み取る」といった、単体でテスト可能な小さな処理をまとめる。

このモジュールは他のどのパイプラインモジュールにも依存しない（依存されるだけの
末端モジュール）。
"""

from __future__ import annotations

import re

from spellchecker import SpellChecker

_SPELL = SpellChecker()

# --- 辞書判定 ------------------------------------------------------------------


def is_known_word(word: str) -> bool:
    """wordが英単語として辞書に存在するかどうかを返す。

    注意: pyspellcheckerはアルファベット1文字を常に「既知」と判定する
    （編集距離1以内の候補が必ず見つかるため）。1文字の実在単語判定には
    使わないこと。
    """
    return bool(word) and word.lower() in _SPELL


# --- 結合語の分割（MinerUの改行ハイフン処理の補正） ---------------------------
#
# MinerUは改行時のハイフンを無条件に除去するため、"data-efficient"が
# "dataefficient"に、"well-localized"が"welllocalized"になるなど、複合語の
# ハイフンが失われることがある。辞書に無い語のうち、両側とも辞書に載っている
# 分割点が見つかった場合のみハイフンを復元する。

_MERGED_WORD_RE = re.compile(r"\b[a-z]{6,}\b")

MERGED_WORD_MIN_HALF_LEN = 3
"""結合語を分割する際の各半分の最小文字数。短すぎる分割による誤爆を防ぐ。"""

_INLINE_MATH_RE = re.compile(r"\$[^$]*\$")


def split_merged_compound(word: str) -> str | None:
    """辞書に無い単語を対象に、両側とも辞書に載っている分割点を探してハイフンを
    復元する。

    実在する1語や固有名詞・モデル名（大文字を含む語）はそのまま返す
    （Noneを返し、呼び出し側で元の語を維持する）。分割候補が複数見つかる
    場合は、最初に見つかったもの（最も左側の分割点）を採用する。
    """
    if is_known_word(word):
        return None
    for split_at in range(MERGED_WORD_MIN_HALF_LEN, len(word) - MERGED_WORD_MIN_HALF_LEN + 1):
        left, right = word[:split_at], word[split_at:]
        if is_known_word(left) and is_known_word(right):
            return f"{left}-{right}"
    return None


def restore_merged_hyphens(text: str) -> str:
    """テキスト中の誤って連結された複合語（小文字のみの語に限定し、固有名詞や
    モデル名を誤爆しないようにする）にハイフンを復元する。

    $...$ で囲まれたインライン数式部分は対象外とする。数式中のLaTeXコマンド
    （例: "\\mathcal", "\\textless", "\\emptyset"）は英字が6文字以上連続する
    ため誤って複合語と判定され、"\\math-cal"のように壊れてしまうことがある。

    Args:
        text: 変換対象の文字列（MinerUのtext_levelなし本文やキャプション等）。

    Returns:
        ハイフン復元後の文字列。数式部分は元のまま保持される。
    """

    def repl(m: re.Match) -> str:
        fixed = split_merged_compound(m.group(0))
        return fixed if fixed is not None else m.group(0)

    parts: list[str] = []
    last = 0
    for math_match in _INLINE_MATH_RE.finditer(text):
        parts.append(_MERGED_WORD_RE.sub(repl, text[last:math_match.start()]))
        parts.append(math_match.group(0))
        last = math_match.end()
    parts.append(_MERGED_WORD_RE.sub(repl, text[last:]))
    return "".join(parts)


# --- ギリシャ文字の数式保護（2026-08-10追加） ---------------------------------
#
# MinerUの数式検出は完全ではなく、"scale γ"のように地の文に単独で出現する
# ギリシャ文字が$...$で囲まれないことがある（math_protection.py参照）。
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
# 形式であることとの一貫性のため、明示的にコマンド形式へ変換する
# （2026-08-10、ユーザー指示により変更）。
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


def wrap_bare_greek_letters(text: str) -> str:
    """$...$で保護されていないギリシャ文字をTeXコマンド形式（例:"γ"→
    "\\gamma"）に変換した上で$...$に包み、翻訳エンジンから保護する
    （DeepLに地の文として渡って表記が変質するリスクを防ぐ）。

    既に$...$/$$...$$で保護済みの数式スパン内は対象外とし、二重にラップ
    しない（restore_merged_hyphensと同じ、_INLINE_MATH_REで数式スパンを
    避けながら処理する方式）。連続するギリシャ文字（例:"αβ"）は、それぞれ
    を個別のコマンドに変換して連結する（"\\alpha\\beta"。TeXコマンド名は
    バックスラッシュで区切られるため、間に空白は不要）。

    Args:
        text: 変換対象の文字列（MinerUのtext_levelなし本文やキャプション等）。

    Returns:
        ギリシャ文字をTeXコマンド形式で$...$に包んだ後の文字列。数式
        スパン部分は元のまま。
    """

    def char_repl(m: re.Match) -> str:
        ch = m.group(0)
        command = _GREEK_TO_TEX_COMMAND.get(ch)
        return f"\\{command}" if command is not None else ch

    def run_repl(m: re.Match) -> str:
        converted = _GREEK_LETTER_RE.sub(char_repl, m.group(0))
        return f"${converted}$"

    parts: list[str] = []
    last = 0
    for math_match in _INLINE_MATH_RE.finditer(text):
        parts.append(_GREEK_RUN_RE.sub(run_repl, text[last:math_match.start()]))
        parts.append(math_match.group(0))
        last = math_match.end()
    parts.append(_GREEK_RUN_RE.sub(run_repl, text[last:]))
    return "".join(parts)


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


def split_sentences(text: str) -> list[str]:
    """英文テキストを文単位に分割する（略語による誤分割を抑制）。

    Args:
        text: 1段落分程度の英文テキスト。

    Returns:
        文単位に分割された文字列のリスト（空文字列は除外される）。
    """
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


def slugify_section_name(title_text: str) -> str:
    """見出しテキストから章ラベル用の短い英字スラッグを作る（例: "Preliminaries" -> "preliminaries"）。"""
    m = re.match(r"[A-Za-z]+", title_text)
    return m.group(0).lower() if m else "section"


def parse_caption_label(text: str) -> tuple[str, int] | None:
    """キャプション文から種別（"figure"|"table"）と元論文の番号を読み取る。

    Returns:
        (種別, 番号) のタプル。キャプション形式と判定できない場合はNone。
    """
    m = CAPTION_RE.match(text.strip())
    if not m:
        return None
    fig_kind = "figure" if m.group(1).lower().startswith("fig") else "table"
    return fig_kind, int(m.group(2))
