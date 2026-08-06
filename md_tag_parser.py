"""タグ付き構造化Markdown（pdf_processor.pyの出力）の解析モジュール。

`[P1-S2-abstract-S2] text` のような行冒頭の識別タグを本文から分離し、
タグIDと本文の1対1対応を保ったまま :class:`translation_models.DocUnit` の
順序付きリストへ変換する。ページ構造・章立てを一切解釈しない後続の翻訳
ステップが、タグ種別（見出し／メタ情報／本文文）だけを見て処理を分岐
できるようにするのがこのモジュールの責務。
"""

from __future__ import annotations

import re
from pathlib import Path

from translation_models import DocUnit

_TAG_LINE_RE = re.compile(r"^\[([^\]]+)\]\s?(.*)$")
_IMAGE_LINE_RE = re.compile(r"^!\[([^\]]+)\]\(([^)]+)\)\s\[([^\]]+)\]$")
_PAGE_NUM_RE = re.compile(r"^P(\d+)-")


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


_TRANSLATABLE_KINDS = {"title", "heading", "body_sentence", "caption_sentence"}


def parse_page_file(path: Path) -> list[DocUnit]:
    """1ページ分のMarkdownファイルを :class:`DocUnit` のリストへ変換する。"""
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


def parse_output_dir(output_dir: Path) -> list[DocUnit]:
    """出力ディレクトリ直下の `page_*_en.md` をページ順に解析し、1つの順序付き
    リストへ結合する。"""
    output_dir = Path(output_dir)

    def _page_number(path: Path) -> int:
        m = re.search(r"page_(\d+)_en", path.name)
        return int(m.group(1)) if m else 0

    files = sorted(output_dir.glob("page_*_en.md"), key=_page_number)

    units: list[DocUnit] = []
    for file_path in files:
        units.extend(parse_page_file(file_path))
    return units


_REFERENCES_SLUG_RE = re.compile(r"(?:^|\.)references$", re.IGNORECASE)


def _section_slug(unit: DocUnit) -> str | None:
    if unit.kind == "heading":
        m = re.match(r"^P\d+-HEADING-(.+)$", unit.tag)
        return m.group(1) if m else None
    if unit.kind == "body_sentence":
        m = re.match(r"^P\d+-S\d+-(.+)-S\d+$", unit.tag)
        return m.group(1) if m else None
    return None


def exclude_references_section(units: list[DocUnit]) -> None:
    """参考文献セクション（見出し・本文とも）を翻訳対象から除外する。

    著者名等の固有名詞が翻訳エンジンによって書き換えられてしまうのを防ぐため、
    セクションスラッグが "references" の見出し・本文文は原文をそのまま
    ``ja_text`` に設定し、翻訳対象から外す。
    """
    for unit in units:
        slug = _section_slug(unit)
        if slug and _REFERENCES_SLUG_RE.search(slug):
            unit.translatable = False
            unit.ja_text = unit.en_text


def build_document_context(units: list[DocUnit]) -> str:
    """文脈情報①: タイトル＋Abstractをドキュメント全体の要約代わりとして構築する。"""
    title = next((u.en_text for u in units if u.kind == "title"), "")
    abstract_sentences = [u.en_text for u in units if u.kind == "body_sentence" and "-abstract-" in u.tag]
    parts = [part for part in (title, " ".join(abstract_sentences)) if part]
    return "\n".join(parts)
