"""工程(6)「数式保護」モジュール。

翻訳後に残った未保護の数式らしき断片の自動保護と、翻訳済みタグ付き
Markdownの書き出しをまとめている。工程(5)「翻訳後処理」（数式プレースホルダの
復元、:mod:`mainCode.stage5.stage5` の ``apply_restore``）が完了した後の
DocUnit列を入力として受け取る。

インライン数式（`$...$` / `$$...$$`）のプレースホルダ退避・復元の仕組み
（``protect``/``restore``）自体は:mod:`mainCode.shared.shared` に置かれて
いる（工程(3)の仕上げである``mainCode.stage3.stage3.protect_units``/
``normalize``と、この工程(6)側の複数関数の双方が、互いをimportし合う
ことなく使うため）。

工程(6)全体の入口は ``postprocess``。単体アルファベット変数の自動保護と
関数呼び出し／比較演算子付き数式断片の統合を決まった順序で実行した上で、
翻訳済みタグ付きMarkdownを書き出す（``write_translated_pages``。工程(3)の
``parse_page_file`` の逆変換だが、タグ書式の実装は共有せず、実行順に合わせて
ここに置いている）。翻訳エンジンとの送受信内容の記録保存はこの関数の責務では
なく、呼び出し元（:func:`mainCode.whole_pipeline.whole_pipeline.main`）が担う。
構成の詳細は ``doc/architecture/stage6.md`` を参照。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from mainCode.shared.shared import MATH_RE, DocUnit, filter_translatable_units, protect, restore


# ============================================================================
# 翻訳済みタグ付きMarkdownの書き出し（工程(3)の parse_page_file の逆変換）
# ============================================================================


# 翻訳済み（ja_text設定済み）のunitsを、page_*_en.mdと同じタグ形式で
# page_*_ja.mdとして書き出す。あわせて、翻訳後の後処理
# （protect_confirmed_single_letter_leaks等）でen_textが更新されている
# 場合に備え、page_*_en.mdも現在のen_textで上書きする（そうしないと、
# process_pdf（工程(2)）時点のen.mdが翻訳後の保護処理を反映しないまま
# になってしまう）。
#
# parse_page_file（:mod:`mainCode.stage3.stage3`）の逆変換にあたる。
# 翻訳結果を人間が目視確認しやすくする（PDFをレンダリングせずテキスト
# のまま原文と見比べられる）ほか、未保護の数式らしき断片を検出する
# 後続処理（本ファイル参照）の入力としても使う。
def write_translated_pages(units: list[DocUnit], output_dir: Path) -> list[Path]:
    output_dir = Path(output_dir)
    pages: dict[int, list[DocUnit]] = {}
    for unit in units:
        pages.setdefault(unit.page, []).append(unit)

    written: list[Path] = []
    for page_num in sorted(pages):
        en_lines: list[str] = []
        ja_lines: list[str] = []
        for unit in pages[page_num]:
            if unit.kind in ("figure_image", "equation_image"):
                image_line = f"![{unit.tag}]({unit.image_rel_path}) [{unit.tag}]"
                en_lines.append(image_line)
                en_lines.append("")
                ja_lines.append(image_line)
                ja_lines.append("")
            elif unit.kind == "equation_latex":
                en_lines.append(f"[{unit.tag}] {unit.en_text}")
                en_lines.append("")
                ja_lines.append(f"[{unit.tag}] {unit.ja_text}")
                ja_lines.append("")
            else:
                en_lines.append(f"[{unit.tag}] {unit.en_text}")
                ja_lines.append(f"[{unit.tag}] {unit.ja_text}")
        en_path = output_dir / f"page_{page_num:02d}_en.md"
        en_path.write_text("\n".join(en_lines) + "\n", encoding="utf-8")
        written.append(en_path)
        ja_path = output_dir / f"page_{page_num:02d}_ja.md"
        ja_path.write_text("\n".join(ja_lines) + "\n", encoding="utf-8")
        written.append(ja_path)
    return written


# ============================================================================
# 単体文字変数の事後保護（未保護の数式らしき断片の検出＋半角アルファベット1文字の
#   $...$ 保護。断片検出はほかに許可リスト突合テストの測定器も兼ねる）
# ============================================================================


_GREEK = r"Α-Ωα-ω"
_LATIN_OR_GREEK_LETTER_RE = re.compile(rf"[a-zA-Z{_GREEK}]")
_HALFWIDTH_OR_GREEK_CHAR = rf"\x21-\x7E{_GREEK}"


# 翻訳後のja_textに半角のまま残っている断片のうち、未検出（未保護）の
# 数式らしい候補をヒューリスティックに洗い出す。
#
# 日本語訳では地の文はほぼ全て全角文字になる一方、DeepLが自然言語として
# 翻訳しない固有名詞・型番・数式的表記は半角のまま残る傾向がある。この
# 性質を利用し、$...$で保護済みの数式を除いた残りのテキストから半角
# 文字列の連続を抽出する。保護済みスパンの除外は、shared.MATH_REで該当スパンを
# 直接空文字列に置換する方式で行う（プレースホルダ（例:"__MATH0__"）へ
# 置換する方式は使わない。隣接する2つの保護済みスパンの間が半角スペース
# 1つだけの場合（例:"$a$ $b$"）、プレースホルダ文字列同士が
# "__MATH0__ __MATH1__"のように連結されて1つの断片として抽出されてしまい、
# プレースホルダの"_"がここでの「数式らしい記号」の条件にも合致するため
# 誤検知の原因になる）。ギリシャ文字（α-ω, Α-Ω）も、半角英数字と同様に
# DeepLが翻訳せずそのまま残す数式記号（例:"scale γ"）であるため、検出
# 対象に含める。
#
# パターンを列挙するのではなく「全角の訳文中に半角の連続が生き残って
# いたら怪しい」という統計的な網で拾う。そのため"t = 1"・"O(n)"・型番の
# ように列挙しきれない数式的表記を一括で拾える代わりに、著者名・
# データセット名・略語（"FLUX"、"BSDS500"等）のような固有名詞も半角の
# まま残るため誤検知として含みうる。下記の条件で緩く絞り込む（完全に
# 誤検知を排除できるわけではない。人間が確認する前提のヒューリスティック）。
#     - 少なくとも1つの英字またはギリシャ文字を含む（引用番号
#       "[12, 13]"等の数字・記号だけの断片を除外するため）
#     - かつ、4文字以下、または"()=_^{}"等の数式らしい記号を含む
#       （"EasyControlEdge"のような長い固有名詞を除外するため）
#
# 既知の限界: 上記の「英字を1つ以上含む」条件により、"$...$"で保護され
# ていない裸の数字1つだけの数式漏れ（例:"to 0 with K discrete steps"の
# "0"）は検出できない。この条件を外すと、見出し番号("1."）・節番号の
# 参照("2.2"）・引用番号("[12]"）等、本文中に大量に存在する正当な半角の
# 裸数字を誤検知してしまい実用にならないため、意図的な設計上のトレード
# オフとして残している。
#
# 呼び出し元は2つ。本番のpostprocessでは
# protect_confirmed_single_letter_leaksが候補源として呼び、そのうち半角
# アルファベット1文字だけを自動保護に採用する。それ以外の候補（固有
# 名詞・型番等の誤検知を含む）は自動保護せず、無課金の回帰テスト
# test_untranslated_fragment_candidates_against_cached_deepl_output
# （cache/配下の実翻訳結果へ本関数を直接適用し、人間確認済みの許可
# リストに無い候補が出たら失敗する）と人間の目視確認に
# 委ねる（CLAUDE.md「テスト・実行運用規定」項目4）。
def find_untranslated_fragment_candidates(ja_text: str) -> list[str]:
    protected = MATH_RE.sub("", ja_text)
    candidates = []
    # 半角スペースを挟んだ"t = 1"のような表記も1つの断片としてまとめて
    # 拾うため、非空白の半角文字（またはギリシャ文字）で始まり非空白の
    # 半角文字（またはギリシャ文字）で終わる区間をマッチ対象にする
    # （前後の全角文字・改行では区切られる）。
    pattern = rf"[{_HALFWIDTH_OR_GREEK_CHAR}](?:[\x20-\x7E{_GREEK}]*[{_HALFWIDTH_OR_GREEK_CHAR}])?"
    for m in re.finditer(pattern, protected):
        token = m.group(0)
        if not _LATIN_OR_GREEK_LETTER_RE.search(token):
            continue
        if len(token) <= 4 or re.search(r"[()=_^{}]", token):
            candidates.append(token)
    return candidates


_SAFE_SINGLE_LETTER_RE = re.compile(r"^[a-zA-Z]$")


# tokenの前後に対にならない丸括弧が1つだけ付いている場合、それを剥がす。
#
# 地の文の丸括弧書き（例:"...patchifying x)."）の中に単体アルファベット
# の数式変数があると、find_untranslated_fragment_candidatesの断片抽出が
# 直前・直後の"("・")"だけを巻き込んで"(x"や"Z)"のような2文字の候補に
# してしまうことがある（開き括弧と閉じ括弧が離れた位置にあり、候補の
# 範囲内には片方しか入らないため）。
#
# 一方"(i)"のように開き・閉じ括弧の両方がtoken内に揃っている場合は、
# "(a)"/"(i)"のような列挙記号である可能性が高いため区別する必要がある
# （実データで確認済み: sample0.pdf/sample1.pdfの"(i)"）。そのため、
# token内に開き・閉じ括弧の両方が既に揃っている場合は何もしない
# （片側だけが孤立している場合に限って剥がす）。
def _strip_unpaired_paren(token: str) -> str:
    has_open = "(" in token
    has_close = ")" in token
    if token.startswith("(") and not has_close:
        token = token[1:]
    if token.endswith(")") and not has_open:
        token = token[:-1]
    return token


# 半角英数字に前後を挟まれていない、tokenの孤立した出現箇所にのみ
# マッチする正規表現を作る（"\\b"はUnicodeの結合文字クラスの都合上、
# 直前直後が日本語の場合に期待通り機能しないことがあるため使わない）。
def _isolated_token_re(token: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])")


# 翻訳後に確認できた、未保護の半角アルファベット1文字だけの数式変数を、
# en_text・ja_text双方の該当箇所に直接$...$を挿入して保護する。
#
# units: DocUnitのリスト（翻訳済み、en_text/ja_text設定済みのもの）。
# log: 情報メッセージを受け取るコールバック（既定はprint）。
# 戻り値: 情報メッセージのリスト（1件も無ければ空リスト）。
#
# find_untranslated_fragment_candidatesが検出する候補のうち、半角
# アルファベット1文字だけの候補（例:"x","K"）に限定して対象とする。
# 実在の英単語（"I"や"a"等）であれば通常の翻訳で全角の日本語に置き
# 換わり候補として残らないため、翻訳後も半角のまま生き残っている
# 時点で数式変数である可能性が高いと判断できる（"DiT"や"NMS"のような
# 複数文字のトークンは、実在の固有名詞・略語と区別できないため対象外
# とし、翻訳結果の目視確認（CLAUDE.md「テスト・実行運用規定」項目4）
# に委ねる）。
#
# 単体アルファベット1文字かどうかの判定前に_strip_unpaired_parenを
# 適用し、地の文の丸括弧が偶然巻き込まれた候補（例:"(x"、"Z)"）から
# 括弧だけを剥がした上で判定する（"(i)"のような列挙記号は開き・閉じ
# 両方の括弧が揃っているため区別され、対象外のまま）。
#
# DeepLへの再翻訳は行わない。1回目の翻訳で既に確定したen_text/
# ja_textの同一箇所に、後から$...$を直接挿入するだけの後処理である
# （該当トークンは翻訳前後で文字として変化していないことが前提のため、
# 再翻訳する意味が無い）。1unit内にそのtokenが複数箇所出現する場合は
# 区別せず全て保護する（現状の実データでは1unit内に同じ単体アルファ
# ベットが複数回登場するケースは確認されていないが、その場合は文脈を
# 問わず一律で保護される点に注意）。
#
# 置換前にen_text/ja_textそれぞれをprotect()で一時的にプレースホルダへ
# 退避し、プレースホルダ化されたテキスト（$記号を含まない）に対して
# 置換を行った上でrestore()する。理由: 置換を直接en_text/ja_textへ適用
# すると、既に$...$で保護済みの数式スパン（例:"$p ( y \\mid x )$"）の
# 内部にある"y"/"x"まで誤って二重に$で囲んでしまい、数式スパンが壊れる
# （find_untranslated_fragment_candidates自体はprotect()経由で既存の
# $...$スパンを避けて候補を検出するが、検出したtokenをテキストへ実際に
# 置換する処理はこれとは別なので、同じくprotect()を経由させる必要が
# ある）。
def protect_confirmed_single_letter_leaks(units, log=print) -> list[str]:
    messages: list[str] = []
    for unit in filter_translatable_units(units):
        for raw_token in find_untranslated_fragment_candidates(unit.ja_text):
            token = _strip_unpaired_paren(raw_token)
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


# ============================================================================
# 断片統合の共通ヘルパー（関数呼び出し版・比較演算子版が共有する
#   「検出→適用→ログ出力」の骨格）
# ============================================================================


# ja_text上で検出された(元の文字列, 置換後の文字列)のペアを、ja_text
# には必ず適用し、en_textには同じ文字列がそのまま見つかった場合のみ
# 適用する（見つからなければen_textには触れない）。戻り値は
# (新しいen_text, 新しいja_text, 何らかの置換が行われたか)。
#
# ja_text側での検出結果を、パターンとしてではなく具体的な文字列一致
# としてen_textへ機械的に反映する設計（protect_confirmed_single_
# letter_leaksと同じ方針）。en_text上で同じ正規表現を独立に再実行
# しない理由は、翻訳後のja_textは大半が全角文字であることを前提に
# ヒューリスティックの誤爆を抑えているのに対し、翻訳されない（＝原文
# のまま半角英字が続く）en_textではその前提が成り立たず、同じ
# ヒューリスティックが暴走しうるため。
def _apply_ja_detected_replacements(
    en_text: str, ja_text: str, replacements: list[tuple[str, str]]
) -> tuple[str, str, bool]:
    new_en, new_ja = en_text, ja_text
    changed = False
    for old, new in replacements:
        if old in new_ja:
            new_ja = new_ja.replace(old, new, 1)
            changed = True
        if old in new_en:
            new_en = new_en.replace(old, new, 1)
    return new_en, new_ja, changed


# find_replacements(ja_text)で見つけた置換をunit.en_text/ja_textへ
# _apply_ja_detected_replacements経由で適用し、変化があったunitごとに
# message_for(unit)のメッセージを記録・ログ出力する。
#
# merge_function_call_math_spans/merge_comparison_math_spansの共通の
# 「検出→適用→ログ出力」骨格をまとめたもの（差分は検出関数とメッセージ
# 文言のみ）。
def _apply_replacement_finder(units, find_replacements, message_for, log):
    messages: list[str] = []
    for unit in filter_translatable_units(units):
        replacements = find_replacements(unit.ja_text)
        if not replacements:
            continue
        new_en, new_ja, changed = _apply_ja_detected_replacements(unit.en_text, unit.ja_text, replacements)
        if not changed:
            continue
        unit.en_text = new_en
        unit.ja_text = new_ja
        message = message_for(unit)
        messages.append(message)
        log(message)
    return messages


# ============================================================================
# 関数呼び出し断片の統合（"P($A$, $B$)" → "$P(A, B)$"）
# ============================================================================


_FUNC_CALL_WITH_MATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z]{1,4})\(((?:\$[^$\n]+\$|[A-Za-z0-9,._+\-|'’\s])+?)\)"
)


# text上で_FUNC_CALL_WITH_MATH_REにマッチした断片ごとに、
# (元の文字列, まとめ直した後の文字列)のペアを列挙する。テキストへの
# 書き換えはここでは行わない（呼び出し側でja_text/en_textへの反映方法
# を制御するため）。
def _find_function_call_math_replacements(text: str) -> list[tuple[str, str]]:
    replacements: list[tuple[str, str]] = []
    for m in _FUNC_CALL_WITH_MATH_RE.finditer(text):
        name, args = m.group(1), m.group(2)
        if "$" not in args:
            continue
        unwrapped = re.sub(r"\$([^$\n]+)\$", r"\1", args)
        replacements.append((m.group(0), f"${name}({unwrapped})$"))
    return replacements


# "P($A$, $B$)"のように、関数呼び出し記法の引数だけが個別に$...$で
# 保護されている箇所を、識別子・括弧を含めた1つの$...$（例:"$P(A, B)$"）
# にまとめ直す。
#
# units: DocUnitのリスト（翻訳済み、en_text/ja_text設定済みのもの）。
# log: 情報メッセージを受け取るコールバック（既定はprint）。
# 戻り値: 情報メッセージのリスト（1件も無ければ空リスト）。
#
# MinerUの数式検出は、"P(A, B)"のような関数呼び出し表記中の変数
# （"A"・"B"）だけを個別の数式領域として認識し、"P("や")"は地の文として
# 残すことがある。この場合protect/restoreを経ても"A"・"B"はそれぞれ独立
# した$...$のまま残り、find_untranslated_fragment_candidates／
# protect_confirmed_single_letter_leaksは"$...$"で既に保護済みの断片と
# しては扱わない（＝正しく保護されているとみなす）ため、"P("・")"だけが
# 地の文として残った不自然な組版（"P"と"()"はテキストフォント、
# "A"・"B"だけ数式フォントで描画される）になりうる。本関数はこの後処理
# として、識別子（英字1〜4文字。長い固有名詞・英単語との誤検知を避ける
# ため上限を設けている）に空白なしで直接続く"("から、対応する")"までの
# 区間に、既に$...$で保護済みのスパンが最低1つ含まれる場合にのみ発火
# する。発火条件を「protect_confirmed_single_letter_leaks等が既に保護
# 済みと確定させた断片を含むこと」に限定しているため、$...$を含まない
# 通常の地の文には一切マッチせず、既存の検知・保護結果を壊さない。
# 引数部は、カンマ区切りの列挙（"P(A, B)"）に加え、条件付き確率の"|"
# 区切り表記（"P(A | B)"）、プライム記号（直立アポストロフィ"'"・
# Unicodeの右シングルクォーテーションマーク"'"の両方。"P(X'|Y)"の
# ような更新後の値を表す表記）を含む文字列にも対応する。
#
# 識別子と"("の間に空白を要求しない（"P("は対象だが"call ("は対象外）
# ことが、通常の英語散文の丸括弧書き（"call (see also)"のように"("の前に
# 空白を置くのが一般的）と数式の関数呼び出し記法を区別する主な手がかりに
# なっている。ただし完全な区別ではないため、本関数もあくまでヒューリス
# ティックであり、messagesとしてログに残った箇所は人間が目視確認する
# ことを推奨する。
#
# 検出はja_textに対してのみ行い、見つかった具体的な文字列（パターンでは
# なく1件ごとの実際のマッチ文字列）が、en_textにもそのまま存在する場合
# にのみen_text側へ同じ置換を反映する（見つからなければen_textには
# 触れない。protect_confirmed_single_letter_leaksと同じ方針）。
# en_text上で同じ正規表現を独立に再実行しない理由は、この関数の引数用
# 文字クラスが英字・空白・カンマ等をほぼ無制限に許容するほど緩く、
# 「翻訳後のja_textは大半が全角文字である」という前提のもとでしか
# 誤爆が実用上抑えられないため。半角英字の地の文が延々と続くen_textに
# 同じ検出を独立に行うと、この前提が成り立たず、無関係な後続の英文まで
# 1つの$...$に巻き込んでしまう（実際にsample2.pdfの実データで確認された
# 不具合）。
#
# DeepLへの再翻訳は行わない。ja_textは既に確定した文字列に対して置換
# するだけの後処理であり、en_textはja_text側の検出結果をそのまま
# 文字列一致で反映するだけである。
#
# 既知の限界:
#     - 識別子は英字のみ（ギリシャ文字・添字付き識別子は対象外）
#     - 入れ子の関数呼び出し（例:"f(g($x$))"）は想定していない
#     - 引数部に$...$を含まない関数呼び出し（例:"P(A, B)"がそもそも
#       未保護のまま）は対象外（この関数は「まとめ直す」ことだけを行い、
#       新規の保護は行わない）
#     - ハイフン区切りの複合識別子（例:"sub-loss("）は、識別子直前条件が
#       "_"のみを除外対象としているため、ハイフンより後ろの部分
#       （"loss("）だけが識別子として拾われ、"sub-"が地の文に取り残される
#       可能性がある（"D_KL("のようなアンダースコア区切りのケースは
#       直前条件に"_"を含めることで対策済み）
#     - ja_textで検出された文字列がen_textに一字一句一致しない場合
#       （DeepLが識別子周辺の表記をわずかに変えた等）、en_text側は
#       未保護のまま残る
def merge_function_call_math_spans(units, log=print) -> list[str]:
    return _apply_replacement_finder(
        units,
        _find_function_call_math_replacements,
        lambda unit: (
            f"[情報] {unit.tag}: 関数呼び出し形式の数式断片（例:\"P($A$, $B$)\"）を"
            "1つの$...$にまとめ直しました。"
        ),
        log,
    )


# ============================================================================
# 比較演算子断片の統合（"$K$ > 1" → "$K > 1$"）
# ============================================================================


_COMPARISON_AFTER_MATH_RE = re.compile(
    r"\$([^$\n]+)\$(\s*(?:>=|<=|==|!=|≥|≤|≠|>|<)\s*(?:-?\d+(?:\.\d+)?|\$[^$\n]+\$))"
)


# text上で_COMPARISON_AFTER_MATH_REにマッチした断片ごとに、
# (元の文字列, まとめ直した後の文字列)のペアを列挙する。テキストへの
# 書き換えはここでは行わない（呼び出し側でja_text/en_textへの反映方法
# を制御するため）。
def _find_comparison_math_replacements(text: str) -> list[tuple[str, str]]:
    replacements: list[tuple[str, str]] = []
    for m in _COMPARISON_AFTER_MATH_RE.finditer(text):
        left, rest = m.group(1), m.group(2)
        rest = re.sub(r"\$([^$\n]+)\$", r"\1", rest)
        replacements.append((m.group(0), f"${left}{rest}$"))
    return replacements


# "$K$ > 1"のように、既存の$...$スパンの直後に比較演算子（>, <, >=,
# <=, ==, !=, ≥, ≤, ≠）と数値または別の$...$スパンが地の文として続く
# 箇所を、1つの$...$（例:"$K > 1$"）にまとめ直す。
#
# units: DocUnitのリスト（翻訳済み、en_text/ja_text設定済みのもの）。
# log: 情報メッセージを受け取るコールバック（既定はprint）。
# 戻り値: 情報メッセージのリスト（1件も無ければ空リスト）。
#
# MinerUの数式検出は、"$K$ > 1"のように変数（"K"）だけを数式領域として
# 認識し、続く比較演算子と値（"> 1"）は地の文として残すことがある
# （merge_function_call_math_spansの関数呼び出し版と同種の問題）。
# この場合、比較演算子や数値は自然言語の単語ではないためDeepLの翻訳
# 対象としてもほぼそのまま残るが、$...$で保護されないままだと
# find_untranslated_fragment_candidatesの報告対象からも外れてしまう
# （英字を1つ以上含むことを要求するため、"> 1"のように英字を含まない
# 断片は拾えない）。本関数は
# そうした検知漏れを、既存の$...$スパンとの隣接関係を手がかりに
# ソース側で自動保護することで解消する。
#
# 右辺（比較対象の値）を「数値」または「既に$...$で保護済みのスパン」に
# 限定しているため、"$X$ > many models"のような自然文（右辺が単語）や、
# "$X$</sup>"のようなHTML上付きタグ（"<sup>"は数字でも$でもなく文字
# 始まりのため対象外）には一致しない。
#
# 検出はja_textに対してのみ行い、見つかった具体的な文字列が、en_text
# にもそのまま存在する場合にのみen_text側へ同じ置換を反映する（見つ
# からなければen_textには触れない。protect_confirmed_single_letter_
# leaks・merge_function_call_math_spansと同じ方針）。この関数自体の
# 右辺限定（数値/既存の$...$スパンのみ）によりen_text上で独立実行して
# も誤爆リスクは低いが、"翻訳後のja_textは大半が全角文字である"という
# 前提に頼らない設計へ統一するため、他の後処理関数と同じ方式を採用
# している。
#
# DeepLへの再翻訳は行わない。ja_textは既に確定した文字列に対して置換
# するだけの後処理であり、en_textはja_text側の検出結果をそのまま
# 文字列一致で反映するだけである。
#
# 既知の限界:
#     - 逆方向（値が先、$...$スパンが後に続く"1 < $K$"等）は対象外
#       （実データで確認できていないパターンのため、あえて対応しない）
#     - 演算子の前後に半角スペースが無い場合（"$K$>1"）や複数個ある
#       場合も対象になるが、比較対象が全角文字を挟む場合は対象外
#     - ja_textで検出された文字列がen_textに一字一句一致しない場合、
#       en_text側は未保護のまま残る
def merge_comparison_math_spans(units, log=print) -> list[str]:
    return _apply_replacement_finder(
        units,
        _find_comparison_math_replacements,
        lambda unit: (
            f"[情報] {unit.tag}: 比較演算子付きの数式断片（例:\"$K$ > 1\"）を"
            "1つの$...$にまとめ直しました。"
        ),
        log,
    )


# ============================================================================
# 入口（apply_restore適用後の自動保護・書き出しを決まった順序で実行）
# ============================================================================


# apply_restore適用後のunitsに対し、単体アルファベット変数の自動保護
# （protect_confirmed_single_letter_leaks）・関数呼び出し／比較演算子付き
# 数式断片の統合（merge_function_call_math_spans・merge_comparison_math_
# spans）を決まった順序でまとめて実行した上で、翻訳済みタグ付きMarkdownを
# 書き出す（write_translated_pages）。
#
# units: apply_restore適用済みのDocUnitの列。
# output_dir: write_translated_pagesの書き出し先。
# log: ログメッセージを受け取るコールバック（既定はprint）。
# 戻り値: write_translated_pagesが書き出したファイルパスの一覧
# （page_XX_en.md / page_XX_ja.md）。
#
# 数式の復元（apply_restore）は工程(5)「翻訳後処理」
# （:mod:`mainCode.stage5.stage5`）の責務で、呼び出し元がこの関数より
# 前に別途行う。翻訳エンジンとの送受信内容の記録保存（cache/配下への
# スナップショット保存）もこの関数の責務ではなく、呼び出し元が担う。
#
# 「英字1文字 = 値」形式（例:"t = 1"）や地の文の裸のギリシャ文字の
# 自動保護は、翻訳前の工程(3)（:func:`mainCode.stage2.stage2.
# wrap_bare_letter_equals_expressions` / ``wrap_bare_greek_letters``）で
# 済ませてあるため、この関数では扱わない。
def postprocess(
    units: list[DocUnit],
    output_dir: str | Path,
    log: Callable[[str], None] = print,
) -> list[Path]:
    # 1. 単体アルファベット変数の自動保護。既存の$...$スパンに隣接して
    #    いなくても、地の文に裸で残った1文字を新たに$...$で囲める唯一の
    #    ステップ（ステップ2-3は既存スパンに隣接する断片しか統合しない）。
    #    この関数群で最初にen_text/ja_textを書き換え、以降のステップ2-3は
    #    ここでの保護結果も反映済みのテキストに対して動作する。
    protected_letters = protect_confirmed_single_letter_leaks(units, log=log)
    if protected_letters:
        log(f"[数式チェック] 単体アルファベットの数式変数を{len(protected_letters)}件、自動的に$...$で保護しました（上記参照）。")

    # 2. 関数呼び出し断片の統合。既存の$...$スパンを引数に含む
    #    "P($A$, $B$)" 形式を、識別子・括弧ごと1つの$...$（"$P(A, B)$"）へ
    #    まとめ直す。ステップ1で新たに保護されたスパンも対象になりうる
    #    ため、1の後に実行する。
    merged_calls = merge_function_call_math_spans(units, log=log)
    if merged_calls:
        log(f"[数式チェック] 関数呼び出し形式の数式断片を{len(merged_calls)}件、1つの$...$にまとめ直しました（上記参照）。")

    # 3. 比較演算子断片の統合。既存の$...$スパン直後に地の文として続く
    #    "$K$ > 1" 形式を、1つの$...$（"$K > 1$"）へまとめ直す
    #    （ステップ2と対になる後処理。同じくステップ1の後に実行する）。
    merged_comparisons = merge_comparison_math_spans(units, log=log)
    if merged_comparisons:
        log(f"[数式チェック] 比較演算子付きの数式断片を{len(merged_comparisons)}件、1つの$...$にまとめ直しました（上記参照）。")

    # 4. 上記すべての自動保護・統合が反映された最終状態を書き出す
    #    （書き出しを最後にすることで、途中のen_text/ja_text書き換えを
    #    漏れなく反映する）。
    return write_translated_pages(units, output_dir)
