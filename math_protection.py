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


_GREEK = r"Α-Ωα-ω"
_LATIN_OR_GREEK_LETTER_RE = re.compile(rf"[a-zA-Z{_GREEK}]")
_HALFWIDTH_OR_GREEK_CHAR = rf"\x21-\x7E{_GREEK}"


def find_untranslated_fragment_candidates(ja_text: str) -> list[str]:
    """翻訳後のja_textに半角のまま残っている断片のうち、未検出（未保護）の
    数式らしい候補をヒューリスティックに洗い出す（2026-08-09追加）。

    日本語訳では地の文はほぼ全て全角文字になる一方、DeepLが自然言語として
    翻訳しない固有名詞・型番・数式的表記は半角のまま残る傾向がある。この
    性質を利用し、$...$で保護済みの数式（protectで除外済み）を除いた
    残りのテキストから半角文字列の連続を抽出する。ギリシャ文字（α-ω,
    Α-Ω）も、半角英数字と同様にDeepLが翻訳せずそのまま残す数式記号
    （例:"scale γ"）であるため、検出対象に含める（2026-08-09追加）。

    ただし著者名・データセット名・略語（"FLUX"、"BSDS500"等）のような
    固有名詞も同様に半角のまま残るため、下記の条件で緩く絞り込む
    （find_unprotected_math_like_tokensより広い網だが、完全に誤検知を
    排除できるわけではない。人間が確認する前提のヒューリスティック）。
        - 少なくとも1つの英字またはギリシャ文字を含む（引用番号
          "[12, 13]"等の数字・記号だけの断片を除外するため）
        - かつ、4文字以下、または"()=_^{}"等の数式らしい記号を含む
          （"EasyControlEdge"のような長い固有名詞を除外するため）

    既知の限界（2026-08-09確認）: 上記の「英字を1つ以上含む」条件により、
    "$...$"で保護されていない裸の数字1つだけの数式漏れ（例:"to 0 with K
    discrete steps"の"0"）は検出できない。この条件を外すと、見出し番号
    ("1."）・節番号の参照("2.2"）・引用番号("[12]"）等、本文中に大量に
    存在する正当な半角の裸数字を誤検知してしまい実用にならないため、
    意図的な設計上のトレードオフとして残している。
    """
    protected, _ = protect(ja_text)
    candidates = []
    # 半角スペースを挟んだ"t = 1"のような表記も1つの断片としてまとめて
    # 拾うため、非空白の半角文字（またはギリシャ文字）で始まり非空白の
    # 半角文字（またはギリシャ文字）で終わる区間をマッチ対象にする
    # （前後の全角文字・改行では区切られる）。
    pattern = rf"[{_HALFWIDTH_OR_GREEK_CHAR}](?:[\x20-\x7E{_GREEK}]*[{_HALFWIDTH_OR_GREEK_CHAR}])?"
    for m in re.finditer(pattern, protected):
        token = m.group(0)
        if _TOKEN_RE.fullmatch(token):
            continue  # __MATH0__ 等のプレースホルダ自身は候補ではない
        if not _LATIN_OR_GREEK_LETTER_RE.search(token):
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


_SAFE_SINGLE_LETTER_RE = re.compile(r"^[a-zA-Z]$")


def _isolated_token_re(token: str) -> re.Pattern[str]:
    """半角英数字に前後を挟まれていない、tokenの孤立した出現箇所にのみ
    マッチする正規表現を作る（"\\b"はUnicodeの結合文字クラスの都合上、
    直前直後が日本語の場合に期待通り機能しないことがあるため使わない）。
    """
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])")


def protect_confirmed_single_letter_leaks(units, log=print) -> list[str]:
    """翻訳後に確認できた、未保護の半角アルファベット1文字だけの数式変数を、
    en_text・ja_text双方の該当箇所に直接$...$を挿入して保護する
    （2026-08-10追加）。

    find_untranslated_fragment_candidatesが検出する候補のうち、半角
    アルファベット1文字だけの候補（例:"x","K"）に限定して対象とする。
    実在の英単語（"I"や"a"等）であれば通常の翻訳で全角の日本語に置き
    換わり候補として残らないため、翻訳後も半角のまま生き残っている
    時点で数式変数である可能性が高いと判断できる（"DiT"や"NMS"のような
    複数文字のトークンは、実在の固有名詞・略語と区別できないため
    引き続き対象外のまま、report_untranslated_fragment_candidatesによる
    人間向けの情報ログのみで扱う）。

    DeepLへの再翻訳は行わない。1回目の翻訳で既に確定したen_text/
    ja_textの同一箇所に、後から$...$を直接挿入するだけの後処理である
    （該当トークンは翻訳前後で文字として変化していないことが前提のため、
    再翻訳する意味が無い）。1unit内にそのtokenが複数箇所出現する場合は
    区別せず全て保護する（現状の実データでは1unit内に同じ単体アルファ
    ベットが複数回登場するケースは確認されていないが、その場合は文脈を
    問わず一律で保護される点に注意）。

    既知の不具合修正（2026-08-10）: 当初、置換をen_text/ja_textへ直接
    適用していたが、これだと既に$...$で保護済みの数式スパン（例:
    "$p ( y \\mid x )$"）の内部にある"y"/"x"まで誤って二重に$で囲んで
    しまい、数式スパンが壊れる不具合があった（find_untranslated_
    fragment_candidates自体はprotect()経由で既存の$...$スパンを避けて
    候補を検出するが、検出したtokenを実際に置換する際にはprotect()を
    経由していなかったため）。置換前にen_text/ja_textそれぞれをprotect()
    で一時的にプレースホルダへ退避し、プレースホルダ化されたテキスト
    （$記号を含まない）に対して置換を行った上でrestore()すること
    で、既存の数式スパンの内部には触れないようにしている。

    Args:
        units: DocUnitのリスト（翻訳済み、en_text/ja_text設定済みのもの）。
        log: 情報メッセージを受け取るコールバック（既定はprint）。

    Returns:
        情報メッセージのリスト（1件も無ければ空リスト）。
    """
    messages: list[str] = []
    for unit in units:
        if not getattr(unit, "translatable", False):
            continue
        for token in find_untranslated_fragment_candidates(unit.ja_text):
            if not _SAFE_SINGLE_LETTER_RE.match(token):
                continue
            pattern = _isolated_token_re(token)

            en_protected, en_spans = protect(unit.en_text)
            ja_protected, ja_spans = protect(unit.ja_text)
            if not pattern.search(en_protected) or not pattern.search(ja_protected):
                continue
            replacement = f"${token}$"
            unit.en_text = restore(pattern.sub(replacement, en_protected), en_spans)
            unit.ja_text = restore(pattern.sub(replacement, ja_protected), ja_spans)
            message = (
                f"[情報] {unit.tag}: 単体アルファベット {token!r} を数式として"
                "$...$で保護しました（再翻訳は行っていません）。"
            )
            messages.append(message)
            log(message)
    return messages
