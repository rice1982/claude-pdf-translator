"""工程(3)「構造化・タグ処理」モジュール。

`[P1-S2-abstract-S2] text` のような行冒頭の識別タグ（:mod:`stage2` が
出力するタグ付きMarkdown由来）を本文から分離し、タグIDと本文の1対1対応を
保ったまま :class:`shared.DocUnit` の順序付きリストへ変換する。ページ構造・
章立てを一切解釈しない後続の翻訳ステップが、タグ種別（見出し／メタ情報／
本文文）だけを見て処理を分岐できるようにするのがこのモジュールの責務。

翻訳直前の仕上げ（``normalize`` による ``\\textless``/``\\textgreater`` 正規化、
``protect_units`` による翻訳対象unitの数式スパンのプレースホルダ退避）まで
行うが、退避・復元の実装そのもの（``protect``/``restore``）は
:mod:`mainCode.shared.shared` に委譲する（工程(6)の複数関数からも使われる
ため）。工程(3)全体の入口は ``prepare_translation_input`` で、``shared`` 経由の
``protect``/``restore`` を除き他のどの ``mainCode`` モジュールにも依存しない。
構成の詳細は ``doc/architecture/stage3.md`` を参照。
"""

from __future__ import annotations

import re
from pathlib import Path

from mainCode.shared.shared import DocUnit, protect, restore


# ============================================================================
# 解析（タグ付きMarkdownの各行を、行冒頭の識別タグから判定した kind 付きの
#   DocUnit の順序付きリストへ変換する）
# ============================================================================


# 行冒頭の`[tag] body`形式（タグ行）にマッチする。group(1)=タグ文字列、group(2)=本文。
_TAG_LINE_RE = re.compile(r"^\[([^\]]+)\]\s?(.*)$")
# `![tag](path) [tag]`形式（図表・数式の画像行）にマッチする。
# group(1)/group(3)=タグ文字列（同一のはず）、group(2)=画像パス。
_IMAGE_LINE_RE = re.compile(r"^!\[([^\]]+)\]\(([^)]+)\)\s\[([^\]]+)\]$")
# タグ文字列先頭の`P{page}-`からページ番号を取り出す。
_PAGE_NUM_RE = re.compile(r"^P(\d+)-")


# タグ文字列からDocUnit.kindを判定する。
# 上から順に判定し、最初に一致した種別を採用する優先順位付きのパターン
# マッチで、いずれにも一致しなければ"unknown"にフォールバックする。
def _classify(tag: str) -> str:
    if tag.endswith("-LATEX"):
        return "equation_latex"
    if re.fullmatch(r"P\d+-TITLE", tag):
        return "title"
    if re.fullmatch(r"P\d+-AUTHORS", tag):
        return "authors"
    if re.fullmatch(r"P\d+-AFFIL", tag):
        return "affil"
    if re.match(r"^P\d+-HEADING-", tag):
        return "heading"
    if re.match(r"^P\d+-UNKNOWN-", tag):
        return "unknown"
    if re.search(r"-CAPTION-S\d+$", tag):
        return "caption_sentence"
    if re.match(r"^P\d+-S\d+-", tag):
        return "body_sentence"
    return "unknown"


# 翻訳対象になりうる（実際の文を表す）kind。それ以外（authors/affil/
# equation_latex/figure_image/equation_image/unknown等）は原文のまま扱う。
# prepare_translation_inputの数式正規化（normalize）の対象判定にも同じ集合を使う
# （実際の文でなければ\textless/\textgreaterの正規化も不要なため）。
_TRANSLATABLE_KINDS = {"title", "heading", "body_sentence", "caption_sentence"}


# 1ページ分のタグ付きMarkdownファイルをDocUnitのリストへ変換する。
# 1行につき3通りの扱いをする。
# - 画像行（![tag](path) [tag]）: 図表・数式画像を表すDocUnitを追加する。
# - タグ行（[tag] body）: タグ文字列を_classifyで判定し、本文を持つDocUnitを
#   追加する。
# - それ以外の行（タグで始まらない行）: 新規unitにはせず、直前のDocUnitへの
#   継続として連結する（理由は該当箇所のコメント参照）。
def parse_page_file(path: Path) -> list[DocUnit]:
    units: list[DocUnit] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue

        m_img = _IMAGE_LINE_RE.match(line)
        if m_img:
            tag, image_path, _closing_tag = m_img.groups()
            page_match = _PAGE_NUM_RE.match(tag)
            if page_match is None:
                continue
            kind = "equation_image" if "-EQ" in tag else "figure_image"
            units.append(
                DocUnit(tag=tag, kind=kind, page=int(page_match.group(1)), image_rel_path=image_path)
            )
            continue

        m_tag = _TAG_LINE_RE.match(line)
        if not m_tag:
            # `[TAG]`で始まらない行は、直前のunitの本文が複数物理行に
            # またがって書き出された際の続き（例: UnknownElement.textが
            # MinerUのcode_body等、改行を含む生テキストをそのまま保持して
            # いた場合）とみなし、単純に読み飛ばさず直前unitへ半角スペース
            # 区切りで連結する。連結せず読み飛ばすと、2行目以降の本文が
            # 静かに失われる（実データのsample1.pdf、Appendix A.1の
            # プロンプト例文で発生を確認した回帰）。連結先が無い（ファイル
            # 先頭からタグ行ではない）場合は、従来通り無視する。
            if units:
                continuation = line.strip()
                last = units[-1]
                last.en_text = f"{last.en_text.rstrip()} {continuation}".strip()
                if not last.translatable:
                    last.ja_text = last.en_text
            continue
        tag, body = m_tag.groups()
        page_match = _PAGE_NUM_RE.match(tag)
        if page_match is None:
            continue
        kind = _classify(tag)
        translatable = kind in _TRANSLATABLE_KINDS
        units.append(
            DocUnit(
                tag=tag,
                kind=kind,
                page=int(page_match.group(1)),
                en_text=body,
                ja_text="" if translatable else body,
                translatable=translatable,
            )
        )
    return units


# 出力ディレクトリ直下のpage_*_en.mdをページ順に解析し、1つの順序付き
# リストへ結合する。
def parse_output_dir(output_dir: Path) -> list[DocUnit]:
    output_dir = Path(output_dir)

    def _page_number(path: Path) -> int:
        m = re.search(r"page_(\d+)_en", path.name)
        return int(m.group(1)) if m else 0

    files = sorted(output_dir.glob("page_*_en.md"), key=_page_number)

    units: list[DocUnit] = []
    for file_path in files:
        units.extend(parse_page_file(file_path))
    return units


# ============================================================================
# 文脈組み立て・除外（DeepL の context 引数用の文書文脈の組み立てと、
#   参考文献セクションの翻訳対象からの除外）
# ============================================================================


# re.compile()は、渡した正規表現パターン文字列をコンパイル済みのPatternオブジェクト
# に変換し、それを_REFERENCES_SLUG_REという名前に代入する。以降、
# `_REFERENCES_SLUG_RE.search(文字列)`のように呼び出すことで、その文字列の中に
# このパターンにマッチする箇所があるかを判定できる（あればMatchオブジェクト、
# 無ければNoneが返る）。
#
# ここでのパターン（大文字小文字を区別しない）は、スラッグ全体が"references"と
# 一致するか、"5.references"のようにドット区切りの最後の要素が"references"と
# 一致する場合にマッチする（"preferences"のように末尾が"references"という
# 文字列を含むだけのケースはマッチしない）。
_REFERENCES_SLUG_RE = re.compile(r"(?:^|\.)references$", re.IGNORECASE)


# unitが属する章のスラッグ（タグ中のセクション部分）を取り出す。
# heading/body_sentence以外のkindはセクション情報を持たないためNoneを
# 返す。exclude_references_sectionの参考文献判定にのみ使う内部ヘルパー。
def _section_slug(unit: DocUnit) -> str | None:
    if unit.kind == "heading":
        m = re.match(r"^P\d+-HEADING-(.+)$", unit.tag)
        return m.group(1) if m else None
    if unit.kind == "body_sentence":
        m = re.match(r"^P\d+-S\d+-(.+)-S\d+$", unit.tag)
        return m.group(1) if m else None
    return None


# 参考文献セクション（見出し・本文とも）を翻訳対象から除外する。
# 著者名等の固有名詞が翻訳エンジンによって書き換えられてしまうのを防ぐため、
# セクションスラッグが"references"の見出し・本文文は原文をそのままja_textに
# 設定し、翻訳対象から外す。
def exclude_references_section(units: list[DocUnit]) -> None:
    for unit in units:
        slug = _section_slug(unit)
        if slug and _REFERENCES_SLUG_RE.search(slug):
            unit.translatable = False
            unit.ja_text = unit.en_text


# 文脈情報①: タイトル＋Abstractをドキュメント全体の要約代わりとして構築する。
def build_document_context(units: list[DocUnit]) -> str:
    title = next((u.en_text for u in units if u.kind == "title"), "")
    abstract_sentences = [u.en_text for u in units if u.kind == "body_sentence" and "-abstract-" in u.tag]
    parts = [part for part in (title, " ".join(abstract_sentences)) if part]
    return "\n".join(parts)


# ============================================================================
# 仕上げ（翻訳直前の DocUnit を組み立てる工程(3)の最終ステップ）
# ============================================================================


# 数式スパンだけを正規化し、それ以外の本文はそのまま返す。
# protect（数式スパンの退避）→restore（復元）のラウンドトリップを利用して、
# MinerUが数式中に出力する\textlessと\textgreater（KaTeXが解釈できない
# テキストモード用の比較記号エスケープ）をそれぞれ<と>に変更する（実際の変換は
# protectが行う。詳細はmainCode/shared/shared.py参照）。文脈組み立て前の
# 本文正規化として、翻訳対象unitのen_textに適用する。
def normalize(text: str) -> str:
    protected, spans = protect(text)
    return restore(protected, spans)


# 翻訳対象unitごとにprotectを適用し、結果をunit.protected_en_text/
# unit.math_spansへ書き込む（工程3の終わりに一度だけ呼ぶ）。en_text自体は
# 変更しない。
# 翻訳エンジン（call_deepl）はここで設定された
# protected_en_text/math_spansをそのまま使い、自身では二度とprotectを
# 呼ばない。これにより「保護」は工程3の最終ステップ、「復元」
# （stage5.apply_restore）は工程5そのものという形で、工程間の境界が
# はっきりする。
def protect_units(units: list[DocUnit]) -> None:
    for unit in units:
        if unit.translatable:
            unit.protected_en_text, unit.math_spans = protect(unit.en_text)


# ============================================================================
# 入口（タグ付きMarkdownの解析→数式正規化→参考文献除外→文書文脈の組み立て→
#   数式スパンの保護をこの順で実行。工程(3)全体を代表する）
# ============================================================================


# 工程(3)全体を代表する入口。output_dir配下のタグ付きMarkdownを解析し、
# 数式正規化・参考文献除外・文書文脈の組み立て・数式保護（工程(3)の仕上げ）を
# 行った上で、翻訳対象のDocUnit列と文書文脈を返す。
# output_dir配下にpage_*_en.mdが1つも見つからない場合はSystemExitを送出する。
def prepare_translation_input(output_dir: Path) -> tuple[list[DocUnit], str]:
    # 1. 解析: output_dir配下のpage_*_en.mdをページ順に読み込み、DocUnitの
    #    順序付きリストへ変換する。
    units = parse_output_dir(output_dir)
    # 2. unitsが空のリストだったら（output_dir配下にpage_*_en.mdが1つも
    #    見つからなかった場合）、後続の全ステップが扱うデータが無い以上、
    #    ここで即座に打ち切る（フォールバックせず明確なエラーとして
    #    呼び出し元に伝える）。
    if not units:
        raise SystemExit(f"{output_dir} に page_*_en.md が見つかりませんでした。")
    # 3. 数式正規化: 翻訳対象になりうるkind（title/heading/body_sentence/
    #    caption_sentence）のen_textだけ、\textlessと\textgreaterをそれぞれ<と>に変換する。
    #    (逆に言うと、他の数式対象は対象外でそれらには何もしない)
    #    参考文献除外より先に行うことで、後段のprotect_unitsが扱うen_textは
    #    既に正規化済みの状態になる。
    for unit in units:
        if unit.kind in _TRANSLATABLE_KINDS:
            unit.en_text = normalize(unit.en_text)
    # 4. 参考文献セクション（見出し・本文とも）を翻訳対象から除外する。
    exclude_references_section(units)
    # 5. DeepLのcontext引数用に、タイトル+Abstractから文書全体の文脈を組み立てる。
    document_context = build_document_context(units)
    # 6. 工程(3)の最終ステップとして、翻訳対象unitごとに数式スパンを
    #    プレースホルダへ退避する（復元は工程(5)のapply_restoreが行う）。
    protect_units(units)
    return units, document_context
