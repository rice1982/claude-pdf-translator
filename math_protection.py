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


_UNPROTECTED_MATH_RE = re.compile(r"\b[a-zA-Z]\s*=\s*[a-zA-Z0-9]+\b")


def find_unprotected_math_like_tokens(text: str) -> list[str]:
    """$...$/$$...$$で保護されていない、数式らしき断片（例:"t = 1"）を
    ヒューリスティックに検出する。

    MinerUの数式検出は完全ではなく、同じ表現（例:"t = 1"）が文によっては
    $...$で囲まれず地の文に残ることがある。この関数はそうした未保護の
    断片を"文字＋=＋英数字"というパターンで機械的に拾い上げる。

    あくまで簡易なヒューリスティックであり、"z"や"K"のように"="を伴わず
    単独で使われる変数名は検出できない（誤検知を避けるため、英単語1文字
    との区別が付かないこの種のパターンはあえて対象にしていない）。
    """
    protected, _ = protect(text)
    return [m.group(0) for m in _UNPROTECTED_MATH_RE.finditer(protected)]


def check_unprotected_math_survival(units, log=print) -> list[str]:
    """翻訳前後で、保護されていない数式らしき断片が壊れていないかを
    ヒューリスティックに確認し、消えている場合は警告ログを出す。

    $...$で保護された数式はprotect/restoreにより翻訳の影響を受けないが、
    保護されていない断片（find_unprotected_math_like_tokens参照）は
    DeepLに地の文として渡るため、翻訳によって表記が変わるリスクが残る。
    この関数はDeepLの実際の翻訳結果に依存するため、モック翻訳を使う
    pytestでは意味のある検証ができない（実行のたびにDeepLへ実際に
    問い合わせる本番実行でのみ意味を持つ、実行時の安全確認）。

    Returns:
        警告メッセージのリスト（1件も無ければ空リスト）。
    """
    warnings: list[str] = []
    for unit in units:
        if not getattr(unit, "translatable", False):
            continue
        for token in find_unprotected_math_like_tokens(unit.en_text):
            normalized_token = re.sub(r"\s+", "", token)
            normalized_ja = re.sub(r"\s+", "", unit.ja_text)
            if normalized_token not in normalized_ja:
                message = (
                    f"[警告] {unit.tag}: 未保護の数式らしき文字列 {token!r} が"
                    "翻訳後の文に見つかりません（DeepLによって表記が変わった"
                    "可能性があります。念のため出力PDFを確認してください）。"
                )
                warnings.append(message)
                log(message)
    return warnings


def find_untranslated_fragment_candidates(ja_text: str) -> list[str]:
    """翻訳後のja_textに半角のまま残っている断片のうち、未検出（未保護）の
    数式らしい候補をヒューリスティックに洗い出す（2026-08-09追加）。

    日本語訳では地の文はほぼ全て全角文字になる一方、DeepLが自然言語として
    翻訳しない固有名詞・型番・数式的表記は半角のまま残る傾向がある。この
    性質を利用し、$...$で保護済みの数式（protectで除外済み）を除いた
    残りのテキストから半角文字列の連続を抽出する。

    ただし著者名・データセット名・略語（"FLUX"、"BSDS500"等）のような
    固有名詞も同様に半角のまま残るため、下記の条件で緩く絞り込む
    （find_unprotected_math_like_tokensより広い網だが、完全に誤検知を
    排除できるわけではない。人間が確認する前提のヒューリスティック）。
        - 少なくとも1つの英字を含む（引用番号"[12, 13]"等の数字・記号
          だけの断片を除外するため）
        - かつ、4文字以下、または"()=_^{}"等の数式らしい記号を含む
          （"EasyControlEdge"のような長い固有名詞を除外するため）
    """
    protected, _ = protect(ja_text)
    candidates = []
    # 半角スペースを挟んだ"t = 1"のような表記も1つの断片としてまとめて
    # 拾うため、非空白の半角文字で始まり非空白の半角文字で終わる区間を
    # マッチ対象にする（前後の全角文字・改行では区切られる）。
    for m in re.finditer(r"[\x21-\x7E](?:[\x20-\x7E]*[\x21-\x7E])?", protected):
        token = m.group(0)
        if _TOKEN_RE.fullmatch(token):
            continue  # __MATH0__ 等のプレースホルダ自身は候補ではない
        if not re.search(r"[a-zA-Z]", token):
            continue
        if len(token) <= 4 or re.search(r"[()=_^{}]", token):
            candidates.append(token)
    return candidates


def report_untranslated_fragment_candidates(units, log=print) -> list[str]:
    """find_untranslated_fragment_candidatesを翻訳対象unit全体に適用し、
    候補を情報ログとして出力する（2026-08-09追加）。

    find_unprotected_math_like_tokensより広い網で候補を拾うため、固有
    名詞・型番等の誤検知を含みうる。そのため警告（[警告]）ではなく
    情報（[情報]）として出力し、処理は止めない。あくまで「人間が確認
    すべき候補一覧」であり、この関数自体は正誤を判定しない。

    Returns:
        情報メッセージのリスト（1件も無ければ空リスト）。
    """
    messages: list[str] = []
    for unit in units:
        if not getattr(unit, "translatable", False):
            continue
        for token in find_untranslated_fragment_candidates(unit.ja_text):
            message = (
                f"[情報] {unit.tag}: 翻訳後も半角のまま残った断片 {token!r}"
                "（未検出の数式の可能性があります。固有名詞等による誤検知の"
                "可能性もあるため、必要に応じて出力PDFを確認してください）。"
            )
            messages.append(message)
            log(message)
    return messages
