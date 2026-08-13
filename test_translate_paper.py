"""新テストスイート。

test_translator.py / test_mineru_cache.py を将来的に破棄し、こちらへ
テストを移植・追加していく前提のファイル。explain.txtの「■ 工程」で
定義された6工程（(1)ページ範囲の決定 / (2)PDF解析＝MinerU実行 /
(3)構造化・タグ処理 / (4)翻訳実行＝DeepL呼び出し / (5)翻訳後処理 /
(6)PDF生成）に対応する形でテストスイートを分割する。

各工程の境界は「外部の重い処理（MinerU・DeepL）を実際に再実行しないと
検証できない工程」と「合成データ・自作データだけで独立テストできる
工程」を分ける実務的な境界線でもある（explain.txtの「■ 工程」参照）。
6工程中、実行必須（モック不可）なのは(2)と(4)のみで、(2)はsubprocess、
(4)はdeepl.Translatorをそれぞれ差し替えて実MinerU・実DeepLを一切
起動せずに検証する。CLAUDE.mdの規定により、DeepLの有料APIは本ファイル
では一切呼び出さない。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import fitz  # PyMuPDF
import pytest
from dotenv import load_dotenv
from PIL import Image

import deepl_translator
import mineru_cache
import pdf_mineru_runner
from deepl_translator import (
    RawDeeplResult,
    TranslationBackendError,
    apply_restore,
    build_deepl_requests,
    call_deepl,
)
from math_protection import (
    check_unprotected_math_survival,
    find_untranslated_fragment_candidates,
    find_unprotected_math_like_tokens,
    normalize as normalize_math,
    protect,
    protect_confirmed_single_letter_leaks,
    report_untranslated_fragment_candidates,
    restore,
)
from md_tag_parser import (
    build_document_context,
    exclude_references_section,
    parse_output_dir,
    parse_page_file,
    write_translated_pages,
)
from pdf_chapter_resolver import ChapterResolutionError, parse_chapter_spec, resolve_chapter_page_range
from pdf_document_builder import build_document
from pdf_models import (
    CaptionElement,
    FigureElement,
    HeadingElement,
    LabeledElement,
    PageContent,
    StructuredDocument,
    TextBlockElement,
    TranslationUnit,
    UnknownElement,
)
from pdf_page_label_resolver import PageLabelResolutionError, resolve_physical_page, resolve_physical_page_range
from pdf_processor import process_pdf
from pdf_renderer import build_blocks, render_all_pdfs
from pdf_structure_analyzer import analyze_structure
from pdf_text_utils import (
    HEADING_RE,
    parse_caption_label,
    slugify_section_name,
    split_sentences,
    wrap_bare_greek_letters,
    wrap_bare_letter_equals_expressions,
)
from pdf_translator import translate_units
import translate_paper
from translate_paper import resolve_page_range
from translation_models import DocUnit

# テスト用サンプルのパス
SAMPLE_PDF_PATH = Path("input/sample0.pdf")
SAMPLE1_PDF_PATH = Path("input/sample1.pdf")
SAMPLE2_PDF_PATH = Path("input/sample2.pdf")
SAMPLE3_PDF_PATH = Path("input/sample3.pdf")

SENTENCE_ID_RE = re.compile(r"^\[P(\d+)-S(\d+)-([A-Za-z0-9.]+)-S(\d+)\] (.+)$")
FIGURE_ID_RE = re.compile(r"^!\[P(\d+)-FIG(\d+)\]\(images/(fig_p\d+_\d+\.png)\) \[P(\d+)-FIG(\d+)\]$")
HEADING_ID_RE = re.compile(r"^\[P(\d+)-HEADING-([A-Za-z0-9.]+)\] (.+)$")
CAPTION_ID_RE = re.compile(r"^\[P(\d+)-FIG(\d+)-CAPTION-S(\d+)\] (.+)$")
EQUATION_ID_RE = re.compile(r"^!\[P(\d+)-EQ(\d+)\]\(images/(eq_p\d+_\d+\.png)\) \[P(\d+)-EQ(\d+)\]$")
TABLE_FIG_ID_RE = re.compile(r"^!\[P(\d+)-TABLE(\d+)\]\(images/(table_p\d+_\d+\.png)\) \[P(\d+)-TABLE(\d+)\]$")
TABLE_CAPTION_ID_RE = re.compile(r"^\[P(\d+)-TABLE(\d+)-CAPTION-S(\d+)\] (.+)$")

# 実サンプルPDFを対象にした「工程横断の実データ・スモークテスト」で共通
# 利用するシナリオ一覧。sample0〜3すべてを列挙しておくことで、対象PDFを
# 後から差し替え・追加しやすくする（sample2/3は現時点でMinerU実行結果
# こそキャッシュ済みだが、CLAUDE.mdの規定により実DeepLキャッシュを持たない
# ため、DeepL翻訳結果を必要とするテストでは自動的にskipされる）。
# start_page/end_pageは1始まり（process_pdfと同じ）。sample3.pdfは
# CLAUDE.mdの運用規定により全件処理が禁止されているため、既存のスイートF
# 実データ確認と同じ最小範囲（印刷ページラベル55〜60＝物理67〜72）に限定する。
#
# run_idは、mineru_cache.py が実際に使うキャッシュフォルダ名
# （cache/<run_id>/mineru_cache/）と完全に一致させてある。range_labelは
# mineru_cache._run_id()に渡す人間可読な範囲記述子（translate_paper.
# describe_page_range()が生成するのと同じ形式）で、run_id自体も
# "<stem>_<range_label>"の形（例:"sample3_label55-60"）になる
# （"sample3"単体ではない点に注意。pytestの-kは部分一致なので
# `-k sample3`でも選択できる）。工程(5)(6)のrun_id（real_deepl_output
# 用）とは別のキャッシュ系統だが、「run_idで選ぶ」という考え方は統一する。
_REAL_SAMPLE_SCENARIOS = [
    pytest.param(SAMPLE_PDF_PATH, None, None, "sample0_full", "full", id="sample0_full"),
    pytest.param(SAMPLE1_PDF_PATH, None, None, "sample1_full", "full", id="sample1_full"),
    pytest.param(SAMPLE2_PDF_PATH, None, None, "sample2_full", "full", id="sample2_full"),
    pytest.param(SAMPLE3_PDF_PATH, 67, 72, "sample3_label55-60", "label55-60", id="sample3_label55-60"),
]


def _make_blank_pdf(path: Path, page_count: int = 1) -> None:
    doc = fitz.open()
    for _ in range(page_count):
        doc.new_page()
    doc.save(str(path))
    doc.close()


def _make_source_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), color=(200, 50, 50)).save(path)


def _find_latest_real_deepl_cache(run_id: str) -> Path | None:
    """cache/<run_id>/real_deepl_output_<タイムスタンプ>/ に一致するフォルダ
    のうち、タイムスタンプが最も新しいものを1つ返す（1件も無ければNone）。

    cache/<run_id>/ はmineru_cache.pyが実際に使うキャッシュフォルダ
    （cache/<run_id>/mineru_cache/）と同じ親ディレクトリで、real_deepl_
    output_<タイムスタンプ>/はそのきょうだいとして置かれる。実DeepLを
    呼ぶ処理（test_run_pipeline_end_to_end_with_real_deepl、または
    translate_paper.pyでの本実行）は、同じrun_idで再実行しても過去の
    実行結果を上書きしないよう、実行のたびにタイムスタンプ付きの新しい
    フォルダへ書き込む。タイムスタンプは"%Y%m%d-%H%M%S"形式
    （例:"20260813-153045"）で、辞書順ソートがそのまま時系列順になるため、
    文字列としてソートするだけで最新版を特定できる。
    """
    run_dir = Path(__file__).resolve().parent / "cache" / run_id
    if not run_dir.is_dir():
        return None
    candidates = sorted(run_dir.glob("real_deepl_output_*"))
    return candidates[-1] if candidates else None


# ============================================================================
# 工程(1): ページ範囲の決定
#   対応関数: parse_chapter_spec / resolve_chapter_page_range /
#     resolve_physical_page / resolve_physical_page_range /
#     translate_paper.resolve_page_range / translate_paper._require_pdf_exists
#   fitzで組み立てる合成PDFのみで完結し、MinerU・DeepLを一切使わない
#   （sample3.pdfを使う2テストのみ、CLAUDE.mdの「実データ最低1テスト」
#   規定に沿って例外的に実データを使う。無ければskipする）。
# ============================================================================


def _build_synthetic_toc_pdf(
    path: Path, page_count: int, toc: list[list], page_labels: list[dict] | None = None
) -> None:
    doc = fitz.open()
    for _ in range(page_count):
        doc.new_page()
    doc.set_toc(toc)
    if page_labels is not None:
        doc.set_page_labels(page_labels)
    doc.save(str(path))
    doc.close()


def _build_synthetic_labeled_pdf(path: Path, page_count: int, page_labels: list[dict] | None = None) -> None:
    doc = fitz.open()
    for _ in range(page_count):
        doc.new_page()
    if page_labels is not None:
        doc.set_page_labels(page_labels)
    doc.save(str(path))
    doc.close()


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("1,2", [1, 2]),
        ("1-2", [1, 2]),
        ("1,3-4", [1, 3, 4]),
        ("2", [2]),
        ("2,1", [1, 2]),  # 順序に依存せず昇順ソートされる
    ],
)
def test_parse_chapter_spec_valid(spec, expected):
    assert parse_chapter_spec(spec) == expected


@pytest.mark.parametrize("spec", ["", "0", "abc", "2-1"])
def test_parse_chapter_spec_invalid(spec):
    with pytest.raises(ChapterResolutionError):
        parse_chapter_spec(spec)


def test_resolve_chapter_page_range_requires_toc(tmp_path):
    """目次（TOC/Outline）が無いPDFでは、--chapterではなく--start/--endを
    使うよう案内する例外を送出する。"""
    pdf_path = tmp_path / "no_toc.pdf"
    _build_synthetic_toc_pdf(pdf_path, page_count=3, toc=[])
    with pytest.raises(ChapterResolutionError):
        resolve_chapter_page_range(pdf_path, "1")


def test_resolve_chapter_page_range_computes_ranges_from_toc(tmp_path):
    """章番号→物理ページ範囲への変換ロジックを、合成TOCで決定的に検証する。

    次の一般的な性質を検証する（特定の論文の目次構成には依存しない）。
    - 最上位階層（レベル最小値）のみを章として数え、下位階層（節等、
      ここではlevel=2）は無視する。
    - 章の終了ページは「その章の開始ページより大きい最小の開始ページの
      直前」（最終章は文書の最終ページ）。
    - 複数章指定はそれらを包含する最小の連続範囲になる。
    """
    toc = [
        [1, "Chapter 1", 1],
        [2, "Chapter 1.1", 2],
        [1, "Chapter 2", 4],
        [2, "Chapter 2.1", 5],
        [2, "Chapter 2.2", 7],
        [1, "Chapter 3", 9],
    ]
    pdf_path = tmp_path / "toc.pdf"
    _build_synthetic_toc_pdf(pdf_path, page_count=12, toc=toc)

    assert resolve_chapter_page_range(pdf_path, "1") == (1, 3)
    assert resolve_chapter_page_range(pdf_path, "2") == (4, 8)
    # 最終章（章3）は次の開始ページが無いため、文書の最終ページまでとなる
    assert resolve_chapter_page_range(pdf_path, "3") == (9, 12)
    # 複数章指定はそれらを包含する最小の連続範囲になる
    assert resolve_chapter_page_range(pdf_path, "1,2") == (1, 8)
    assert resolve_chapter_page_range(pdf_path, "1-3") == (1, 12)


def test_resolve_chapter_page_range_out_of_range(tmp_path):
    """目次に存在しない章番号を指定した場合はエラーになる（合成TOCは
    章3件分のみなので、章番号10の指定はエラーになる）。"""
    toc = [[1, "Chapter 1", 1], [1, "Chapter 2", 2], [1, "Chapter 3", 3]]
    pdf_path = tmp_path / "toc.pdf"
    _build_synthetic_toc_pdf(pdf_path, page_count=3, toc=toc)
    with pytest.raises(ChapterResolutionError):
        resolve_chapter_page_range(pdf_path, "10")


def test_resolve_chapter_page_range_without_page_labels_uses_all_top_level_entries(tmp_path):
    """印刷ページラベルの情報を持たないPDF（学術論文PDF等。fitzのget_label
    が全ページで空文字列を返す）では、前付け判定は行わず、従来通り目次の
    最上位階層すべてを章として数える（後方互換の確認）。合成PDFは
    page_labelsを指定しないため、sample2.pdfのような「印刷ページラベル
    情報が無い」状態を再現する。"""
    toc = [[1, "Chapter 1", 1], [1, "Chapter 2", 3], [1, "Chapter 3", 5]]
    pdf_path = tmp_path / "no_page_labels.pdf"
    _build_synthetic_toc_pdf(pdf_path, page_count=6, toc=toc)

    assert resolve_chapter_page_range(pdf_path, "1") == (1, 2)
    assert resolve_chapter_page_range(pdf_path, "1,2") == (1, 4)


def test_resolve_chapter_page_range_skips_roman_numeral_front_matter():
    """唯一の実データ確認（sample3.pdf）。前付け（ローマ数字ページ）が
    章として数えられず、本文の算用数字ページから第1章が始まることを
    確認する（CLAUDE.mdの「実データ最低1テスト」規定）。"""
    if not SAMPLE3_PDF_PATH.exists():
        pytest.skip("sample3.pdf がないためスキップ")
    assert resolve_chapter_page_range(SAMPLE3_PDF_PATH, "1") == (17, 49)
    # 第1・2章 = Part I 〜 Part II（Part III開始=物理ページ89の直前）
    assert resolve_chapter_page_range(SAMPLE3_PDF_PATH, "1,2") == (17, 88)


def test_resolve_physical_page_range_handles_label_gap(tmp_path):
    """印刷ページ番号にギャップ（欠番）がある場合でも正しく変換できるか。"""
    pdf_path = tmp_path / "gap.pdf"
    page_labels = [
        {"startpage": 0, "style": "D", "firstpagenum": 1},
        {"startpage": 3, "style": "D", "firstpagenum": 5},
    ]
    _build_synthetic_labeled_pdf(pdf_path, page_count=6, page_labels=page_labels)
    assert resolve_physical_page_range(pdf_path, "3", "5") == (3, 4)


def test_resolve_physical_page_unknown_label_raises(tmp_path):
    """存在しない印刷ページラベルを指定した場合にエラーになるか。"""
    pdf_path = tmp_path / "labeled.pdf"
    _build_synthetic_labeled_pdf(
        pdf_path, page_count=3, page_labels=[{"startpage": 0, "style": "D", "firstpagenum": 1}]
    )
    with pytest.raises(PageLabelResolutionError):
        resolve_physical_page(pdf_path, "zzz")


def test_resolve_physical_page_without_page_labels_raises(tmp_path):
    """印刷ページラベルの情報を持たないPDF（学術論文PDF等）では、
    どのラベルを指定してもエラーになる（--start-labelではなく
    --start/--endを使うよう案内される）。"""
    pdf_path = tmp_path / "no_labels.pdf"
    _build_synthetic_labeled_pdf(pdf_path, page_count=3, page_labels=None)
    with pytest.raises(PageLabelResolutionError):
        resolve_physical_page(pdf_path, "1")


def test_resolve_physical_page_for_sample3():
    """唯一の実データ確認その2（sample3.pdf）。"cov"の短縮形一致・大文字
    小文字同一視・ローマ数字前付けが実際の書籍PDFで正しく解決されるか。"""
    if not SAMPLE3_PDF_PATH.exists():
        pytest.skip("sample3.pdf がないためスキップ")
    assert resolve_physical_page(SAMPLE3_PDF_PATH, "cov") == 1  # 短縮形（実ラベルは"Cover"）
    assert resolve_physical_page(SAMPLE3_PDF_PATH, "COV") == 1  # 大文字小文字を区別しない
    assert resolve_physical_page(SAMPLE3_PDF_PATH, "Cover") == 1
    assert resolve_physical_page(SAMPLE3_PDF_PATH, "i") == 2
    assert resolve_physical_page(SAMPLE3_PDF_PATH, "xviii") == 16
    assert resolve_physical_page(SAMPLE3_PDF_PATH, "55") == 67  # 本文開始後の算用数字ページ
    assert resolve_physical_page(SAMPLE3_PDF_PATH, "60") == 72


def test_resolve_page_range_conflicting_start_and_start_label_raises():
    """--startと--start-labelを同時に指定するのは矛盾した指定であり、
    どちらを優先すべきか一意に決まらないためエラーとする。"""
    with pytest.raises(ValueError):
        resolve_page_range(SAMPLE3_PDF_PATH, None, 67, None, "55", None)


def test_resolve_page_range_prefers_start_over_chapter(tmp_path):
    toc = [[1, "Chapter 1", 1], [1, "Chapter 2", 5]]
    pdf_path = tmp_path / "toc.pdf"
    _build_synthetic_toc_pdf(pdf_path, page_count=8, toc=toc)
    assert resolve_page_range(pdf_path, "1", 3, 4, None, None) == (3, 4)


def test_resolve_page_range_prefers_label_over_chapter():
    """--start-label/--end-labelが指定された場合、--chapterより優先される
    （--start/--endが優先されるのと同じ優先順位）。"""
    start_page, end_page = resolve_page_range(SAMPLE3_PDF_PATH, "1", None, None, "55", "60")
    assert (start_page, end_page) == (67, 72)


def test_resolve_page_range_falls_back_to_chapter_when_no_page_range_given(tmp_path):
    toc = [[1, "Chapter 1", 1], [1, "Chapter 2", 5]]
    pdf_path = tmp_path / "toc.pdf"
    _build_synthetic_toc_pdf(pdf_path, page_count=8, toc=toc)
    assert resolve_page_range(pdf_path, "1", None, None, None, None) == (1, 4)


def test_resolve_page_range_returns_none_when_nothing_specified():
    assert resolve_page_range(Path("dummy.pdf"), None, None, None, None, None) == (None, None)


def test_require_pdf_exists_raises_for_missing_file():
    """存在しない入力PDFパスを指定した場合、fitzの生の例外
    （FileNotFoundError）ではなく、分かりやすいSystemExitで終了すること
    を確認する。"""
    with pytest.raises(SystemExit, match="入力PDFファイルが見つかりません"):
        translate_paper._require_pdf_exists("input/does_not_exist.pdf")


def test_require_pdf_exists_passes_for_existing_file():
    """実在するPDFパスを指定した場合は何も送出しないこと（正常系）を
    確認する。"""
    translate_paper._require_pdf_exists(SAMPLE_PDF_PATH)


@pytest.mark.parametrize(
    "chapter,start,end,start_label,end_label,expected",
    [
        (None, None, None, None, None, "full"),
        ("1,2", None, None, None, None, "chapter1_2"),
        (None, 66, 71, None, None, "p66-71"),
        (None, None, None, "55", "60", "label55-60"),
        # --start/--end・--start-label/--end-labelはresolve_page_rangeと
        # 同じ優先順位（印刷ページラベル・物理ページ番号 > 章指定）。
        ("1", 66, 71, None, None, "p66-71"),
        ("1", None, None, "55", "60", "label55-60"),
    ],
)
def test_describe_page_range(chapter, start, end, start_label, end_label, expected):
    """cache/配下のフォルダ名・output/配下の自動生成名に使う人間可読な
    範囲記述子が、resolve_page_rangeと同じ優先順位で組み立てられるかの
    単体テスト。"""
    assert translate_paper.describe_page_range(chapter, start, end, start_label, end_label) == expected


def test_default_output_dir_builds_manual_naming_convention():
    """output_dir省略時にmain()が使う自動生成パスが、testExplain.txtで
    定義した人間による手動実行（「本実行」）と同じ命名規則
    （output/manual_{PDF名}_{範囲記述子}_{実行日時}）になるかを確認する。
    main()自体はargparse・sys.stdout.reconfigure・run_pipelineの実行を
    含み単体テストしにくいため、命名ロジックを切り出したこの関数を直接
    検証する。"""
    result = translate_paper.default_output_dir("input/sample0.pdf", "full", timestamp="20260101-120000")
    assert result == Path("output") / "manual_sample0_full_20260101-120000"


# ============================================================================
# 工程(2): PDF解析（MinerU実行）
#   対応関数: pdf_mineru_runner.run_mineru のみ。6工程中唯一の重い外部
#   処理のため、subprocess.runを差し替えて実MinerUを一切起動せずに検証
#   する（test_mineru_cache.pyと同じ手法。キャッシュ保存先も汚染しない
#   よう一時ディレクトリへ差し替える）。
# ============================================================================

_FAKE_MINERU_ITEMS = [{"type": "text", "text": "hello", "text_level": None, "page_idx": 0}]


@pytest.fixture()
def isolated_mineru_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(mineru_cache, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(mineru_cache, "get_mineru_version", lambda: "test-version-1")


def _install_fake_mineru_subprocess(monkeypatch, call_counter: list[int], succeed: bool = True) -> None:
    def _fake_run(command, check, capture_output):
        call_counter[0] += 1
        if not succeed:
            raise subprocess.CalledProcessError(1, command, stderr=b"boom")
        pdf_path = Path(command[command.index("--path") + 1])
        work_dir = Path(command[command.index("--output") + 1])
        stem = pdf_path.stem
        auto_dir = work_dir / stem / "auto"
        (auto_dir / "images").mkdir(parents=True)
        (auto_dir / "images" / "fig1.jpg").write_bytes(b"fake-image-bytes")
        (auto_dir / f"{stem}_content_list.json").write_text(json.dumps(_FAKE_MINERU_ITEMS), encoding="utf-8")
        return MagicMock(returncode=0)

    monkeypatch.setattr(pdf_mineru_runner.subprocess, "run", _fake_run)


def test_run_mineru_invokes_subprocess_and_parses_content_list(tmp_path, isolated_mineru_cache, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    _make_blank_pdf(pdf_path)
    call_counter = [0]
    _install_fake_mineru_subprocess(monkeypatch, call_counter)

    result = pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work")

    assert call_counter[0] == 1
    assert result.items == _FAKE_MINERU_ITEMS
    assert (result.images_base / "images" / "fig1.jpg").exists()


def test_run_mineru_uses_cache_on_second_call(tmp_path, isolated_mineru_cache, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    _make_blank_pdf(pdf_path)
    call_counter = [0]
    _install_fake_mineru_subprocess(monkeypatch, call_counter)

    pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work1")
    result_2 = pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work2")

    assert call_counter[0] == 1  # 2回目はキャッシュヒットでsubprocessが呼ばれない
    assert result_2.items == _FAKE_MINERU_ITEMS


def test_run_mineru_cache_invalidated_by_different_page_range(tmp_path, isolated_mineru_cache, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    _make_blank_pdf(pdf_path, page_count=3)
    call_counter = [0]
    _install_fake_mineru_subprocess(monkeypatch, call_counter)

    pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work1", 0, 1)
    pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work2", 1, 2)

    assert call_counter[0] == 2  # ページ範囲が異なるためキャッシュヒットしない


def test_run_mineru_raises_when_subprocess_fails(tmp_path, isolated_mineru_cache, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    _make_blank_pdf(pdf_path)
    call_counter = [0]
    _install_fake_mineru_subprocess(monkeypatch, call_counter, succeed=False)

    with pytest.raises(pdf_mineru_runner.MinerURunError):
        pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work")


def test_run_mineru_raises_when_output_file_missing(tmp_path, isolated_mineru_cache, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    _make_blank_pdf(pdf_path)

    monkeypatch.setattr(
        pdf_mineru_runner.subprocess, "run", lambda command, check, capture_output: MagicMock(returncode=0)
    )

    with pytest.raises(pdf_mineru_runner.MinerURunError):
        pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work")


# --- mineru_cache.py / mineru_version.py 直接の単体テスト ---------------------


def _make_images_base(tmp_path: Path, name: str = "images_base") -> Path:
    images_base = tmp_path / name
    (images_base / "images").mkdir(parents=True)
    (images_base / "images" / "fig1.jpg").write_bytes(b"fake-image-bytes")
    return images_base


def test_load_returns_none_when_no_cache(tmp_path, isolated_mineru_cache):
    pdf_path = tmp_path / "sample.pdf"
    _make_blank_pdf(pdf_path)
    assert mineru_cache.load_cached_items(pdf_path, None, None) is None


def test_save_then_load_roundtrip(tmp_path, isolated_mineru_cache):
    pdf_path = tmp_path / "sample.pdf"
    _make_blank_pdf(pdf_path)
    images_base = _make_images_base(tmp_path)

    mineru_cache.save_cache(pdf_path, None, None, _FAKE_MINERU_ITEMS, images_base)
    cached = mineru_cache.load_cached_items(pdf_path, None, None)

    assert cached is not None
    items, images_dir = cached
    assert items == _FAKE_MINERU_ITEMS
    assert (images_dir / "images" / "fig1.jpg").read_bytes() == b"fake-image-bytes"


def test_cache_isolated_by_page_range(tmp_path, isolated_mineru_cache):
    pdf_path = tmp_path / "sample.pdf"
    _make_blank_pdf(pdf_path, page_count=3)
    images_base = _make_images_base(tmp_path)

    mineru_cache.save_cache(pdf_path, 0, 1, _FAKE_MINERU_ITEMS, images_base)

    assert mineru_cache.load_cached_items(pdf_path, 0, 1) is not None
    assert mineru_cache.load_cached_items(pdf_path, 1, 2) is None
    assert mineru_cache.load_cached_items(pdf_path, None, None) is None


def test_load_returns_none_when_pdf_content_changes(tmp_path, isolated_mineru_cache):
    pdf_path = tmp_path / "sample.pdf"
    _make_blank_pdf(pdf_path, page_count=1)
    images_base = _make_images_base(tmp_path)
    mineru_cache.save_cache(pdf_path, None, None, _FAKE_MINERU_ITEMS, images_base)
    assert mineru_cache.load_cached_items(pdf_path, None, None) is not None

    # 同じパスに別内容のPDFを書き直す（著者の改訂・別ファイルへの差し替え等を想定）。
    _make_blank_pdf(pdf_path, page_count=5)

    assert mineru_cache.load_cached_items(pdf_path, None, None) is None


def test_load_returns_none_when_mineru_version_changes(tmp_path, isolated_mineru_cache, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    _make_blank_pdf(pdf_path)
    images_base = _make_images_base(tmp_path)

    monkeypatch.setattr(mineru_cache, "get_mineru_version", lambda: "1.0.0")
    mineru_cache.save_cache(pdf_path, None, None, _FAKE_MINERU_ITEMS, images_base)
    assert mineru_cache.load_cached_items(pdf_path, None, None) is not None

    monkeypatch.setattr(mineru_cache, "get_mineru_version", lambda: "2.0.0")
    assert mineru_cache.load_cached_items(pdf_path, None, None) is None


def test_load_returns_none_on_corrupted_content_list_json(tmp_path, isolated_mineru_cache):
    pdf_path = tmp_path / "sample.pdf"
    _make_blank_pdf(pdf_path)
    images_base = _make_images_base(tmp_path)
    mineru_cache.save_cache(pdf_path, None, None, _FAKE_MINERU_ITEMS, images_base)

    cache_dir = mineru_cache._cache_dir(pdf_path, None, None)
    (cache_dir / "content_list.json").write_text("{not valid json", encoding="utf-8")

    assert mineru_cache.load_cached_items(pdf_path, None, None) is None


def test_save_is_noop_when_pdf_hash_fails(tmp_path, isolated_mineru_cache):
    pdf_path = tmp_path / "does_not_exist.pdf"
    images_base = _make_images_base(tmp_path)

    # ファイルが存在しない場合、_compute_pdf_hashはOSErrorを送出するため、
    # save_cacheは例外を伝播させず静かに何もしない。
    mineru_cache.save_cache(pdf_path, None, None, _FAKE_MINERU_ITEMS, images_base)

    assert not (tmp_path / "cache").exists()


def test_cache_disabled_via_env_var(tmp_path, isolated_mineru_cache, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    _make_blank_pdf(pdf_path)
    images_base = _make_images_base(tmp_path)

    mineru_cache.save_cache(pdf_path, None, None, _FAKE_MINERU_ITEMS, images_base)
    assert mineru_cache.load_cached_items(pdf_path, None, None) is not None

    monkeypatch.setenv("MINERU_CACHE_DISABLE", "1")
    # 既にキャッシュが存在していても、無効化フラグが優先される。
    assert mineru_cache.load_cached_items(pdf_path, None, None) is None

    mineru_cache.save_cache(pdf_path, 0, 0, _FAKE_MINERU_ITEMS, images_base)
    assert mineru_cache.load_cached_items(pdf_path, 0, 0) is None
    monkeypatch.delenv("MINERU_CACHE_DISABLE")
    assert mineru_cache.load_cached_items(pdf_path, 0, 0) is None  # 無効化中は保存自体されていない


def test_get_mineru_version_raises_on_missing_package(monkeypatch):
    import importlib.metadata

    import mineru_version

    def _raise(_name):
        raise importlib.metadata.PackageNotFoundError()

    monkeypatch.setattr(importlib.metadata, "version", _raise)
    with pytest.raises(mineru_version.MinerUVersionError):
        mineru_version.get_mineru_version()


def test_run_mineru_reruns_after_version_change(tmp_path, isolated_mineru_cache, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    _make_blank_pdf(pdf_path)
    call_counter = [0]
    _install_fake_mineru_subprocess(monkeypatch, call_counter)

    monkeypatch.setattr(mineru_cache, "get_mineru_version", lambda: "1.0.0")
    pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work1")
    assert call_counter[0] == 1

    monkeypatch.setattr(mineru_cache, "get_mineru_version", lambda: "2.0.0")
    (tmp_path / "work2").mkdir()
    pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work2")
    assert call_counter[0] == 2


@pytest.mark.parametrize("pdf_path,start_page,end_page,run_id,range_label", _REAL_SAMPLE_SCENARIOS)
def test_mineru_cache_has_real_content_for_sample_pdfs(pdf_path, start_page, end_page, run_id, range_label):
    """実サンプルPDF（sample0〜3）について、cache/<run_id>/mineru_cache/に
    実MinerU実行結果が既に永続化されているかを確認する実データ・
    スモークテスト。run_idで対象サンプルを選べるよう
    _REAL_SAMPLE_SCENARIOSでパラメータ化してある（`-k sample0`〜
    `-k sample3`で選択可能。run_idはcache/配下の実際のフォルダ名と一致）。

    キャッシュが無い場合（対象PDFが未配置、または一度もprocess_pdfを
    実行していない場合）はskipする。実MinerUは起動しない
    （load_cached_itemsのみ呼ぶ）。
    """
    if not pdf_path.exists():
        pytest.skip(f"{pdf_path} がないためスキップ")

    start_0idx = start_page - 1 if start_page is not None else None
    end_0idx = end_page - 1 if end_page is not None else None
    cached = mineru_cache.load_cached_items(pdf_path, start_0idx, end_0idx, range_label=range_label)
    if cached is None:
        pytest.skip(
            f"{run_id} のMinerUキャッシュが無いためスキップ"
            "（一度process_pdfを実行すると生成される）"
        )

    items, images_base = cached
    assert items, f"{run_id} のcontent_listが空"
    assert images_base.exists()


# ============================================================================
# 工程(3): 構造化・タグ処理
#   対応関数: analyze_structure / translate_units / build_document
#     （content_list → タグ付きMarkdownへの変換）、および
#     parse_output_dir / exclude_references_section / build_document_context
#     （タグ付きMarkdown → DocUnit+文脈への変換）。MinerU出力を模した
#   自作items・自作Markdown・自作StructuredDocumentのみを使い、MinerU・
#   DeepLのどちらも呼ばずに検証する。
# ============================================================================


def test_analyze_structure_assigns_front_matter_and_numbered_heading_labels():
    items = [
        {"type": "text", "text": "A Study of Something", "text_level": None, "page_idx": 0},
        {"type": "text", "text": "Jane Doe", "text_level": None, "page_idx": 0},
        {"type": "text", "text": "Example University", "text_level": None, "page_idx": 0},
        {"type": "text", "text": "1. INTRODUCTION", "text_level": 1, "page_idx": 0},
        {
            "type": "text",
            "text": "This is the first sentence. This is the second sentence.",
            "text_level": None,
            "page_idx": 0,
        },
    ]
    doc = analyze_structure(items, images_base=Path("."))
    page = doc.pages[0]

    labeled = [e for e in page.elements if isinstance(e, LabeledElement)]
    assert [e.label for e in labeled] == ["TITLE", "AUTHORS", "AFFIL"]

    headings = [e for e in page.elements if isinstance(e, HeadingElement)]
    assert len(headings) == 1
    assert headings[0].section_id == "1.introduction"

    text_blocks = [e for e in page.elements if isinstance(e, TextBlockElement)]
    assert len(text_blocks) == 1
    assert text_blocks[0].sentence_ids == ["P1-S1-1.introduction-S1", "P1-S2-1.introduction-S2"]


def test_analyze_structure_assigns_synthetic_ids_to_unnumbered_headings():
    """書籍PDFにありがちな、章番号の付かない見出しに文書全体で一意な
    合成章ID（"u1","u2",...）が振られ、直後の本文文IDにも反映されるか。"""
    items = [
        {"type": "text", "text": "Symbol Emergence Systems: Overview", "text_level": 1, "page_idx": 0},
        {"type": "text", "text": "This is the first body sentence of the chapter.", "text_level": None, "page_idx": 0},
        {"type": "text", "text": "Language as a Dynamic Equilibrium System", "text_level": 2, "page_idx": 0},
        {"type": "text", "text": "This is a sentence under the subsection.", "text_level": None, "page_idx": 0},
    ]
    # page_offset=16は絶対1ページ目（前付け判定のトリガー）を避けるため。
    doc = analyze_structure(items, images_base=Path("."), page_offset=16)
    page = doc.pages[0]

    headings = [e for e in page.elements if isinstance(e, HeadingElement)]
    assert [h.section_id for h in headings] == ["u1.symbol", "u2.language"]

    text_blocks = [e for e in page.elements if isinstance(e, TextBlockElement)]
    assert text_blocks[0].sentence_ids[0].endswith("-u1.symbol-S1")
    assert text_blocks[1].sentence_ids[0].endswith("-u2.language-S1")


def test_analyze_structure_auto_protects_bare_greek_and_letter_equals_expressions():
    """MinerUが数式として検出しなかった裸のギリシャ文字・"文字=値"形式の
    断片が、構造解析段階で自動的に$...$保護されるか。"""
    items = [
        # text_level==2は前付け（TITLE/AUTHORS/AFFIL）判定の対象外のため、
        # ページ1冒頭でもこの見出しが直接HeadingElementになる。
        {"type": "text", "text": "Introduction", "text_level": 2, "page_idx": 0},
        {
            "type": "text",
            "text": "We use scale γ and set t = 1 as the starting point.",
            "text_level": None,
            "page_idx": 0,
        },
    ]
    doc = analyze_structure(items, images_base=Path("."))
    text_block = next(e for e in doc.pages[0].elements if isinstance(e, TextBlockElement))
    sentence = text_block.sentences[0]
    assert "$\\gamma$" in sentence
    assert "$t = 1$" in sentence


def test_analyze_structure_separates_figure_and_caption_with_number():
    items = [
        {"type": "image", "img_path": "images/fig1.jpg", "image_caption": ["Fig. 1: Example figure."], "page_idx": 0},
    ]
    doc = analyze_structure(items, images_base=Path("base"))
    page = doc.pages[0]

    figures = [e for e in page.elements if isinstance(e, FigureElement)]
    captions = [e for e in page.elements if isinstance(e, CaptionElement)]
    assert len(figures) == 1
    assert figures[0].number == 1
    assert figures[0].labeled is True
    assert len(captions) == 1
    assert captions[0].sentence_ids == ["P1-FIG1-CAPTION-S1"]


def test_analyze_structure_falls_back_to_unknown_element_on_error():
    """要素の解析中に例外が発生しても（ここではimg_path欠如によるKeyError）
    ドキュメント全体を止めず、UnknownElementへフォールバックするか。"""
    items = [{"type": "image", "page_idx": 0}]
    doc = analyze_structure(items, images_base=Path("."))
    page = doc.pages[0]
    assert len(page.elements) == 1
    assert isinstance(page.elements[0], UnknownElement)
    assert page.elements[0].raw_type == "image"


def test_translate_units_defaults_to_identity_and_falls_back_on_translation_failure():
    units = [
        TranslationUnit(unit_id="P1-S1-body-S1", text="Hello."),
        TranslationUnit(unit_id="P1-S2-body-S2", text="World."),
    ]
    assert translate_units(units) == {"P1-S1-body-S1": "Hello.", "P1-S2-body-S2": "World."}

    def _boom(_text: str) -> str:
        raise RuntimeError("translation backend down")

    assert translate_units(units, translate=_boom) == {"P1-S1-body-S1": "Hello.", "P1-S2-body-S2": "World."}


def test_build_document_writes_markdown_and_saves_figure_image(tmp_path):
    source_image = tmp_path / "source" / "fig.png"
    _make_source_image(source_image)

    page = PageContent(
        page_number=1,
        elements=[
            LabeledElement(text="A Title", label="TITLE"),
            HeadingElement(text="1. INTRODUCTION", section_num="1", section_name="introduction"),
            TextBlockElement(sentences=["Body sentence."], sentence_ids=["P1-S1-1.introduction-S1"]),
            FigureElement(image_path=source_image, fig_kind="figure", number=1),
            CaptionElement(
                sentences=["Fig. 1: Example."], number=1, fig_kind="figure", sentence_ids=["P1-FIG1-CAPTION-S1"]
            ),
        ],
    )
    structured_doc = StructuredDocument(pages=[page])
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    md_paths = build_document(
        structured_doc,
        translations={"P1-S1-1.introduction-S1": "訳文。"},
        output_dir=output_dir,
        first_page=1,
        last_page=1,
    )

    assert [p.name for p in md_paths] == ["page_01_en.md"]
    text = md_paths[0].read_text(encoding="utf-8")
    assert "[P1-TITLE] A Title" in text
    assert "[P1-HEADING-1.introduction] 1. INTRODUCTION" in text
    assert "[P1-S1-1.introduction-S1] 訳文。" in text  # Step3の翻訳結果が反映される
    assert "![P1-FIG1](images/fig_p1_1.png) [P1-FIG1]" in text
    assert "[P1-FIG1-CAPTION-S1] Fig. 1: Example." in text
    assert (output_dir / "images" / "fig_p1_1.png").exists()


def test_parse_page_file_classifies_tag_kinds_and_translatability(tmp_path):
    page_path = tmp_path / "page_01_en.md"
    page_path.write_text(
        "[P1-TITLE] A Title\n"
        "[P1-AUTHORS] Jane Doe\n"
        "[P1-AFFIL] Example University\n"
        "[P1-HEADING-1.introduction] 1. INTRODUCTION\n"
        "[P1-S1-1.introduction-S1] Body sentence.\n"
        "[P1-FIG1-CAPTION-S1] Fig. 1: Example.\n"
        "![P1-FIG1](images/fig_p1_1.png) [P1-FIG1]\n"
        "[P1-EQ1-LATEX] $$x = 1$$\n",
        encoding="utf-8",
    )
    units = parse_page_file(page_path)
    kinds = {u.tag: u.kind for u in units}
    translatable = {u.tag: u.translatable for u in units}

    assert kinds == {
        "P1-TITLE": "title",
        "P1-AUTHORS": "authors",
        "P1-AFFIL": "affil",
        "P1-HEADING-1.introduction": "heading",
        "P1-S1-1.introduction-S1": "body_sentence",
        "P1-FIG1-CAPTION-S1": "caption_sentence",
        "P1-FIG1": "figure_image",
        "P1-EQ1-LATEX": "equation_latex",
    }
    assert translatable["P1-TITLE"] is True
    assert translatable["P1-FIG1"] is False
    assert translatable["P1-EQ1-LATEX"] is False


def test_parse_output_dir_combines_pages_in_page_number_order(tmp_path):
    (tmp_path / "page_02_en.md").write_text("[P2-S1-body-S1] Second page.\n", encoding="utf-8")
    (tmp_path / "page_01_en.md").write_text("[P1-S1-body-S1] First page.\n", encoding="utf-8")
    units = parse_output_dir(tmp_path)
    assert [u.tag for u in units] == ["P1-S1-body-S1", "P2-S1-body-S1"]


def test_exclude_references_section_marks_units_non_translatable():
    units = [
        DocUnit(tag="P5-HEADING-5.references", kind="heading", page=5, en_text="References", translatable=True),
        DocUnit(
            tag="P5-S1-5.references-S1",
            kind="body_sentence",
            page=5,
            en_text="A. Author, Title, 2020.",
            translatable=True,
        ),
        DocUnit(
            tag="P1-S1-1.introduction-S1",
            kind="body_sentence",
            page=1,
            en_text="Intro sentence.",
            translatable=True,
        ),
    ]
    exclude_references_section(units)

    assert units[0].translatable is False and units[0].ja_text == "References"
    assert units[1].translatable is False and units[1].ja_text == "A. Author, Title, 2020."
    assert units[2].translatable is True


def test_build_document_context_uses_title_and_abstract_sentences():
    units = [
        DocUnit(tag="P1-TITLE", kind="title", page=1, en_text="A Great Paper"),
        DocUnit(tag="P1-AUTHORS", kind="authors", page=1, en_text="Jane Doe"),
        DocUnit(tag="P1-S1-abstract-S1", kind="body_sentence", page=1, en_text="We propose a method."),
        DocUnit(tag="P1-S2-abstract-S2", kind="body_sentence", page=1, en_text="It works well."),
        DocUnit(tag="P1-S1-1.introduction-S1", kind="body_sentence", page=1, en_text="Intro text."),
    ]
    assert build_document_context(units) == "A Great Paper\nWe propose a method. It works well."


@pytest.mark.parametrize(
    "text,expected",
    [
        ("We use a linear model. It performs well.", ["We use a linear model.", "It performs well."]),
        (
            "See Fig. 1 for details. The results are shown in Sec. 2.",
            ["See Fig. 1 for details.", "The results are shown in Sec. 2."],
        ),
        (
            "This includes e.g. cats and dogs. Both are pets.",
            ["This includes e.g. cats and dogs.", "Both are pets."],
        ),
        ("", []),
    ],
)
def test_split_sentences_handles_abbreviations(text, expected):
    """略語（Fig., Sec., e.g. など）の直後で誤分割しないことを確認する単体テスト。"""
    assert split_sentences(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1. INTRODUCTION", ("1", "introduction")),
        ("2.1. Preliminaries", ("2.1", "preliminaries")),
        ("ABSTRACT", None),  # 章番号が無いので見出しとして扱わない
    ],
)
def test_heading_regex_and_slugify(text, expected):
    """章・節見出しの判定と章名スラッグ化の単体テスト。"""
    m = HEADING_RE.match(text)
    if expected is None:
        assert m is None
    else:
        assert m is not None
        assert m.group(1) == expected[0]
        assert slugify_section_name(m.group(2)) == expected[1]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Fig. 1: Overview of EasyControlEdge.", ("figure", 1)),
        ("Figure 3. Something", ("figure", 3)),
        ("Table 2: Comparison results.", ("table", 2)),
        ("This is not a caption.", None),
    ],
)
def test_parse_caption_label(text, expected):
    """キャプション文からの種別・番号抽出の単体テスト。"""
    assert parse_caption_label(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        # 地の文に単独で出現するギリシャ文字は、実在の英単語と衝突しないため
        # 自動的に$...$で保護してよい。TeXコマンド形式（例:"γ"→"\gamma"）に
        # 変換した上で保護する（他の数式スパン（MinerU由来）との表記の一貫性のため）。
        ("we control edge density via scale γ (Sec. 2.4).", "we control edge density via scale $\\gamma$ (Sec. 2.4)."),
        # 複数のギリシャ文字が連続する場合は1つの数式スパンとしてまとめ、
        # それぞれ個別のコマンドに変換して連結する。
        ("with schedule αβ decay", "with schedule $\\alpha\\beta$ decay"),
        # 既に$...$で保護済みのギリシャ文字は二重にラップ・変換しない。
        ("already protected $γ$ here", "already protected $γ$ here"),
        ("no greek letters here at all.", "no greek letters here at all."),
        # ラテン文字と同形の大文字ギリシャ文字（例:"Α"=Alpha）はKaTeXに
        # 専用コマンドが無いため、変換せず元の文字のまま$...$で囲む。
        ("value Α here", "value $Α$ here"),
    ],
)
def test_wrap_bare_greek_letters(text, expected):
    """未保護のギリシャ文字が$...$で自動的に囲まれるかの単体テスト。"""
    assert wrap_bare_greek_letters(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        # "1文字の変数 = 値"という数式的表現は、実在の英文として自然に使われ
        # ることが無いため自動的に$...$で保護してよい。
        ("integrate t = 1 to t = 0.", "integrate $t = 1$ to $t = 0$."),
        ("where K=5 discrete steps", "where $K=5$ discrete steps"),
        ("already protected $t = 1$ here", "already protected $t = 1$ here"),
        ("no bare equations here at all.", "no bare equations here at all."),
        # 2文字以上の識別子（"ab = 1"）は対象外（単体アルファベットに
        # 限定することで、通常の英単語との誤認を避ける設計）。
        ("ab = 1 is not a match", "ab = 1 is not a match"),
    ],
)
def test_wrap_bare_letter_equals_expressions(text, expected):
    """未保護の"1文字の変数 = 値"形式が$...$で自動的に囲まれるかの単体テスト。"""
    assert wrap_bare_letter_equals_expressions(text) == expected


# --- 工程(3)の実データ確認（process_pdf経由でMinerU+analyze_structureを --
# 実際に実行し、複雑な実データに対する挙動を確認する。sample0.pdfのMinerU
# 解析結果はcache/配下に永続化されており、2回目以降は数秒で完了する） ---


@pytest.mark.parametrize("pdf_path,start_page,end_page,run_id,range_label", _REAL_SAMPLE_SCENARIOS)
def test_analyze_structure_on_real_cached_mineru_content(pdf_path, start_page, end_page, run_id, range_label):
    """実サンプルPDF（sample0〜3）のMinerUキャッシュ済みcontent_listを
    analyze_structureに通し、DeepLを一切経由せずに実データの構造化結果
    （ページ・翻訳対象文が最低1件ずつ得られること）を確認する実データ・
    スモークテスト。run_idで対象サンプルを選べるよう
    _REAL_SAMPLE_SCENARIOSでパラメータ化してある。

    厳密な期待値（見出し文言等）はスイートB/D/Fの実データ確認テストが
    個別に担当するため、本テストはあくまで「例外なく完走し、最低限の
    構造が得られるか」を全サンプル共通で確認する薄いチェックに留める。
    """
    if not pdf_path.exists():
        pytest.skip(f"{pdf_path} がないためスキップ")

    start_0idx = start_page - 1 if start_page is not None else None
    end_0idx = end_page - 1 if end_page is not None else None
    cached = mineru_cache.load_cached_items(pdf_path, start_0idx, end_0idx, range_label=range_label)
    if cached is None:
        pytest.skip(
            f"{run_id} のMinerUキャッシュが無いためスキップ"
            "（一度process_pdfを実行すると生成される）"
        )

    items, images_base = cached
    page_offset = start_0idx if start_0idx is not None else 0
    doc = analyze_structure(items, images_base=images_base, page_offset=page_offset)

    assert doc.pages, "ページが1つも構築されていない"
    assert doc.translation_units, "翻訳対象文が1つも抽出されていない"
    for unit in doc.translation_units:
        assert unit.unit_id
        assert unit.text.strip()


@pytest.fixture(scope="module")
def processed(tmp_path_factory):
    """sample0.pdf を一度だけMinerUで処理し、結果を全テストで共有する
    （MinerUの推論はCPU環境で数分かかるため、モジュール内で使い回す）。"""
    if not SAMPLE_PDF_PATH.exists():
        pytest.skip("sample0.pdf がないためスキップ")
    output_dir = tmp_path_factory.mktemp("output")
    md_paths = process_pdf(SAMPLE_PDF_PATH, output_dir, range_label="full")
    texts = {p.name: p.read_text(encoding="utf-8") for p in md_paths}
    return {"output_dir": output_dir, "md_paths": md_paths, "texts": texts}


def test_extract_and_number_sentences(processed):
    """
    input/sample0.pdf を解析し、
    - 画像が抽出されているか
    - [P1-S{n}-{section}-S{n}] 形式のナンバリングが付与されたテキストが得られるか
    をテストする
    """
    md_paths = processed["md_paths"]
    assert len(md_paths) == 2  # 2ページ分出力されているか
    assert [p.name for p in md_paths] == ["page_01_en.md", "page_02_en.md"]

    page1_text = processed["texts"]["page_01_en.md"]
    assert "[P1-TITLE]" in page1_text
    assert "[P1-S1-abstract-S1]" in page1_text


def test_front_matter_labels_are_not_counted_as_sentences(processed):
    """タイトル・著者・所属は本文の文としてナンバリングされず、専用ラベルになっているか。"""
    page1_text = processed["texts"]["page_01_en.md"]
    lines = page1_text.splitlines()
    assert lines[0] == "[P1-TITLE] EASYCONTROLEDGE: A FOUNDATION-MODEL FINE-TUNING FOR EDGE DETECTION"
    assert lines[1].startswith("[P1-AUTHORS] Hiroki Nakamura")
    assert lines[2].startswith("[P1-AFFIL]")
    # ABSTRACT見出し自体は本文の文として扱われる（章ラベルは"abstract"、文としてナンバリングされる）
    assert lines[3] == "[P1-S1-abstract-S1] ABSTRACT"


def test_chapter_headings_get_dedicated_labels(processed):
    """章・節見出しが本文の文カウントに含まれず、章番号ベースの専用ラベルになっているか。"""
    page1_text = processed["texts"]["page_01_en.md"]
    page2_text = processed["texts"]["page_02_en.md"]

    m = HEADING_ID_RE.search(next(line for line in page1_text.splitlines() if "INTRODUCTION" in line))
    assert m is not None
    assert m.group(2) == "1.introduction"
    assert m.group(3) == "1. INTRODUCTION"

    for line, expected_section in [
        ("2. METHOD: EASYCONTROLEDGE", "2.method"),
        ("2.1. Preliminaries", "2.1.preliminaries"),
        ("2.2. Lightweight Adaptation via Condition Injection", "2.2.lightweight"),
    ]:
        full_line = next(l for l in page2_text.splitlines() if l.endswith(line))
        m = HEADING_ID_RE.match(full_line)
        assert m is not None, f"unexpected heading line format: {full_line!r}"
        assert m.group(2) == expected_section


def test_cross_page_paragraph_is_kept_on_starting_page(processed):
    """段落が物理ページをまたいでいても、MinerUが1つの段落としてまとめた場合は
    開始ページ側のMarkdownに収まる（本ツールが許容している既知の挙動）。"""
    page1_text = processed["texts"]["page_01_en.md"]
    page2_text = processed["texts"]["page_02_en.md"]

    # 元は物理2ページ目にある(2)(3)の列挙が、1ページ目の段落として結合されている
    assert "(2) We add an edge-specific pixel loss" in page1_text
    assert "(3) At inference time, we adopt a Classifier-Free Guidance" in page1_text
    assert "(2) We add an edge-specific pixel loss" not in page2_text


def test_figure_caption_is_separated_from_body_text(processed):
    """図表のキャプションが本文の文カウントに含まれず、対応する図番号に紐づいた
    専用ラベルで、かつ文単位に分割・ナンバリングされているか。"""
    page2_text = processed["texts"]["page_02_en.md"]
    lines = page2_text.splitlines()

    caption_lines = [line for line in lines if line.startswith("[P2-FIG1-CAPTION-S")]
    assert len(caption_lines) == 3  # "Fig. 1: ..." / "The left side..." / "We train only..." の3文

    for expected_seq, line in enumerate(caption_lines, start=1):
        m = CAPTION_ID_RE.match(line)
        assert m is not None, f"unexpected caption line format: {line!r}"
        assert m.group(1) == "2"
        assert m.group(2) == "1"
        assert int(m.group(3)) == expected_seq  # 図ごとに1から連番

    assert caption_lines[0].endswith("Fig. 1: Overview of EasyControlEdge.")
    assert "We train only a condition-injection LoRA" in caption_lines[2]

    # キャプションの文は本文（P2-S...）の連番には含まれない
    assert not any(
        line.startswith("[P2-S") and "We train only a condition-injection LoRA" in line for line in lines
    )


def test_images_are_extracted_as_png(processed):
    """図・数式領域がPNGとして output/images/ に切り出されているかを確認する
    （sample0.pdfには図1つと数式3つ(Eq.1〜Eq.3)が含まれる）。"""
    images_dir = processed["output_dir"] / "images"
    png_files = sorted(images_dir.glob("*.png"))
    assert [p.name for p in png_files] == ["eq_p2_1.png", "eq_p2_2.png", "eq_p2_3.png", "fig_p2_1.png"]

    for png_file in png_files:
        data = png_file.read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        pix = fitz.Pixmap(str(png_file))
        assert pix.width > 0 and pix.height > 0


def test_figure_reference_is_placed_in_page2_markdown(processed):
    """画像領域に参照用ID [P2-FIG1] が割り当てられ、Markdownに埋め込まれているか。"""
    page2_text = processed["texts"]["page_02_en.md"]
    fig_lines = [line for line in page2_text.splitlines() if line.startswith("![P2-FIG")]
    assert len(fig_lines) == 1
    m = FIGURE_ID_RE.match(fig_lines[0])
    assert m is not None, f"unexpected figure line format: {fig_lines[0]!r}"
    assert m.group(3) == "fig_p2_1.png"

    # page1には図が存在しない
    assert "![P1-FIG" not in processed["texts"]["page_01_en.md"]


def test_all_three_equations_are_detected_with_latex(processed):
    """本文中に埋め込まれた数式番号(1)(2)(3)を持つ3つの数式すべてが、画像として
    分離され、かつMinerUが生成した構文的に正しいLaTeXテキストを伴っているか。"""
    page2_text = processed["texts"]["page_02_en.md"]
    lines = page2_text.splitlines()

    for n in (1, 2, 3):
        assert any(line.startswith(f"![P2-EQ{n}]") for line in lines), f"Eq.{n} の画像行が見つからない"
        m = EQUATION_ID_RE.match(next(line for line in lines if line.startswith(f"![P2-EQ{n}]")))
        assert m is not None
        assert m.group(3) == f"eq_p2_{n}.png"
        latex_line = next(line for line in lines if line.startswith(f"[P2-EQ{n}-LATEX]"))
        assert "\\tag{" + str(n) + "}" in latex_line


def test_inline_math_is_wrapped_in_dollar_signs(processed):
    """文中に埋め込まれたインライン数式が、MinerUの数式認識により$...$で
    構造化されているか（2箇所の固定例による回帰用spot check）。"""
    page2_text = processed["texts"]["page_02_en.md"]
    assert "$p ( y \\mid x )$" in page2_text
    # 単位行列を表す"I"（英語の代名詞"I"と同形）も、正しく数式側に含まれていること
    assert "\\mathbf { I }" in page2_text


def test_math_protection_round_trips_all_real_inline_math(processed):
    """page_02_en.md（sample0.pdf）に実際に含まれる全てのインライン数式・
    ディスプレイ数式スパンが、math_protection.protect/restoreのラウンド
    トリップで（\\textless/\\textgreaterの正規化を除き）壊れず復元されるか。
    自作データではなく実データの数式スパン全数を対象にする点が、他の
    protect/restoreテストと異なる。"""
    text = processed["texts"]["page_02_en.md"]

    protected, spans = protect(text)
    # page_02_en.mdに実際に含まれる数式スパン数（インライン24+ディスプレイ3）。
    # MinerUの数式検出結果や自動保護ロジックが変われば変化しうる値のため、
    # 変化した場合は数式検出の挙動が変わっていないか確認すること。
    assert len(spans) == 27
    # プレースホルダ置換後のテキストに数式デリミタ$が一切残っていないこと
    assert "$" not in protected

    restored = restore(protected, spans)
    expected = text.replace("\\textless", "<").replace("\\textgreater", ">")
    assert restored == expected


def test_header_and_footer_noise_excluded(processed):
    """arXiv IDなどのノイズが本文に含まれていないか。"""
    full_text = "\n".join(processed["texts"].values())
    assert "arXiv:" not in full_text


def test_sentence_ids_are_sequential_per_page(processed):
    """各ページの文IDが1から連番で付与されているか。"""
    for name, text in processed["texts"].items():
        page_num = int(re.search(r"page_(\d+)_en", name).group(1))
        sentence_lines = [line for line in text.splitlines() if line.startswith(f"[P{page_num}-S")]
        assert sentence_lines, f"{name} に文が1つも抽出されていない"

        ids = []
        for line in sentence_lines:
            m = SENTENCE_ID_RE.match(line)
            assert m is not None, f"unexpected sentence line format: {line!r}"
            assert int(m.group(1)) == page_num
            ids.append(int(m.group(2)))
        assert ids == list(range(1, len(ids) + 1))


@pytest.fixture(scope="module")
def processed_sample1(tmp_path_factory):
    """sample1.pdf（表を含む）を一度だけMinerUで処理し、結果を全テストで共有する。"""
    if not SAMPLE1_PDF_PATH.exists():
        pytest.skip("sample1.pdf がないためスキップ")
    output_dir = tmp_path_factory.mktemp("output_sample1")
    md_paths = process_pdf(SAMPLE1_PDF_PATH, output_dir, range_label="full")
    texts = {p.name: p.read_text(encoding="utf-8") for p in md_paths}
    return {"output_dir": output_dir, "md_paths": md_paths, "texts": texts}


@pytest.fixture(scope="module")
def processed_sample2(tmp_path_factory):
    """sample2.pdf（表を含む）を一度だけMinerUで処理し、結果を全テストで共有する。"""
    if not SAMPLE2_PDF_PATH.exists():
        pytest.skip("sample2.pdf がないためスキップ")
    output_dir = tmp_path_factory.mktemp("output_sample2")
    md_paths = process_pdf(SAMPLE2_PDF_PATH, output_dir, range_label="full")
    texts = {p.name: p.read_text(encoding="utf-8") for p in md_paths}
    return {"output_dir": output_dir, "md_paths": md_paths, "texts": texts}


@pytest.fixture(scope="module")
def processed_sample3(tmp_path_factory):
    """sample3.pdf（書籍PDF）を一度だけMinerUで処理し、結果を全テストで共有する。

    CLAUDE.mdの運用規定によりsample3.pdfは全体を一括処理せず、印刷ページ
    ラベル55〜60（物理ページ67〜72）の最小範囲のみを処理対象とする
    （test_unnumbered_headings_in_real_book_pdf・_REAL_SAMPLE_SCENARIOSと
    同じ範囲）。
    """
    if not SAMPLE3_PDF_PATH.exists():
        pytest.skip("sample3.pdf がないためスキップ")
    output_dir = tmp_path_factory.mktemp("output_sample3")
    md_paths = process_pdf(SAMPLE3_PDF_PATH, output_dir, start_page=67, end_page=72, range_label="label55-60")
    texts = {p.name: p.read_text(encoding="utf-8") for p in md_paths}
    return {"output_dir": output_dir, "md_paths": md_paths, "texts": texts}


def _assert_table_caption(texts, page_name, page_num, table_num, expected_sentences):
    """指定ページのMarkdownに、表番号に紐づいたキャプションIDが文単位で
    正しく振られているかを検証する共通ヘルパー。"""
    text = texts[page_name]
    lines = text.splitlines()

    fig_line = next((l for l in lines if l.startswith(f"![P{page_num}-TABLE{table_num}]")), None)
    assert fig_line is not None, f"{page_name} に![P{page_num}-TABLE{table_num}]の画像行が見つからない"
    m = TABLE_FIG_ID_RE.match(fig_line)
    assert m is not None, f"unexpected table image line format: {fig_line!r}"
    assert m.group(3) == f"table_p{page_num}_{table_num}.png"

    caption_lines = [l for l in lines if l.startswith(f"[P{page_num}-TABLE{table_num}-CAPTION-S")]
    assert len(caption_lines) == len(expected_sentences)

    for expected_seq, (line, expected_text) in enumerate(zip(caption_lines, expected_sentences), start=1):
        m = TABLE_CAPTION_ID_RE.match(line)
        assert m is not None, f"unexpected table caption line format: {line!r}"
        assert int(m.group(1)) == page_num
        assert int(m.group(2)) == table_num
        assert int(m.group(3)) == expected_seq
        assert expected_text in line


def test_table_captions_sample1(processed_sample1):
    """sample1.pdf（4つの表を含む）で、表キャプションがFIGとは別のTABLEラベルで
    ページ・表番号ごとに文単位で正しく振られているか。"""
    texts = processed_sample1["texts"]
    _assert_table_caption(
        texts, "page_04_en.md", 4, 1,
        ["Quantitative comparisons on BSDS500", "Our own implementation of GED, as no official code was released."],
    )
    _assert_table_caption(
        texts, "page_04_en.md", 4, 2,
        ["Quantitative comparison on the BIPED data-set", "column headers denote the percentage"],
    )
    _assert_table_caption(
        texts, "page_05_en.md", 5, 3,
        ["Quantitative comparison of wall detection", "column headers denote the percentage"],
    )
    _assert_table_caption(texts, "page_05_en.md", 5, 4, ["The effectiveness of"])


def test_table_captions_sample2(processed_sample2):
    """sample2.pdf（2つの表を含む）でも、同じTABLEキャプション形式が正しく振られているか。

    両キャプションとも"Table N: ..."とコロン区切りのため、ピリオド区切りの
    略語リストによる分割は起こらず、1文（S1）にまとまる。"""
    texts = processed_sample2["texts"]
    _assert_table_caption(
        texts, "page_06_en.md", 6, 1, ["Table 1: Correspondences between mathematical notations"]
    )
    _assert_table_caption(texts, "page_23_en.md", 23, 2, ["Table 2: Nomenclature of CPC-MS"])


def _assert_table_pngs_extracted(processed):
    """表領域が図と同様にPNGとして images/ に切り出されているかを検証する共通処理。"""
    images_dir = processed["output_dir"] / "images"
    table_pngs = sorted(images_dir.glob("table_*.png"))
    assert table_pngs, f"{images_dir} に表のPNGが見つからない"
    for png_file in table_pngs:
        data = png_file.read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        pix = fitz.Pixmap(str(png_file))
        assert pix.width > 0 and pix.height > 0


def test_table_images_are_extracted_as_png_sample1(processed_sample1):
    _assert_table_pngs_extracted(processed_sample1)


def test_table_images_are_extracted_as_png_sample2(processed_sample2):
    _assert_table_pngs_extracted(processed_sample2)


def test_unnumbered_headings_in_real_book_pdf(tmp_path):
    """test_analyze_structure_assigns_synthetic_ids_to_unnumbered_headingsが
    自作データで検証している合成章ID（"u1","u2",...）のロジックを、実際の
    sample3.pdf（印刷ページラベル55〜60、物理ページ67〜72）を処理して裏取りする。

    このページ範囲には、番号の付いていない見出し（例: "Fundamentals of
    Probability Theory"）と、番号付きの見出し（例: "1. Sampling-based
    methods:"）の両方が実際に含まれており、前者が合成ID・後者が通常の
    章番号として正しく処理されることを、process_pdfの実出力で確認する。
    CLAUDE.mdの運用規定に従い、sample3.pdfは印刷ページラベルで絞り込んだ
    最小限の範囲（6ページ分）のみを処理対象とする。
    """
    if not SAMPLE3_PDF_PATH.exists():
        pytest.skip("sample3.pdf がないためスキップ")

    start_page, end_page = resolve_physical_page_range(SAMPLE3_PDF_PATH, "55", "60")
    assert (start_page, end_page) == (67, 72)

    output_dir = tmp_path / "sample3_labels_recheck"
    md_paths = process_pdf(
        SAMPLE3_PDF_PATH, output_dir, start_page=start_page, end_page=end_page, range_label="label55-60"
    )
    texts = {p.name: p.read_text(encoding="utf-8") for p in md_paths}

    # MinerUはOCR結果の単語間にノーブレークスペース(\xa0)を挟むことがある
    # ため、この後付け見出しID検証の本質とは無関係な差異として正規化する。
    heading_lines = sorted(
        line.replace("\xa0", " ")
        for text in texts.values()
        for line in text.splitlines()
        if "-HEADING-" in line
    )

    expected_unnumbered = [
        '[P67-HEADING-u1.probabilistic] Probabilistic Generative Models: Foundational Theory for Cognitive Modeling Based on Bayesian Inference',
        '[P67-HEADING-u2.probabilistic] Probabilistic Generative Models and Bayesian Inference in Cognitive Modeling',
        '[P68-HEADING-u3.fundamentals] Fundamentals of Probability Theory',
        '[P69-HEADING-u4.bayesian] Bayesian Inference (Bayesian Estimation)',
        '[P70-HEADING-u5.graphical] Graphical Model Representation for PGMS',
        '[P71-HEADING-u6.cross] Cross-Modal Inference and Deep PGMs',
    ]
    expected_numbered = [
        '[P69-HEADING-1.sampling] 1. Sampling-based methods:',
        '[P70-HEADING-2.approximate] 2. Approximate inference methods:',
    ]
    assert heading_lines == sorted(expected_unnumbered + expected_numbered)


# ============================================================================
# 工程(4): 翻訳実行（DeepL呼び出し）
#   対応関数: deepl_translator.call_deepl のみ（build_deepl_requestsは
#   call_deeplと同じ計算をDeepLへの通信なしに行う、完全にオフラインの
#   姉妹関数）。6工程中(2)と並ぶ実行必須の工程だが、deepl.Translatorを
#   差し替えることで実DeepL APIを一切呼ばずに検証する
#   （CLAUDE.mdの規定によりpytestではDeepLの有料APIキーを消費しない）。
# ============================================================================


def _make_fake_deepl_translator_class(response_text: str = "[JA] translated"):
    calls: list[dict] = []

    class _FakeTranslator:
        def __init__(self, api_key):
            self.api_key = api_key

        def translate_text(self, text, source_lang, target_lang, context, preserve_formatting, split_sentences):
            calls.append({"api_key": self.api_key, "text": text, "context": context})
            return SimpleNamespace(text=response_text)

    return _FakeTranslator, calls


def test_call_deepl_raises_when_api_key_missing():
    units = [DocUnit(tag="P1-S1-body-S1", kind="body_sentence", page=1, en_text="Hello.", translatable=True)]
    with pytest.raises(TranslationBackendError):
        call_deepl(units, api_key=None, document_context="")


def test_call_deepl_protects_math_and_builds_context_history(monkeypatch):
    fake_class, calls = _make_fake_deepl_translator_class()
    monkeypatch.setattr(deepl_translator.deepl, "Translator", fake_class)

    units = [
        DocUnit(
            tag="P1-S1-body-S1", kind="body_sentence", page=1, en_text="We use $x$ as input.", translatable=True
        ),
        DocUnit(tag="P1-S2-body-S2", kind="body_sentence", page=1, en_text="It works well.", translatable=True),
        DocUnit(tag="P1-FIG1", kind="figure_image", page=1, translatable=False),
    ]
    raw_results = call_deepl(units, api_key="dummy-key", document_context="Doc summary.")

    # 非翻訳対象unit（figure_image）はDeepLに送られない
    assert set(raw_results) == {"P1-S1-body-S1", "P1-S2-body-S2"}
    assert len(calls) == 2

    # 数式スパンがプレースホルダへ退避された状態でDeepLへ送られる
    assert calls[0]["text"] == "We use __MATH0__ as input."
    assert raw_results["P1-S1-body-S1"].math_spans == ["$x$"]

    # 文脈: 1文目はドキュメント全体の要約のみ、2文目はそれに直近履歴が続く
    assert calls[0]["context"] == "Doc summary."
    assert calls[1]["context"] == "Doc summary.\n\nWe use __MATH0__ as input."


def test_call_deepl_wraps_deepl_exception_as_backend_error(monkeypatch):
    class _RaisingTranslator:
        def __init__(self, api_key):
            pass

        def translate_text(self, *args, **kwargs):
            raise deepl_translator.deepl.DeepLException("quota exceeded")

    monkeypatch.setattr(deepl_translator.deepl, "Translator", _RaisingTranslator)
    units = [DocUnit(tag="P1-S1-body-S1", kind="body_sentence", page=1, en_text="Hello.", translatable=True)]

    with pytest.raises(TranslationBackendError):
        call_deepl(units, api_key="dummy-key", document_context="")


def test_build_deepl_requests_computes_same_protected_text_and_context_as_call_deepl():
    """build_deepl_requestsはDeepLへ通信しない姉妹関数だが、call_deeplと
    同じ決定的な計算（protect + 直近履歴の連結）を行うはずである。"""
    units = [
        DocUnit(
            tag="P1-S1-body-S1", kind="body_sentence", page=1, en_text="We use $x$ as input.", translatable=True
        ),
        DocUnit(tag="P1-S2-body-S2", kind="body_sentence", page=1, en_text="It works well.", translatable=True),
    ]
    requests = build_deepl_requests(units, document_context="Doc summary.")

    assert requests["P1-S1-body-S1"].protected_text == "We use __MATH0__ as input."
    assert requests["P1-S1-body-S1"].context_text == "Doc summary."
    assert requests["P1-S2-body-S2"].context_text == "Doc summary.\n\nWe use __MATH0__ as input."


@pytest.mark.parametrize("pdf_path,start_page,end_page,run_id,range_label", _REAL_SAMPLE_SCENARIOS)
def test_build_deepl_requests_on_real_sample_structured_content(
    tmp_path, pdf_path, start_page, end_page, run_id, range_label
):
    """実サンプルPDF（sample0〜3）をprocess_pdf（MinerUキャッシュ経由・
    DeepL不使用）で構造化した上で、build_deepl_requestsが実際の複雑な
    本文・数式混じりテキストに対しても、数式デリミタ$を含まない
    protected_textを組み立てられるかを確認する実データ・スモークテスト。
    DeepLへは一切通信しない（build_deepl_requestsは通信しない関数）ため、
    run_idで対象サンプルを選べるよう_REAL_SAMPLE_SCENARIOSで
    パラメータ化してある。
    """
    if not pdf_path.exists():
        pytest.skip(f"{pdf_path} がないためスキップ")

    output_dir = tmp_path / "real_sample_output"
    md_paths = process_pdf(pdf_path, output_dir, start_page=start_page, end_page=end_page, range_label=range_label)
    units = parse_output_dir(output_dir)
    document_context = build_document_context(units)

    requests = build_deepl_requests(units, document_context)

    assert requests, f"{run_id} から翻訳対象unitが1件も抽出されなかった"
    for tag, request in requests.items():
        assert "$" not in request.protected_text, (
            f"{tag} のprotected_textに数式デリミタ$が残っている"
            "（数式スパンのプレースホルダ退避に失敗している可能性がある）"
        )
    assert md_paths


def test_run_translation_snapshot_writes_pre_protection_pages(tmp_path, monkeypatch):
    """run_translationのsnapshot_dir機能が、05_postprocess/へ
    units_raw.jsonと同じ状態（apply_restore直後・protect_confirmed_
    single_letter_leaks適用前）のpage_XX_en.md/page_XX_ja.mdも書き出す
    ことを確認する。DeepLはモックする。

    翻訳後処理（protect_confirmed_single_letter_leaksによる単体アルファ
    ベット数式変数への$...$保護）はrun_translationの外（translate_and_
    export側）で行われるため、ここで書き出されるen.md/ja.mdには反映
    されていないはずである（保護済み版はoutput_dir側のみに存在する）。
    """
    units = [
        DocUnit(
            tag="P1-S1-body-S1",
            kind="body_sentence",
            page=1,
            en_text="We decode a latent z back to an edge map.",
            ja_text="",
            translatable=True,
        )
    ]

    def _fake_call_deepl(units, api_key, document_context, log=print):
        return {
            unit.tag: RawDeeplResult(raw_text="潜在変数zをエッジマップにデコードする。", math_spans=[])
            for unit in units
        }

    monkeypatch.setattr(translate_paper, "call_deepl", _fake_call_deepl)

    snapshot_dir = tmp_path / "snapshot"
    translate_paper.run_translation(units, document_context="", snapshot_dir=snapshot_dir)

    postprocess_dir = snapshot_dir / "05_postprocess"
    page_en_text = (postprocess_dir / "page_01_en.md").read_text(encoding="utf-8")
    page_ja_text = (postprocess_dir / "page_01_ja.md").read_text(encoding="utf-8")

    assert "[P1-S1-body-S1] We decode a latent z back to an edge map." in page_en_text
    assert "[P1-S1-body-S1] 潜在変数zをエッジマップにデコードする。" in page_ja_text
    assert "$z$" not in page_en_text
    assert "$z$" not in page_ja_text

    units_raw = json.loads((postprocess_dir / "units_raw.json").read_text(encoding="utf-8"))
    assert units_raw[0]["ja_text"] == "潜在変数zをエッジマップにデコードする。"


# ============================================================================
# 工程(5): 翻訳後処理
#   対応関数: apply_restore / check_unprotected_math_survival /
#     protect_confirmed_single_letter_leaks / report_untranslated_
#     fragment_candidates / write_translated_pages。いずれもDeepLへの
#   再通信を伴わない決定的な処理のため、自作のDocUnit・RawDeeplResultで
#   完全にオフライン検証できる。
# ============================================================================


def test_apply_restore_writes_ja_text_from_raw_results_with_math_restored():
    units = [
        DocUnit(tag="P1-S1-body-S1", kind="body_sentence", page=1, en_text="We use $x$.", ja_text="", translatable=True),
        DocUnit(tag="P1-FIG1", kind="figure_image", page=1, ja_text="", translatable=False),
    ]
    raw_results = {"P1-S1-body-S1": RawDeeplResult(raw_text="__MATH0__を使用します。", math_spans=["$x$"])}

    apply_restore(units, raw_results)

    assert units[0].ja_text == "$x$を使用します。"
    assert units[1].ja_text == ""  # 非翻訳対象unitは書き換えない


@pytest.mark.parametrize("run_id", ["sample0_full", "sample1_full", "sample2_full", "sample3_label55-60"])
def test_apply_restore_matches_cached_deepl_output(run_id):
    """apply_restoreを、cache/<run_id>/real_deepl_output_<タイムスタンプ>/に
    凍結された実際のDeepL実行結果と突き合わせてオフラインで検証する
    （DeepLを一切呼ばない。意図的にテスト名へ"real_deepl"を含めていない。
    CLAUDE.mdの実行前確認ルールは名前に"real_deepl"を含むテストが対象
    のため、無課金の本テストが誤ってその対象に含まれないようにするため）。

    自作データによる上記のround-tripテストとは異なり、本テストは
    `04_deepl_output/raw_deepl_results.json`（DeepLの生応答、restore適用前）
    と`05_postprocess/units_raw.json`（restore直後に凍結されたDocUnit
    スナップショット、独立した正解データ）という、実DeepL実行時にのみ
    生成される永続キャッシュを使う。raw_deepl_results.jsonにapply_restore
    を通した結果を自分自身の正解データにするのではなく、独立に凍結された
    units_raw.jsonのja_textと比較することで、apply_restore自体に
    リグレッションが入っても検知できる（同じ関数の出力を自分自身の正解
    データにしてしまうと、そのリグレッションを検知できなくなるため）。

    CLAUDE.mdの規定により実DeepLテストはsample0/sample1限定のため、現時点
    ではsample2/sample3は対応するキャッシュが無くskipされる。将来
    sample2/sample3向けにキャッシュが用意された場合、コードの変更無しに
    自動的に実行対象になる（サンプルを選べるようにするための
    parametrize）。同じrun_idで複数回実行されたキャッシュがある場合は
    最も新しいものを使う（`_find_latest_real_deepl_cache`参照）。
    """
    cache_root = _find_latest_real_deepl_cache(run_id)
    raw_path = cache_root / "04_deepl_output" / "raw_deepl_results.json" if cache_root else None
    units_raw_path = cache_root / "05_postprocess" / "units_raw.json" if cache_root else None
    if cache_root is None or not raw_path.exists() or not units_raw_path.exists():
        pytest.skip(
            f"{run_id} の実DeepLキャッシュが無いためスキップ"
            "（test_run_pipeline_end_to_end_with_real_deeplの実行、または"
            "translate_paper.pyでの本実行により生成される）"
        )

    raw_results = {
        tag: RawDeeplResult(raw_text=data["raw_text"], math_spans=data["math_spans"])
        for tag, data in json.loads(raw_path.read_text(encoding="utf-8")).items()
    }
    units_raw_data = json.loads(units_raw_path.read_text(encoding="utf-8"))
    expected_ja_by_tag = {d["tag"]: d["ja_text"] for d in units_raw_data}

    # translatable=Falseのunit（著者名・LaTeX数式等）はparse_output_dir時点で
    # ja_text=en_textが設定済みで、apply_restoreはtranslatable=Trueのunitしか
    # 書き換えない。translatableなunitだけja_textを空にリセットする。
    units = [DocUnit(**{**d, "ja_text": "" if d["translatable"] else d["ja_text"]}) for d in units_raw_data]
    apply_restore(units, raw_results)

    actual_ja_by_tag = {u.tag: u.ja_text for u in units}
    assert actual_ja_by_tag == expected_ja_by_tag


@pytest.mark.parametrize(
    "text,expected",
    [
        ("We integrate from t = 1 to t = 0.", ["t = 1", "t = 0"]),
        ("$t = 1$ is protected by dollar signs.", []),
        ("No math-like pattern here at all.", []),
        ("K discrete steps (no equals sign, not detected).", []),
    ],
)
def test_find_unprotected_math_like_tokens(text, expected):
    """$...$で保護されていない"文字=英数字"パターンの検出テスト。"""
    assert find_unprotected_math_like_tokens(text) == expected


@pytest.mark.parametrize("run_id", ["sample0_full", "sample1_full", "sample2_full", "sample3_label55-60"])
def test_check_unprotected_math_survival_on_real_translated_sample(run_id):
    """cache/<sample>/real_deepl_output/05_postprocess/units_raw.jsonに
    凍結された、実際にDeepLで翻訳済みのDocUnitに対してcheck_unprotected_
    math_survivalを実行する実データ・スモークテスト。DeepLの翻訳結果は
    実行のたびに言い回しが変わりうるため厳密な期待件数はハードコードせず、
    「例外を出さず完走し、戻り値の型・メッセージ形式が正しいこと」のみを
    確認する（内容そのものの正誤はCLAUDE.mdの許可リスト運用に従う
    test_page_ja_md_and_candidate_detection_against_real_deeplが担当する）。
    CLAUDE.mdの規定により実DeepLはsample0/sample1限定のため、現時点では
    sample2/sample3はキャッシュが無くskipされる（サンプルを選べるように
    するためのparametrize）。同じrun_idで複数回実行されたキャッシュがある
    場合は最も新しいものを使う（`_find_latest_real_deepl_cache`参照）。
    """
    cache_root = _find_latest_real_deepl_cache(run_id)
    units_raw_path = cache_root / "05_postprocess" / "units_raw.json" if cache_root else None
    if units_raw_path is None or not units_raw_path.exists():
        pytest.skip(
            f"{run_id} の実DeepLキャッシュが無いためスキップ"
            "（該当サンプルでtest_page_ja_md_and_candidate_detection_against_real_deeplを"
            "一度実行すると生成される）"
        )

    units = [DocUnit(**d) for d in json.loads(units_raw_path.read_text(encoding="utf-8"))]
    warnings = check_unprotected_math_survival(units, log=lambda _msg: None)

    assert isinstance(warnings, list)
    assert all(w.startswith("[警告]") for w in warnings)


def test_check_unprotected_math_survival_warns_when_unprotected_token_disappears():
    """未保護の数式らしき文字列が翻訳後に消えている場合、警告が出るか。"""
    units = [
        DocUnit(
            tag="P1-S1-intro-S1",
            kind="body_sentence",
            page=1,
            en_text="We integrate from t = 1 to t = 0.",
            ja_text="ノイズから清潔な状態まで積分する。",  # t = 1 / t = 0 が消えている
            translatable=True,
        )
    ]
    warnings = check_unprotected_math_survival(units, log=lambda _msg: None)
    assert len(warnings) == 2
    assert "t = 1" in warnings[0]
    assert "t = 0" in warnings[1]


def test_check_unprotected_math_survival_silent_when_token_preserved():
    """未保護の数式らしき文字列が翻訳後も残っていれば、警告が出ないか。"""
    units = [
        DocUnit(
            tag="P1-S1-intro-S1",
            kind="body_sentence",
            page=1,
            en_text="We integrate from t = 1 to t = 0.",
            ja_text="t = 1からt = 0まで積分する。",  # 両方とも保持されている
            translatable=True,
        )
    ]
    assert check_unprotected_math_survival(units, log=lambda _msg: None) == []


def test_check_unprotected_math_survival_skips_non_translatable_units():
    """翻訳対象外のunitは、en_text/ja_textが食い違っていても警告しないか。"""
    units = [
        DocUnit(tag="P1-EQ1-LATEX", kind="equation_latex", page=1, en_text="t = 1", ja_text="", translatable=False)
    ]
    assert check_unprotected_math_survival(units, log=lambda _msg: None) == []


@pytest.mark.parametrize(
    "ja_text,expected",
    [
        ("潜在変数zをエッジマップにデコードし、t=1からt=0まで積分する。", ["z", "t=1", "t=0"]),
        # スペースを挟む"t = 1"も、途中で分断されず1つの断片としてまとまるか
        ("誘導された常微分方程式(ODE)を t = 1 から t = 0 まで積分する。", ["(ODE)", "t = 1", "t = 0"]),
        ("K個の離散ステップを用いて", ["K"]),
        ("条件付き生成 $p ( y \\mid x )$ として定式化し", []),  # 保護済み数式は候補にならない
        ("これは完全に日本語だけの文です。", []),
        # ギリシャ文字も、半角英数字と同様にDeepLが翻訳せず残す数式記号のため検出対象に含む。
        ("推論時にはスケールγを用いた分類器フリーガイダンスによって", ["γ"]),
    ],
)
def test_find_untranslated_fragment_candidates(ja_text, expected):
    """翻訳後も半角のまま残る、未検出の数式らしき候補の検出テスト。"""
    assert find_untranslated_fragment_candidates(ja_text) == expected


def test_report_untranslated_fragment_candidates_skips_non_translatable_units():
    """翻訳対象外のunitは、半角断片が残っていても候補にならないか。"""
    units = [DocUnit(tag="P1-EQ1-LATEX", kind="equation_latex", page=1, en_text="z", ja_text="z", translatable=False)]
    assert report_untranslated_fragment_candidates(units, log=lambda _msg: None) == []


def test_protect_confirmed_single_letter_leaks_wraps_matching_occurrences():
    """未保護の単体アルファベット（例:"z"）が、en_text・ja_text双方の
    同じ箇所で$...$に置き換わるか。DeepLへの再翻訳は行わないため、
    テスト内でcall_deepl/apply_restoreは一切呼ばない。"""
    units = [
        DocUnit(
            tag="P1-S1-body-S1",
            kind="body_sentence",
            page=1,
            en_text="We decode a latent z back to an edge map.",
            ja_text="潜在変数zをエッジマップにデコードする。",
            translatable=True,
        )
    ]
    messages = protect_confirmed_single_letter_leaks(units, log=lambda _msg: None)
    assert len(messages) == 1
    assert "'z'" in messages[0]
    assert units[0].en_text == "We decode a latent $z$ back to an edge map."
    assert units[0].ja_text == "潜在変数$z$をエッジマップにデコードする。"


def test_protect_confirmed_single_letter_leaks_does_not_touch_existing_math_spans():
    """既に$...$で保護済みの数式スパン内部の同名文字（"y"）を二重に$で
    囲まず、スパン外のstandaloneな"x"だけを保護することを確認する。"""
    units = [
        DocUnit(
            tag="P2-S6-2.1.preliminaries-S1",
            kind="body_sentence",
            page=2,
            en_text="Given input image x, we formulate it as $p ( y \\mid x )$.",
            ja_text="入力画像xが与えられたとき、$p ( y \\mid x )$として定式化する。",
            translatable=True,
        )
    ]
    messages = protect_confirmed_single_letter_leaks(units, log=lambda _msg: None)

    assert len(messages) == 1
    assert units[0].en_text == "Given input image $x$, we formulate it as $p ( y \\mid x )$."
    assert units[0].ja_text == "入力画像$x$が与えられたとき、$p ( y \\mid x )$として定式化する。"


def test_protect_confirmed_single_letter_leaks_skips_multi_char_tokens():
    """"DiT"のような複数文字のトークンは、単体アルファベットではないため
    保護されず、en_text/ja_textが変化しないことを確認する。"""
    units = [
        DocUnit(
            tag="P2-FIG1-CAPTION-S3",
            kind="caption_sentence",
            page=2,
            en_text="a frozen DiT-based foundation model",
            ja_text="凍結されたDiTベースのファウンデーションモデル",
            translatable=True,
        )
    ]
    original_en, original_ja = units[0].en_text, units[0].ja_text
    messages = protect_confirmed_single_letter_leaks(units, log=lambda _msg: None)
    assert messages == []
    assert units[0].en_text == original_en
    assert units[0].ja_text == original_ja


def test_protect_confirmed_single_letter_leaks_skips_non_translatable_units():
    """翻訳対象外のunitは、単体アルファベットが残っていても保護しないか。"""
    units = [DocUnit(tag="P1-EQ1-LATEX", kind="equation_latex", page=1, en_text="z", ja_text="z", translatable=False)]
    original_en = units[0].en_text
    assert protect_confirmed_single_letter_leaks(units, log=lambda _msg: None) == []
    assert units[0].en_text == original_en


def test_report_untranslated_fragment_candidates_returns_info_messages():
    """候補が見つかった場合、[警告]ではなく[情報]としてログ出力されるか。"""
    units = [
        DocUnit(
            tag="P1-S1-intro-S1",
            kind="body_sentence",
            page=1,
            en_text="We decode a latent z back to an edge map.",
            ja_text="潜在変数zをエッジマップにデコードする。",
            translatable=True,
        )
    ]
    messages = []
    result = report_untranslated_fragment_candidates(units, log=messages.append)
    assert result == messages
    assert len(result) == 1
    assert "[情報]" in result[0]
    assert "z" in result[0]


def test_write_translated_pages_preserves_tag_format(tmp_path):
    """write_translated_pagesが、page_XX_en.mdと同じタグ形式でja_textを
    書き出せているかを、自作unitsで直接検証する。process_pdf/MinerU/DeepLを
    一切経由しないため高速。"""
    units = [
        DocUnit(tag="P1-TITLE", kind="title", page=1, en_text="A Title", ja_text="タイトル"),
        DocUnit(
            tag="P1-FIG1",
            kind="figure_image",
            page=1,
            image_rel_path="images/fig_p1_1.png",
        ),
        DocUnit(
            tag="P1-FIG1-CAPTION-S1",
            kind="caption_sentence",
            page=1,
            en_text="Fig. 1: Example.",
            ja_text="図1: 例。",
        ),
        DocUnit(tag="P2-S1-body-S1", kind="body_sentence", page=2, en_text="Body.", ja_text="本文。"),
    ]

    output_dir = tmp_path / "write_translated_pages_output"
    output_dir.mkdir()
    written = write_translated_pages(units, output_dir)

    assert [p.name for p in written] == [
        "page_01_en.md",
        "page_01_ja.md",
        "page_02_en.md",
        "page_02_ja.md",
    ]

    page1_en_text = (output_dir / "page_01_en.md").read_text(encoding="utf-8")
    assert "[P1-TITLE] A Title" in page1_en_text
    assert "![P1-FIG1](images/fig_p1_1.png) [P1-FIG1]" in page1_en_text
    assert "[P1-FIG1-CAPTION-S1] Fig. 1: Example." in page1_en_text

    page1_ja = (output_dir / "page_01_ja.md").read_text(encoding="utf-8")
    assert "[P1-TITLE] タイトル" in page1_ja
    assert "![P1-FIG1](images/fig_p1_1.png) [P1-FIG1]" in page1_ja
    assert "[P1-FIG1-CAPTION-S1] 図1: 例。" in page1_ja

    page2_en_text = (output_dir / "page_02_en.md").read_text(encoding="utf-8")
    assert "[P2-S1-body-S1] Body." in page2_en_text

    page2_ja = (output_dir / "page_02_ja.md").read_text(encoding="utf-8")
    assert "[P2-S1-body-S1] 本文。" in page2_ja


# ============================================================================
# 工程(6): PDF生成
#   対応関数: pdf_renderer.build_blocks / pdf_renderer.render_all_pdfs。
#   モック翻訳済みの自作DocUnitを使えば独立テスト可能（MinerU・DeepL
#   いずれも経由しない）。render_all_pdfsはPlaywrightでローカルに
#   PDF化するだけで外部APIも課金も発生しない。
# ============================================================================


def test_build_blocks_groups_consecutive_body_sentences_into_one_paragraph():
    units = [
        DocUnit(tag="P1-TITLE", kind="title", page=1, en_text="A Title", ja_text="タイトル"),
        DocUnit(tag="P1-HEADING-1.introduction", kind="heading", page=1, en_text="1. INTRODUCTION", ja_text="1. はじめに"),
        DocUnit(tag="P1-S1-1.introduction-S1", kind="body_sentence", page=1, en_text="First.", ja_text="一つ目。"),
        DocUnit(tag="P1-S2-1.introduction-S2", kind="body_sentence", page=1, en_text="Second.", ja_text="二つ目。"),
        DocUnit(tag="P1-HEADING-2.method", kind="heading", page=1, en_text="2. METHOD", ja_text="2. 手法"),
        DocUnit(tag="P1-S1-2.method-S1", kind="body_sentence", page=1, en_text="Third.", ja_text="三つ目。"),
    ]
    blocks = build_blocks(units, output_dir=Path("."))

    assert [b.kind for b in blocks] == ["title", "heading", "paragraph", "heading", "paragraph"]
    assert len(blocks[2].sentences) == 2  # 同じ章内の2文が1つのparagraphブロックにまとまる
    assert len(blocks[4].sentences) == 1


def test_build_blocks_assigns_heading_level_from_section_number_depth():
    units = [
        DocUnit(tag="P1-HEADING-1.introduction", kind="heading", page=1, en_text="1. INTRO", ja_text="1. はじめに"),
        DocUnit(tag="P1-HEADING-2.1.method", kind="heading", page=1, en_text="2.1 METHOD", ja_text="2.1 手法"),
    ]
    blocks = build_blocks(units, output_dir=Path("."))
    assert [b.level for b in blocks] == [2, 3]


def test_build_blocks_skips_equation_image_and_keeps_equation_latex_block():
    """equation_imageは対応するequation_latexブロックがKaTeXで描画する
    ため、build_blocksの時点で出力から除かれる。"""
    units = [
        DocUnit(tag="P1-EQ1", kind="equation_image", page=1, image_rel_path="images/eq_p1_1.png"),
        DocUnit(tag="P1-EQ1-LATEX", kind="equation_latex", page=1, en_text="x = 1", ja_text="x = 1"),
    ]
    blocks = build_blocks(units, output_dir=Path("."))
    assert [b.kind for b in blocks] == ["equation"]


def test_render_all_pdfs_produces_three_nonempty_pdfs_with_japanese_text(tmp_path):
    units = [
        DocUnit(tag="P1-TITLE", kind="title", page=1, en_text="A Title", ja_text="タイトル"),
        DocUnit(tag="P1-S1-body-S1", kind="body_sentence", page=1, en_text="Hello.", ja_text="こんにちは。"),
    ]
    blocks = build_blocks(units, output_dir=tmp_path)

    pdf_paths = render_all_pdfs(blocks, tmp_path, log=lambda _msg: None)

    assert [p.name for p in pdf_paths] == ["paper_bilingual.pdf", "paper_en.pdf", "paper_ja.pdf"]
    for path in pdf_paths:
        assert path.exists()
        assert path.stat().st_size > 0

    ja_pdf_path = next(p for p in pdf_paths if p.name == "paper_ja.pdf")
    with fitz.open(ja_pdf_path) as doc:
        ja_text = "".join(page.get_text() for page in doc)
    assert "こんにちは" in ja_text


@pytest.mark.parametrize("run_id", ["sample0_full", "sample1_full", "sample2_full", "sample3_label55-60"])
def test_render_all_pdfs_on_real_translated_sample(tmp_path, run_id):
    """cache/<sample>/real_deepl_output/05_postprocess/units_raw.jsonに
    凍結された、実際にDeepLで翻訳済みのDocUnitをbuild_blocks/render_all_pdfs
    に通す実データ・スモークテスト。図表画像自体（image_rel_path先の
    PNGファイル）はcache/配下に永続化されていないため、figure_image種別の
    unitは対象から除外し、テキスト系要素（タイトル・見出し・本文・数式
    LaTeX等）のレンダリングのみを対象にする。CLAUDE.mdの規定により実DeepL
    はsample0/sample1限定のため、現時点ではsample2/sample3はキャッシュが
    無くskipされる（サンプルを選べるようにするためのparametrize）。同じ
    run_idで複数回実行されたキャッシュがある場合は最も新しいものを使う
    （`_find_latest_real_deepl_cache`参照）。
    """
    cache_root = _find_latest_real_deepl_cache(run_id)
    units_raw_path = cache_root / "05_postprocess" / "units_raw.json" if cache_root else None
    if units_raw_path is None or not units_raw_path.exists():
        pytest.skip(
            f"{run_id} の実DeepLキャッシュが無いためスキップ"
            "（該当サンプルでtest_page_ja_md_and_candidate_detection_against_real_deeplを"
            "一度実行すると生成される）"
        )

    all_units = [DocUnit(**d) for d in json.loads(units_raw_path.read_text(encoding="utf-8"))]
    units = [u for u in all_units if u.kind != "figure_image"]

    blocks = build_blocks(units, output_dir=tmp_path)
    assert blocks, f"{run_id} からブロックが1つも組み立てられなかった"

    pdf_paths = render_all_pdfs(blocks, tmp_path, log=lambda _msg: None)
    assert [p.name for p in pdf_paths] == ["paper_bilingual.pdf", "paper_en.pdf", "paper_ja.pdf"]
    for path in pdf_paths:
        assert path.exists()
        assert path.stat().st_size > 0


# ============================================================================
# 全体テスト（工程(1)〜(6)を実際に1本のテストで通しで検証する）
#   これまでの統合テスト（下記セクション）は工程(3)〜(6)（
#   translate_and_export経由）までしかカバーしておらず、工程(1)
#   （resolve_page_range、CLIオプション→ページ範囲の解決）を含めて本当に
#   最初から最後まで通しで動くかを検証するテストは、旧ファイルの時点から
#   一度も存在しなかった（explain.txtにも「CLIレベルのエンドツーエンド
#   テストは実施していない」と明記されていた）。このテストがその唯一の
#   例外で、resolve_page_range（工程1）→run_pipeline（内部でprocess_pdf
#   ＝工程2・3、translate_and_export＝工程3〜6を順に呼ぶ）という、
#   main()が実際に呼ぶのと同じ関数の並びをそのまま実行する。
#
#   main()自体（argparse＋sys.stdout.reconfigure）は経由しない。pytestの
#   標準出力キャプチャ環境下でreconfigure()を呼ぶと不安定になりうる
#   ためで、それ以外の実質的なロジック（ページ範囲解決以降）は
#   resolve_page_range+run_pipelineの呼び出しで完全にカバーされる。
#
#   モック版の出力はpytestの一時ディレクトリ（tmp_path）に書き、テスト
#   終了後に自動的に消える。無課金で気軽に何度も実行されるテストなので、
#   実行のたびにoutput/へフォルダが溜まっていくのを避けるためである。
#   一方、下にある実DeepL版は、DeepLの応答が再現不可能な貴重なデータ
#   であるため、output/・cache/双方に永続化する（詳細は当該テストの
#   docstring参照）。命名規則は
#   `pytest_{入力ファイル名}_{範囲指定オプション名&範囲}_{タイムスタンプ}`
#   で統一する（"pytest"はpytestが生成したフォルダであることを表す接頭辞。
#   人間による手動実行の成果物（`output/manual_...`、testExplain.txtの
#   「本実行」参照）とは区別される）。
# ============================================================================


@pytest.mark.parametrize(
    "pdf_path,start_label,end_label,run_id,range_label",
    [
        (SAMPLE_PDF_PATH, None, None, "sample0", "full"),
        (SAMPLE1_PDF_PATH, None, None, "sample1", "full"),
        (SAMPLE2_PDF_PATH, None, None, "sample2", "full"),
        (SAMPLE3_PDF_PATH, "55", "60", "sample3", "label55-60"),
    ],
    ids=["sample0", "sample1", "sample2", "sample3"],
)
def test_run_pipeline_end_to_end_with_mocked_deepl(
    tmp_path, pdf_path, start_label, end_label, run_id, range_label, monkeypatch
):
    """工程(1)〜(6)を実際につなげて通しで検証する唯一の全体テスト。

    sample3のみ`--start-label 55 --end-label 60`相当（resolve_page_range
    経由）を指定し、ラベル解決の結果が実際にMinerU実行・翻訳・PDF生成まで
    正しくつながることを初めて検証する（他のテストはラベル解決の結果を
    数値として検証するのみで、実際にパイプラインへ流し込むところまでは
    検証していなかった）。sample0/1/2は範囲指定無し（全文処理）。

    range_labelは、cache/配下の実際のMinerUキャッシュフォルダ名
    （sample0_full等）と一致させるために必須。これを渡さないと
    process_pdf内部のmineru_cacheがヒットせず、毎回実際にMinerUを
    再実行してしまう（無課金で気軽に回せるはずのテストが実質的に重い
    処理になってしまうため、他のテストと同じ命名規則に必ず揃えること）。

    無課金で気軽に何度も実行されるテストのため、出力はpytestの一時
    ディレクトリ（tmp_path）に書き、テスト終了後に自動的に消える
    （output/には残さない。永続化する必要がある場合は下の実DeepL版を
    参照）。DeepLをモックしているため、run_pipelineには
    save_deepl_snapshot=Falseを渡す（Trueのままだと、モックの訳文が
    cache/配下の実DeepL凍結データ用フォルダに誤って書き込まれ、実データ
    として後続のオフライン回帰テストに混入してしまう）。
    """
    if not pdf_path.exists():
        pytest.skip(f"{pdf_path} がないためスキップ")

    start_page, end_page = resolve_page_range(pdf_path, None, None, None, start_label, end_label)

    def _fake_call_deepl(units, api_key, document_context, log=print):
        return {
            unit.tag: RawDeeplResult(raw_text="これは全体テスト用の日本語訳です。", math_spans=[])
            for unit in units
            if unit.translatable
        }

    monkeypatch.setattr(translate_paper, "call_deepl", _fake_call_deepl)

    output_dir = tmp_path / f"e2e_{run_id}"
    pdf_paths = translate_paper.run_pipeline(
        pdf_path,
        output_dir,
        start_page=start_page,
        end_page=end_page,
        range_label=range_label,
        save_deepl_snapshot=False,
    )

    assert len(pdf_paths) == 3
    for path in pdf_paths:
        assert path.exists(), f"{path} が生成されていない"
        assert path.stat().st_size > 0, f"{path} が空ファイルになっている"

    ja_pdf_path = next(p for p in pdf_paths if p.name == "paper_ja.pdf")
    with fitz.open(ja_pdf_path) as doc:
        ja_text = "".join(page.get_text() for page in doc)
    has_japanese = any("぀" <= ch <= "ヿ" or "一" <= ch <= "鿿" for ch in ja_text)
    assert has_japanese, "paper_ja.pdfに日本語文字が見つからない（翻訳が行われていない可能性がある）"


@pytest.mark.parametrize(
    "pdf_path,start_label,end_label,run_id,range_label",
    [
        (SAMPLE_PDF_PATH, None, None, "sample0", "full"),
        (SAMPLE1_PDF_PATH, None, None, "sample1", "full"),
    ],
    ids=["sample0", "sample1"],
)
def test_run_pipeline_end_to_end_with_real_deepl(pdf_path, start_label, end_label, run_id, range_label):
    """工程(1)〜(6)を、DeepLも含めて一切モックせず実際に通しで検証する
    全体テスト（上のtest_run_pipeline_end_to_end_with_mocked_deeplの
    実DeepL版）。resolve_page_range（工程1）→run_pipeline（工程2〜6）を
    main()と全く同じ呼び出し順・同じ関数で実行する。

    CLAUDE.mdの例外規定により対象はsample0/sample1限定（他の実DeepL
    テストと同じ制約）。実行するたびに実際に課金が発生するため、
    人間の事前確認を得てから実行すること（CLAUDE.mdの実行前確認規定
    参照。日常の開発ループでは実行しない）。

    出力はpytestの一時ディレクトリではなく、output/配下の
    `pytest_{run_id}_{range_label}_{タイムスタンプ}`フォルダに残す。実際の
    DeepL翻訳結果は再現不可能なため、同じ組み合わせで再実行しても過去の
    結果を上書きしない。

    さらに、run_pipeline自体が備えるスナップショット機能（
    translate_and_exportのpdf_path/range_label引数。人間による本実行でも
    同様に動作する）により、DeepLとの実際の送受信内容が自動的に
    cache/<pdf_pathのstem>_<range_label>/real_deepl_output_
    <タイムスタンプ>/へ03_structured/04_deepl_input/04_deepl_output/
    05_postprocessの形式で記録される。このテストはmain()と全く同じ
    run_pipeline呼び出し1回だけで完結する（DeepLとの送受信を記録する
    ためのラッパー処理はtranslate_paper.py側の本番コードが担っており、
    テスト側で個別に用意する必要はない）。"""
    if not pdf_path.exists():
        pytest.skip(f"{pdf_path} がないためスキップ")

    load_dotenv()
    if os.environ.get("DEEPL_API_KEY") is None:
        pytest.skip("DEEPL_API_KEY が未設定のためスキップ")

    start_page, end_page = resolve_page_range(pdf_path, None, None, None, start_label, end_label)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path("output") / f"pytest_{run_id}_{range_label}_{timestamp}"

    pdf_paths = translate_paper.run_pipeline(
        pdf_path, output_dir, start_page=start_page, end_page=end_page, range_label=range_label
    )

    assert len(pdf_paths) == 3
    for path in pdf_paths:
        assert path.exists(), f"{path} が生成されていない"
        assert path.stat().st_size > 0, f"{path} が空ファイルになっている"

    ja_pdf_path = next(p for p in pdf_paths if p.name == "paper_ja.pdf")
    with fitz.open(ja_pdf_path) as doc:
        ja_text = "".join(page.get_text() for page in doc)
    has_japanese = any("぀" <= ch <= "ヿ" or "一" <= ch <= "鿿" for ch in ja_text)
    assert has_japanese, "paper_ja.pdfに日本語文字が見つからない（翻訳が行われていない可能性がある）"

    cache_root = _find_latest_real_deepl_cache(f"{pdf_path.stem}_{range_label}")
    assert cache_root is not None, "run_pipelineによるreal_deepl_outputスナップショットが見つからない"
    assert (cache_root / "04_deepl_input" / "deepl_input.json").exists()
    assert (cache_root / "04_deepl_output" / "raw_deepl_results.json").exists()
    assert (cache_root / "05_postprocess" / "units_raw.json").exists()


# ============================================================================
# 統合テスト（工程横断）
#   工程(1)〜(6)のいずれか1つには分類できないテスト群。全て
#   translate_paper.translate_and_export（工程(3)〜(6)をまとめて実行する
#   関数）を丸ごと呼ぶため、複数工程の連携そのものを検証対象にしている。
#   無理に単一工程の節に押し込めると実態と説明文がズレるため、あえて
#   工程(1)〜(6)とは別枠のセクションとして独立させている。
#
#   3件とも無課金（DeepLを一切呼ばない）。うち
#   test_translate_and_export_with_mocked_deeplはDeepLをモックしてsample0
#   〜3を`-k`で選んで実行できる（sample3のみ印刷ページラベル55〜60の範囲に
#   限定）。test_untranslated_fragment_candidates_against_cached_deepl_
#   outputは、実際にDeepLを呼ぶ処理
#   （test_run_pipeline_end_to_end_with_real_deeplまたは人間による本実行）
#   が残したキャッシュを読むだけで、DeepLは呼ばない。
# ============================================================================


@pytest.mark.parametrize(
    "processed_fixture_name,run_id",
    [
        ("processed", "sample0"),
        ("processed_sample1", "sample1"),
        ("processed_sample2", "sample2"),
        ("processed_sample3", "sample3_label55-60"),
    ],
    ids=["sample0", "sample1", "sample2", "sample3_label55-60"],
)
def test_translate_and_export_with_mocked_deepl(request, processed_fixture_name, run_id, monkeypatch):
    """CLAUDE.mdの規定により、pytestでの翻訳テストはDeepLの有料APIキーを
    消費しないよう、DeepL呼び出し自体をモックする
    （``translate_paper.call_deepl`` を差し替える）。

    既にMinerU解析済みの``processed``系フィクスチャのoutput_dir（実際の
    図・数式画像を含む）に対し、Step2〜4（タグ解析・翻訳・PDF生成）を
    実行し、
    - 3種類のPDFが空でなく生成されること
    - paper_ja.pdfに実際に日本語訳が書き込まれていること（原文の
      英語のままではなく、恒等関数でもないこと）
    を確認する。MinerU自体は再実行しない（fixtureの結果を再利用する）ため、
    実行時間は翻訳・PDF生成の分のみで済む。DeepLを呼ばないため、run_idで
    `-k sample0`〜`sample3`のように対象サンプルを自由に選んで実行できる
    （sample3のみCLAUDE.mdの規定により印刷ページラベル55〜60の範囲に限定）。
    このrun_idはcache/配下のフォルダ名（工程(5)(6)の実データテストが使う
    ものと同じ命名規則）と揃えてあるが、本テスト自体はDeepL出力キャッシュ
    （real_deepl_output/）を一切参照しない。
    """
    processed = request.getfixturevalue(processed_fixture_name)
    output_dir = processed["output_dir"]

    # 参考文献セクションのみで構成されるページ（exclude_references_sectionに
    # より全unitが翻訳対象外になる）では、モック訳文字列を含まなくて正常。
    # sample0（2ページ抜粋）では発生しないが、sample1/sample2のような実論文
    # 全体では実際に起こりうるため、translate_and_export実行前に判定して
    # おく。
    pre_units = parse_output_dir(output_dir)
    exclude_references_section(pre_units)
    translatable_pages = {u.page for u in pre_units if u.translatable}

    def _fake_call_deepl(units, api_key, document_context, log=print):
        return {
            unit.tag: RawDeeplResult(raw_text="これはテスト用の日本語訳です。", math_spans=[])
            for unit in units
            if unit.translatable
        }

    monkeypatch.setattr(translate_paper, "call_deepl", _fake_call_deepl)

    pdf_paths = translate_paper.translate_and_export(output_dir)

    assert len(pdf_paths) == 3
    for path in pdf_paths:
        assert path.exists(), f"{path} が生成されていない"
        assert path.stat().st_size > 0, f"{path} が空ファイルになっている"

    ja_pdf_path = next(p for p in pdf_paths if p.name == "paper_ja.pdf")
    with fitz.open(ja_pdf_path) as doc:
        ja_text = "".join(page.get_text() for page in doc)
    has_japanese = any("぀" <= ch <= "ヿ" or "一" <= ch <= "鿿" for ch in ja_text)
    assert has_japanese, "paper_ja.pdfに日本語文字が見つからない（翻訳が行われていない可能性がある）"

    # translate_and_export内でwrite_translated_pagesが呼ばれ、page_XX_en.mdと
    # 対になるpage_XX_ja.mdが生成されているか（ページ番号はサンプルごとに
    # 異なるため、processedが実際に生成したページ別Markdownのファイル名
    # から動的に期待値を組み立てる。sample0は2ページ、sample1は9ページ、
    # sample2は23ページ、sample3は67〜72ページ等、ハードコードしない）。
    expected_ja_names = sorted(md_path.name.replace("_en.md", "_ja.md") for md_path in processed["md_paths"])
    ja_md_paths = sorted(output_dir.glob("page_*_ja.md"))
    assert [p.name for p in ja_md_paths] == expected_ja_names
    for path in ja_md_paths:
        page_num = int(path.stem.split("_")[1])
        if page_num in translatable_pages:
            assert "これはテスト用の日本語訳です。" in path.read_text(encoding="utf-8")


def test_translate_and_export_translates_all_translatable_units(tmp_path, monkeypatch):
    """test_translate_and_export_with_mocked_deeplは「日本語が1文字でも
    あればPASS」という緩い検証のため、翻訳対象文の一部しか訳されていない
    （例: 5文中1文しか訳されていない）不具合を検知できない。この弱点を
    補強するため、翻訳対象の文数が既知（5文: title 1 + heading 1 +
    body_sentence 3）の自作Markdownを用意し、モック訳文字列が出力PDFに
    ちょうどその数だけ出現することを確認する。process_pdf/MinerUを
    一切経由しないため、数秒で完了する。
    """
    output_dir = tmp_path / "g_all_units_input"
    output_dir.mkdir()
    mock_ja = "これはテスト用の日本語訳です。"
    (output_dir / "page_01_en.md").write_text(
        "[P1-TITLE] A Study of Something Interesting\n"
        "[P1-AUTHORS] Jane Doe\n"
        "[P1-HEADING-1.introduction] 1. INTRODUCTION\n"
        "[P1-S1-1.introduction-S1] This is the first sentence.\n"
        "[P1-S2-1.introduction-S2] This is the second sentence.\n"
        "[P1-S3-1.introduction-S3] This is the third sentence.\n",
        encoding="utf-8",
    )

    def _fake_call_deepl(units, api_key, document_context, log=print):
        return {
            unit.tag: RawDeeplResult(raw_text=mock_ja, math_spans=[]) for unit in units if unit.translatable
        }

    monkeypatch.setattr(translate_paper, "call_deepl", _fake_call_deepl)

    pdf_paths = translate_paper.translate_and_export(output_dir)
    ja_pdf_path = next(p for p in pdf_paths if p.name == "paper_ja.pdf")
    with fitz.open(ja_pdf_path) as doc:
        ja_text = "".join(page.get_text() for page in doc)

    translated_count = ja_text.count(mock_ja)
    assert translated_count == 5, (
        f"翻訳対象の5文（title/heading/body_sentence x3）すべてに訳文が"
        f"反映されているはずが、{translated_count}回しか出現しなかった"
        f"（一部の文が未翻訳のまま残っている可能性がある）"
    )


# 未知候補チェックで使う許可リスト。sample0.pdfのpage_01/page_02を実際に
# DeepLで翻訳し、find_untranslated_fragment_candidatesが検出した全候補を
# 人間が目視確認した結果を記録したもの。
#
# ここに無い新しい候補が見つかった場合はテストが失敗する。その候補を
# 実際のpage_XX_ja.md・原文PDFで目視確認した上で、
#   (a) 固有名詞・略語・列挙記号など数式ではないもの
#       → KNOWN_FALSE_POSITIVE_FRAGMENTSへ追加
#   (b) $...$で保護されていない本物の数式
#       → KNOWN_LEAKED_MATH_FRAGMENTSへ追加（可能ならMinerU/analyze_
#         structure側での$...$保護も検討する）
# のどちらかに追加すること。単にリストに追加するだけでテストを通す
# ことが目的化しないよう注意（人間が確認した記録として運用する）。
KNOWN_FALSE_POSITIVE_FRAGMENTS = {
    "NMS",  # 略語（Non-Maximum Suppression）。P1-S16。
    "CFG",  # 略語（Classifier-Free Guidance）。P1-S32。
    "Classifier-Free Guidance (CFG)",  # 略語の展開形。P1-S32。DeepLが
    # 略語をそのまま残す代わりに正式名称＋略語をまとめて残すことがある
    # （言い回しの揺れ）。
    "DiT",  # 固有名詞（モデル名）。P2-FIG1-CAPTION-S3。
    "LoRA",  # 固有名詞（手法名）。P2-FIG1-CAPTION-S3。
    "FLUX",  # 固有名詞（モデル名）。P2-S13。
    "(ODE)",  # 略語（Ordinary Differential Equation）。P2-S7。
    "ODE",  # 同上の表記ゆれ。P2-S7。DeepLが全角括弧「（ODE）」で出力する
    # ことがあり、find_untranslated_fragment_candidatesは半角部分のみを
    # 抽出するため"(ODE)"とは別トークンになる。
    "(i)", "(ii)", "(iii)",  # 列挙記号。P2-S4。
}
KNOWN_LEAKED_MATH_FRAGMENTS: set[str] = set()
# ギリシャ文字（wrap_bare_greek_letters）・単体アルファベット
# （protect_confirmed_single_letter_leaks）・"1文字=値"形式
# （wrap_bare_letter_equals_expressions）はいずれも構造解析・後処理段階で
# 自動的に$...$保護されるようになったため、この許可リストに追加する必要は
# ない（詳細はpdf_text_utils.py/math_protection.pyの該当関数のdocstring
# 参照）。"DiT"/"NMS"のような複数文字のトークンは実在の固有名詞・略語と
# 区別できないため、引き続き検出のみ・許可リストでの容認とする。

# sample1.pdf（表を含む論文フルサイズ、9ページ）向けの許可リスト。
# sample0.pdfの許可リストとは別の論文・別の語彙のため独立して管理する。
# 中身はtest_page_ja_md_and_candidate_detection_against_real_deepl_sample1
# の初回実行結果を人間が目視確認して追加したもの。
KNOWN_FALSE_POSITIVE_FRAGMENTS_SAMPLE1: set[str] = {
    "NMS", "CFG",  # 略語（Non-Maximum Suppression / Classifier-Free Guidance）。
    "DiT", "LoRA", "FLUX",  # 固有名詞（モデル・手法名）。
    "(ODE)", "ODE",  # 略語（Ordinary Differential Equation）とその表記ゆれ。
    "(i)", "(ii)", "(iii)",  # 列挙記号。
    "GED",  # 固有名詞（比較対象手法名）。
    "ODS", "OIS",  # 評価指標の略語（Optimal Dataset/Image Scale）。
    "IoU",  # 評価指標の略語（Intersection over Union）。
    "A.", "A.1.", "A.1", "A.2.", "A.2",  # 付録の章番号・参照（Appendix A）。
    "B.", "B.1.", "B.1", "B.2.", "B.2",  # 同上（Appendix B）。
    "C.", "C.1.", "C.1", "C.2.", "C.2",  # 同上（Appendix C）。
}
KNOWN_LEAKED_MATH_FRAGMENTS_SAMPLE1: set[str] = set()


@pytest.mark.parametrize(
    "known_false_positives,known_leaked,run_id",
    [
        (KNOWN_FALSE_POSITIVE_FRAGMENTS, KNOWN_LEAKED_MATH_FRAGMENTS, "sample0_full"),
        (KNOWN_FALSE_POSITIVE_FRAGMENTS_SAMPLE1, KNOWN_LEAKED_MATH_FRAGMENTS_SAMPLE1, "sample1_full"),
    ],
    ids=["sample0", "sample1"],
)
def test_untranslated_fragment_candidates_against_cached_deepl_output(known_false_positives, known_leaked, run_id):
    """実際にDeepLで翻訳済みのunits_raw.jsonスナップショット（
    test_run_pipeline_end_to_end_with_real_deeplの実行、または人間による
    本実行が残したもの。`_find_latest_real_deepl_cache`で最新版を取得）を
    使い、未保護の数式らしき候補の許可リスト回帰チェックを行う。無課金・
    DeepLを一切呼ばない（意図的にテスト名へ"real_deepl"を含めていない。
    CLAUDE.mdの実行前確認ルールは名前に"real_deepl"を含むテストが対象
    のため、無課金の本テストが誤ってその対象に含まれないようにするため）。

    「実DeepL呼び出し（凍結データの生成）」と「許可リスト突合（コード側
    リグレッションの検知）」は目的が異なる。後者は前者の凍結データさえ
    あれば無課金・毎回実行できるため、本テストとして分離している。凍結
    データの生成元は、全体テストの実DeepL版
    （test_run_pipeline_end_to_end_with_real_deepl）または人間による通常の
    本実行（`translate_paper.py`。実行するたびに自動的にcache/配下へ
    スナップショットが残る）のいずれかに一本化している。

    units_raw.jsonはapply_restore直後・protect_confirmed_single_letter_
    leaks適用前のスナップショットのため、本番のtranslate_and_exportと
    同じ順序でここに適用してから候補チェックする（そうしないと、本来
    自動保護されて候補から除外されるはずの単体アルファベット数式変数が
    未知候補として誤検出されてしまう）。

    DeepLの翻訳結果は完全に決定的ではなく、言い回しの変化により許可
    リストに無い新しい候補（真の数式漏れとは限らず、誤検知の場合もある）
    が出現し、凍結データが更新されるたびに失敗することがある。その場合は
    実際のpage_XX_ja.md・原文PDFで目視確認の上、誤検知ならKNOWN_FALSE_
    POSITIVE_FRAGMENTSへ、本物の数式漏れならKNOWN_LEAKED_MATH_FRAGMENTS
    へ追加すること（“取りあえず許可リストに足してテストを通す”という運用
    は本チェックの目的を損なうため避けること）。
    """
    cache_root = _find_latest_real_deepl_cache(run_id)
    units_raw_path = cache_root / "05_postprocess" / "units_raw.json" if cache_root else None
    if units_raw_path is None or not units_raw_path.exists():
        pytest.skip(
            f"{run_id} の実DeepLキャッシュが無いためスキップ"
            "（test_run_pipeline_end_to_end_with_real_deeplの実行、または"
            "translate_paper.pyでの本実行により生成される）"
        )

    units = [DocUnit(**d) for d in json.loads(units_raw_path.read_text(encoding="utf-8"))]
    protect_confirmed_single_letter_leaks(units, log=lambda _msg: None)

    known = known_false_positives | known_leaked
    unexpected: dict[str, list[str]] = {}
    for unit in units:
        if not unit.translatable:
            continue
        new_tokens = [t for t in find_untranslated_fragment_candidates(unit.ja_text) if t not in known]
        if new_tokens:
            unexpected[unit.tag] = new_tokens
    assert not unexpected, (
        "許可リストに無い未知の候補が見つかりました。実際のpage_XX_ja.md・"
        "原文PDFで目視確認の上、誤検知ならKNOWN_FALSE_POSITIVE_FRAGMENTSへ、"
        "本物の数式漏れならKNOWN_LEAKED_MATH_FRAGMENTSへ追加してください: "
        f"{unexpected}"
    )
