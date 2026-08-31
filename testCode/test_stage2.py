"""工程(2): PDF解析のテスト。

前半（MinerU実行。対応関数: pdf_mineru_runner.run_mineru）は、7工程中
唯一の重い外部処理のため、subprocess.runを差し替えて実MinerUを一切起動
せずに検証する（キャッシュ保存先も汚染しないよう一時ディレクトリへ差し替
える）。加えて同じstage2.py内にまとめられているキャッシュ機構
（load_cached_items/save_cache/get_mineru_version、速度最適化のみが
目的で本番の正しさには関与しない）自体の単体テストも収録する。

後半（構造解析・成果物結合。対応関数: analyze_structure / build_document）
は、content_list → タグ付きMarkdownへの変換をMinerU出力を模した自作
items・自作StructuredDocumentのみを使って検証する（MinerU・DeepLの
どちらも呼ばずに検証できる、実データ確認用の一部テストを除く）。

process_pdf（工程(2)全体を代表する入口関数。上記2つの呼び出しを
順に実行するだけの薄いオーケストレーター）の実データ確認もここに含む。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import fitz  # PyMuPDF
import pytest
from PIL import Image

from mainCode.stage1.stage1 import resolve_physical_page_range
from mainCode.stage2 import stage2 as pdf_mineru_runner
from mainCode.stage2.stage2 import (
    HEADING_RE,
    CaptionElement,
    EquationElement,
    FigureElement,
    HeadingElement,
    LabeledElement,
    PageContent,
    StructuredDocument,
    TextBlockElement,
    UnknownElement,
    analyze_structure,
    build_document,
    is_known_word,
    parse_caption_label,
    process_pdf,
    restore_merged_hyphens,
    slugify_section_name,
    split_merged_compound,
    split_sentences,
    wrap_bare_greek_letters,
    wrap_bare_letter_equals_expressions,
)
from mainCode.stage3.stage3 import parse_page_file

from conftest import SAMPLE3_PDF_PATH, _REAL_SAMPLE_SCENARIOS


def _make_blank_pdf(path: Path, page_count: int = 1) -> None:
    doc = fitz.open()
    for _ in range(page_count):
        doc.new_page()
    doc.save(str(path))
    doc.close()



_FAKE_MINERU_ITEMS = [{"type": "text", "text": "hello", "text_level": None, "page_idx": 0}]


@pytest.fixture()
def isolated_mineru_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_mineru_runner, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(pdf_mineru_runner, "get_mineru_version", lambda: "test-version-1")


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





class TestMinerURunner:
    def test_run_mineru_invokes_subprocess_and_parses_content_list(self, tmp_path, isolated_mineru_cache, monkeypatch):
        pdf_path = tmp_path / "sample.pdf"
        _make_blank_pdf(pdf_path)
        call_counter = [0]
        _install_fake_mineru_subprocess(monkeypatch, call_counter)

        result = pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work")

        assert call_counter[0] == 1
        assert result.items == _FAKE_MINERU_ITEMS
        assert (result.images_base / "images" / "fig1.jpg").exists()


    def test_run_mineru_rejects_unsupported_backend(self, tmp_path, isolated_mineru_cache):
        with pytest.raises(ValueError):
            pdf_mineru_runner.run_mineru(tmp_path / "sample.pdf", tmp_path / "work", backend="not-a-real-backend")


    def test_run_mineru_pipeline_backend_passes_method_auto(self, tmp_path, isolated_mineru_cache, monkeypatch):
        """既定のpipelineバックエンドでは、--method autoが付与される。"""
        seen_commands = []

        def _fake_run(command, check, capture_output):
            seen_commands.append(command)
            pdf_path = Path(command[command.index("--path") + 1])
            work_dir = Path(command[command.index("--output") + 1])
            stem = pdf_path.stem
            auto_dir = work_dir / stem / "auto"
            auto_dir.mkdir(parents=True)
            (auto_dir / f"{stem}_content_list.json").write_text(json.dumps(_FAKE_MINERU_ITEMS), encoding="utf-8")
            return MagicMock(returncode=0)

        monkeypatch.setattr(pdf_mineru_runner.subprocess, "run", _fake_run)
        pdf_path = tmp_path / "sample.pdf"
        _make_blank_pdf(pdf_path)

        pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work")

        assert "--method" in seen_commands[0] and "auto" in seen_commands[0]
        assert seen_commands[0][seen_commands[0].index("--backend") + 1] == "pipeline"


    def test_run_mineru_vlm_engine_backend_omits_method_and_finds_backend_specific_subfolder(self,
        tmp_path, isolated_mineru_cache, monkeypatch
    ):
        """vlm-engineバックエンドでは--methodを付与しない（MinerU CLI仕様上
    pipeline/hybrid-*専用のオプションのため）。また、content_list.jsonの
    出力サブフォルダ名がバックエンドにより異なる（実機確認では"vlm"）ため、
    決め打ちの"auto"ではなくglob検索で見つけられることを確認する。"""
        seen_commands = []

        def _fake_run(command, check, capture_output):
            seen_commands.append(command)
            pdf_path = Path(command[command.index("--path") + 1])
            work_dir = Path(command[command.index("--output") + 1])
            stem = pdf_path.stem
            vlm_dir = work_dir / stem / "vlm"
            vlm_dir.mkdir(parents=True)
            (vlm_dir / f"{stem}_content_list.json").write_text(json.dumps(_FAKE_MINERU_ITEMS), encoding="utf-8")
            return MagicMock(returncode=0)

        monkeypatch.setattr(pdf_mineru_runner.subprocess, "run", _fake_run)
        pdf_path = tmp_path / "sample.pdf"
        _make_blank_pdf(pdf_path)

        result = pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work", backend="vlm-engine")

        assert "--method" not in seen_commands[0]
        assert seen_commands[0][seen_commands[0].index("--backend") + 1] == "vlm-engine"
        assert result.items == _FAKE_MINERU_ITEMS


    def test_run_mineru_uses_cache_on_second_call(self, tmp_path, isolated_mineru_cache, monkeypatch):
        pdf_path = tmp_path / "sample.pdf"
        _make_blank_pdf(pdf_path)
        call_counter = [0]
        _install_fake_mineru_subprocess(monkeypatch, call_counter)

        pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work1")
        result_2 = pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work2")

        assert call_counter[0] == 1  # 2回目はキャッシュヒットでsubprocessが呼ばれない
        assert result_2.items == _FAKE_MINERU_ITEMS


    def test_run_mineru_cache_invalidated_by_different_page_range(self, tmp_path, isolated_mineru_cache, monkeypatch):
        pdf_path = tmp_path / "sample.pdf"
        _make_blank_pdf(pdf_path, page_count=3)
        call_counter = [0]
        _install_fake_mineru_subprocess(monkeypatch, call_counter)

        pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work1", 0, 1)
        pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work2", 1, 2)

        assert call_counter[0] == 2  # ページ範囲が異なるためキャッシュヒットしない


    def test_run_mineru_raises_when_subprocess_fails(self, tmp_path, isolated_mineru_cache, monkeypatch):
        pdf_path = tmp_path / "sample.pdf"
        _make_blank_pdf(pdf_path)
        call_counter = [0]
        _install_fake_mineru_subprocess(monkeypatch, call_counter, succeed=False)

        with pytest.raises(pdf_mineru_runner.MinerURunError):
            pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work")


    def test_run_mineru_raises_when_output_file_missing(self, tmp_path, isolated_mineru_cache, monkeypatch):
        pdf_path = tmp_path / "sample.pdf"
        _make_blank_pdf(pdf_path)

        monkeypatch.setattr(
            pdf_mineru_runner.subprocess, "run", lambda command, check, capture_output: MagicMock(returncode=0)
        )

        with pytest.raises(pdf_mineru_runner.MinerURunError):
            pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work")


# --- pdf_mineru_runner.py内のキャッシュ機構 直接の単体テスト ---------------------



def _make_images_base(tmp_path: Path, name: str = "images_base") -> Path:
    images_base = tmp_path / name
    (images_base / "images").mkdir(parents=True)
    (images_base / "images" / "fig1.jpg").write_bytes(b"fake-image-bytes")
    return images_base





class TestMinerUCache:
    def test_load_returns_none_when_no_cache(self, tmp_path, isolated_mineru_cache):
        pdf_path = tmp_path / "sample.pdf"
        _make_blank_pdf(pdf_path)
        assert pdf_mineru_runner.load_cached_items(pdf_path, None, None) is None


    def test_save_then_load_roundtrip(self, tmp_path, isolated_mineru_cache):
        pdf_path = tmp_path / "sample.pdf"
        _make_blank_pdf(pdf_path)
        images_base = _make_images_base(tmp_path)

        pdf_mineru_runner.save_cache(pdf_path, None, None, _FAKE_MINERU_ITEMS, images_base)
        cached = pdf_mineru_runner.load_cached_items(pdf_path, None, None)

        assert cached is not None
        items, images_dir = cached
        assert items == _FAKE_MINERU_ITEMS
        assert (images_dir / "images" / "fig1.jpg").read_bytes() == b"fake-image-bytes"


    def test_cache_isolated_by_page_range(self, tmp_path, isolated_mineru_cache):
        pdf_path = tmp_path / "sample.pdf"
        _make_blank_pdf(pdf_path, page_count=3)
        images_base = _make_images_base(tmp_path)

        pdf_mineru_runner.save_cache(pdf_path, 0, 1, _FAKE_MINERU_ITEMS, images_base)

        assert pdf_mineru_runner.load_cached_items(pdf_path, 0, 1) is not None
        assert pdf_mineru_runner.load_cached_items(pdf_path, 1, 2) is None
        assert pdf_mineru_runner.load_cached_items(pdf_path, None, None) is None


    def test_cache_isolated_by_backend(self, tmp_path, isolated_mineru_cache):
        """backendが異なるキャッシュは互いに影響しない（別フォルダに保存される）。
    pipeline/vlm-engineは認識精度・content_list形式が異なるため、混在して
    誤ったキャッシュヒットをしてはならない。"""
        pdf_path = tmp_path / "sample.pdf"
        _make_blank_pdf(pdf_path, page_count=1)
        images_base = _make_images_base(tmp_path)

        pdf_mineru_runner.save_cache(pdf_path, None, None, _FAKE_MINERU_ITEMS, images_base, backend="pipeline")

        assert pdf_mineru_runner.load_cached_items(pdf_path, None, None, backend="pipeline") is not None
        assert pdf_mineru_runner.load_cached_items(pdf_path, None, None, backend="vlm-engine") is None


    def test_cache_dir_name_differs_by_backend(self, tmp_path, isolated_mineru_cache):
        """既定のpipelineは"mineru_cache"、それ以外のbackendは
    "mineru_cache_<backend>"というフォルダ名になる。"""
        pdf_path = tmp_path / "sample.pdf"
        _make_blank_pdf(pdf_path, page_count=1)
        images_base = _make_images_base(tmp_path)

        pdf_mineru_runner.save_cache(pdf_path, None, None, _FAKE_MINERU_ITEMS, images_base, backend="pipeline")
        pdf_mineru_runner.save_cache(pdf_path, None, None, _FAKE_MINERU_ITEMS, images_base, backend="vlm-engine")

        run_dir = pdf_mineru_runner._CACHE_ROOT / "sample"
        assert (run_dir / "mineru_cache").exists()
        assert (run_dir / "mineru_cache_vlm-engine").exists()


    def test_load_returns_none_when_pdf_content_changes(self, tmp_path, isolated_mineru_cache):
        pdf_path = tmp_path / "sample.pdf"
        _make_blank_pdf(pdf_path, page_count=1)
        images_base = _make_images_base(tmp_path)
        pdf_mineru_runner.save_cache(pdf_path, None, None, _FAKE_MINERU_ITEMS, images_base)
        assert pdf_mineru_runner.load_cached_items(pdf_path, None, None) is not None

        # 同じパスに別内容のPDFを書き直す（著者の改訂・別ファイルへの差し替え等を想定）。
        _make_blank_pdf(pdf_path, page_count=5)

        assert pdf_mineru_runner.load_cached_items(pdf_path, None, None) is None


    def test_load_returns_none_when_mineru_version_changes(self, tmp_path, isolated_mineru_cache, monkeypatch):
        pdf_path = tmp_path / "sample.pdf"
        _make_blank_pdf(pdf_path)
        images_base = _make_images_base(tmp_path)

        monkeypatch.setattr(pdf_mineru_runner, "get_mineru_version", lambda: "1.0.0")
        pdf_mineru_runner.save_cache(pdf_path, None, None, _FAKE_MINERU_ITEMS, images_base)
        assert pdf_mineru_runner.load_cached_items(pdf_path, None, None) is not None

        monkeypatch.setattr(pdf_mineru_runner, "get_mineru_version", lambda: "2.0.0")
        assert pdf_mineru_runner.load_cached_items(pdf_path, None, None) is None


    def test_load_returns_none_on_corrupted_content_list_json(self, tmp_path, isolated_mineru_cache):
        pdf_path = tmp_path / "sample.pdf"
        _make_blank_pdf(pdf_path)
        images_base = _make_images_base(tmp_path)
        pdf_mineru_runner.save_cache(pdf_path, None, None, _FAKE_MINERU_ITEMS, images_base)

        cache_dir = pdf_mineru_runner._cache_dir(pdf_path, None, None)
        (cache_dir / "content_list.json").write_text("{not valid json", encoding="utf-8")

        assert pdf_mineru_runner.load_cached_items(pdf_path, None, None) is None


    def test_save_is_noop_when_pdf_hash_fails(self, tmp_path, isolated_mineru_cache):
        pdf_path = tmp_path / "does_not_exist.pdf"
        images_base = _make_images_base(tmp_path)

        # ファイルが存在しない場合、_compute_pdf_hashはOSErrorを送出するため、
        # save_cacheは例外を伝播させず静かに何もしない。
        pdf_mineru_runner.save_cache(pdf_path, None, None, _FAKE_MINERU_ITEMS, images_base)

        assert not (tmp_path / "cache").exists()


    def test_cache_disabled_via_env_var(self, tmp_path, isolated_mineru_cache, monkeypatch):
        pdf_path = tmp_path / "sample.pdf"
        _make_blank_pdf(pdf_path)
        images_base = _make_images_base(tmp_path)

        pdf_mineru_runner.save_cache(pdf_path, None, None, _FAKE_MINERU_ITEMS, images_base)
        assert pdf_mineru_runner.load_cached_items(pdf_path, None, None) is not None

        monkeypatch.setenv("MINERU_CACHE_DISABLE", "1")
        # 既にキャッシュが存在していても、無効化フラグが優先される。
        assert pdf_mineru_runner.load_cached_items(pdf_path, None, None) is None

        pdf_mineru_runner.save_cache(pdf_path, 0, 0, _FAKE_MINERU_ITEMS, images_base)
        assert pdf_mineru_runner.load_cached_items(pdf_path, 0, 0) is None
        monkeypatch.delenv("MINERU_CACHE_DISABLE")
        assert pdf_mineru_runner.load_cached_items(pdf_path, 0, 0) is None  # 無効化中は保存自体されていない


    def test_get_mineru_version_raises_on_missing_package(self, monkeypatch):
        import importlib.metadata

        from mainCode.stage2 import stage2 as pdf_mineru_runner

        def _raise(_name):
            raise importlib.metadata.PackageNotFoundError()

        monkeypatch.setattr(importlib.metadata, "version", _raise)
        with pytest.raises(pdf_mineru_runner.MinerUVersionError):
            pdf_mineru_runner.get_mineru_version()


    def test_run_mineru_reruns_after_version_change(self, tmp_path, isolated_mineru_cache, monkeypatch):
        pdf_path = tmp_path / "sample.pdf"
        _make_blank_pdf(pdf_path)
        call_counter = [0]
        _install_fake_mineru_subprocess(monkeypatch, call_counter)

        monkeypatch.setattr(pdf_mineru_runner, "get_mineru_version", lambda: "1.0.0")
        pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work1")
        assert call_counter[0] == 1

        monkeypatch.setattr(pdf_mineru_runner, "get_mineru_version", lambda: "2.0.0")
        (tmp_path / "work2").mkdir()
        pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work2")
        assert call_counter[0] == 2


    @pytest.mark.parametrize("pdf_path,start_page,end_page,run_id,range_label", _REAL_SAMPLE_SCENARIOS)
    def test_mineru_cache_has_real_content_for_sample_pdfs(self, pdf_path, start_page, end_page, run_id, range_label):
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
        cached = pdf_mineru_runner.load_cached_items(pdf_path, start_0idx, end_0idx, range_label=range_label)
        if cached is None:
            pytest.skip(
                f"{run_id} のMinerUキャッシュが無いためスキップ"
                "（一度process_pdfを実行すると生成される）"
            )

        items, images_base = cached
        assert items, f"{run_id} のcontent_listが空"
        assert images_base.exists()


# ============================================================================
# 工程(2)後半: 構造解析・成果物結合
#   対応関数: analyze_structure / build_document
#     （content_list → タグ付きMarkdownへの変換）。MinerU出力を模した
#   自作items・自作Markdown・自作StructuredDocumentのみを使い、MinerU・
#   DeepLのどちらも呼ばずに検証する（実データ確認用の一部テストを除く）。
# ============================================================================

SENTENCE_ID_RE = re.compile(r"^\[P(\d+)-S(\d+)-([A-Za-z0-9.]+)-S(\d+)\] (.+)$")
FIGURE_ID_RE = re.compile(r"^!\[P(\d+)-FIG(\d+)\]\(images/(fig_p\d+_\d+\.png)\) \[P(\d+)-FIG(\d+)\]$")
HEADING_ID_RE = re.compile(r"^\[P(\d+)-HEADING-([A-Za-z0-9.]+)\] (.+)$")
CAPTION_ID_RE = re.compile(r"^\[P(\d+)-FIG(\d+)-CAPTION-S(\d+)\] (.+)$")
EQUATION_ID_RE = re.compile(r"^!\[P(\d+)-EQ(\d+)\]\(images/(eq_p\d+_\d+\.png)\) \[P(\d+)-EQ(\d+)\]$")
TABLE_FIG_ID_RE = re.compile(r"^!\[P(\d+)-TABLE(\d+)\]\(images/(table_p\d+_\d+\.png)\) \[P(\d+)-TABLE(\d+)\]$")
TABLE_CAPTION_ID_RE = re.compile(r"^\[P(\d+)-TABLE(\d+)-CAPTION-S(\d+)\] (.+)$")


def _make_source_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), color=(200, 50, 50)).save(path)



class TestAnalyzeStructure:
    def test_analyze_structure_assigns_front_matter_and_numbered_heading_labels(self):
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


    def test_analyze_structure_assigns_synthetic_ids_to_unnumbered_headings(self):
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


    def test_analyze_structure_auto_protects_bare_greek_and_letter_equals_expressions(self):
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


    def test_analyze_structure_separates_figure_and_caption_with_number(self):
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


    def test_analyze_structure_drops_header_footer_page_number_as_noise(self):
        """MinerUの"header"・"footer"・"page_number"種別（実行見出し・著作権
    フッター・ノンブルの繰り返し）が、_NOISE_TYPESにより本文へ紛れ込まず
    に除外されるか（sample2.pdf・sample3.pdfの実データで確認された、
    紙面要素が段落として出力されてしまう問題の回帰テスト）。"""
        items = [
            {"type": "text", "text": "Introduction", "text_level": 2, "page_idx": 0},
            {"type": "header", "text": "Running Title Repeated Every Page", "page_idx": 0},
            {"type": "footer", "text": "© The Author(s) 2026", "page_idx": 0},
            {"type": "page_number", "text": "55", "page_idx": 0},
        ]
        doc = analyze_structure(items, images_base=Path("."), page_offset=16)
        page = doc.pages[0]

        assert not any(isinstance(e, UnknownElement) for e in page.elements)
        assert not any(
            isinstance(e, TextBlockElement) and "Running Title" in " ".join(e.sentences) for e in page.elements
        )
        assert not any(isinstance(e, TextBlockElement) and "55" in e.sentences for e in page.elements)


    def test_analyze_structure_treats_page_footnote_as_translatable_text(self):
        """MinerUの"page_footnote"種別が、紙面ノイズとして捨てられず本文と
    同じ扱い（文分割・翻訳対象）になるか。実データ（sample3.pdf）では
    紙面上は脚注欄にあるが、本物の学術脚注（引用・補足説明）を含む
    ことがあり、単純にノイズ扱いすると内容が失われるため。"""
        items = [
            {"type": "text", "text": "Introduction", "text_level": 2, "page_idx": 0},
            {
                "type": "page_footnote",
                "text": "See Chapter “World Models” for further discussion.",
                "page_idx": 0,
            },
        ]
        doc = analyze_structure(items, images_base=Path("."), page_offset=16)
        page = doc.pages[0]

        assert not any(isinstance(e, UnknownElement) for e in page.elements)
        text_blocks = [e for e in page.elements if isinstance(e, TextBlockElement)]
        assert any("World Models" in " ".join(b.sentences) for b in text_blocks)


    def test_analyze_structure_falls_back_to_unknown_element_on_error(self):
        """要素の解析中に例外が発生しても（ここではimg_path欠如によるKeyError）
    ドキュメント全体を止めず、UnknownElementへフォールバックするか。"""
        items = [{"type": "image", "page_idx": 0}]
        doc = analyze_structure(items, images_base=Path("."))
        page = doc.pages[0]
        assert len(page.elements) == 1
        assert isinstance(page.elements[0], UnknownElement)
        assert page.elements[0].raw_type == "image"


    def test_analyze_structure_equation_without_img_path_keeps_latex(self):
        """vlm-engineバックエンドの数式要素はimg_path（切り出し画像）を持たない。
    img_pathが無くてもEquationElementとしてlatexを保持すべきで、
    image_pathのみNoneになる。"""
        items = [
            {"type": "equation", "text": "$$\nP(A, B) = P(A)P(B)\n$$", "text_format": "latex", "page_idx": 0},
        ]
        doc = analyze_structure(items, images_base=Path("."))
        page = doc.pages[0]

        equations = [e for e in page.elements if isinstance(e, EquationElement)]
        assert len(equations) == 1
        assert equations[0].image_path is None
        assert equations[0].latex == "P(A, B) = P(A)P(B)"


    def test_analyze_structure_equation_with_empty_text_and_no_img_path_is_dropped(self):
        """latexもimg_pathも無い（描画できる情報が皆無の）数式要素は、
    捨ててよい退化ケース。"""
        items = [{"type": "equation", "text": "", "page_idx": 0}]
        doc = analyze_structure(items, images_base=Path("."))
        assert doc.pages[0].elements == []


    def test_analyze_structure_handles_list_items_as_individual_sentences(self):
        """MinerUの"list"種別（list_items配列。参考文献リスト等）が、
    独自の文分割（split_sentences）を経ずに、各項目をそのまま1つの文として
    扱われることを確認する（参考文献のように文中にピリオドを含む短い項目が
    多く、文分割ロジックに通すとかえって誤分割を起こしやすいため）。
    この種別（_handle_list_item）は、これまで一度もテストされていなかった。
    """
        items = [
            {"type": "text", "text": "Introduction", "text_level": 2, "page_idx": 0},
            {
                "type": "list",
                "list_items": ["Smith, J. et al. 2020. Some paper.", "Doe, J. 2021. Another paper."],
                "page_idx": 0,
            },
        ]
        doc = analyze_structure(items, images_base=Path("."))
        page = doc.pages[0]

        text_blocks = [e for e in page.elements if isinstance(e, TextBlockElement)]
        assert len(text_blocks) == 1
        assert text_blocks[0].sentences == [
            "Smith, J. et al. 2020. Some paper.",
            "Doe, J. 2021. Another paper.",
        ]
        assert text_blocks[0].sentence_ids == [
            "P1-S1-u1.introduction-S1",
            "P1-S2-u1.introduction-S2",
        ]


    def test_analyze_structure_backfills_caption_to_previous_unlabeled_figure_on_split_caption_block(self):
        """隣接する図表のキャプションが、レイアウト検出の都合で後続の画像
    ブロックへまとめて誤って結合されることがある（例: Fig.8とFig.9の
    キャプションが両方Fig.9側の画像ブロックに付与される）。この場合、
    直前に追加した未ラベルの画像へ遡ってキャプションを割り当て直し
    （バックフィル）、最後のキャプションを今回の画像に割り当てることを
    確認する。この複数キャプション結合ロジック（_handle_image_or_table_item
    の分岐）は、これまで一度もテストされていなかった。"""
        items = [
            {"type": "text", "text": "Introduction", "text_level": 2, "page_idx": 0},
            {"type": "image", "img_path": "images/fig8.jpg", "image_caption": [], "page_idx": 0},
            {
                "type": "image",
                "img_path": "images/fig9.jpg",
                "image_caption": ["Fig. 8: First figure text.", "Fig. 9: Second figure text."],
                "page_idx": 0,
            },
        ]
        doc = analyze_structure(items, images_base=Path("base"))
        page = doc.pages[0]

        figures = [e for e in page.elements if isinstance(e, FigureElement)]
        captions = [e for e in page.elements if isinstance(e, CaptionElement)]
        assert [(f.number, f.labeled) for f in figures] == [(8, True), (9, True)]
        assert captions[0].sentences == ["Fig. 8: First figure text."]
        assert captions[1].sentences == ["Fig. 9: Second figure text."]

        # バックフィルされた1件目のキャプションは、直前の画像要素の直後に
        # 挿入される（Figure(8)の直後にCaption(8)が続く）。
        fig_and_caption_order = [
            type(e).__name__ for e in page.elements if isinstance(e, (FigureElement, CaptionElement))
        ]
        assert fig_and_caption_order == ["FigureElement", "CaptionElement", "FigureElement", "CaptionElement"]


    def test_handle_image_or_table_item_backfill_does_not_touch_already_labeled_figure(self):
        """複数キャプション結合（バックフィル）の遡り対象は、直前に隣接する
    「未ラベルの」FigureElementに限定されるべきで、既にラベル付け済みの
    FigureElementまで遡って上書きしてはならないことを確認する。

    `if isinstance(el, FigureElement) and not el.labeled:`の
    `not el.labeled`部分を取り除いても、通常の`_build_pages`経由の
    テスト（既存のtest_analyze_structure_backfills_...）では検知できない
    ことを確認した（ラベル付き画像の直後には必ずCaptionElementが続くため、
    reversed(elements)の直前要素はCaptionElementになり、
    isinstance(el, FigureElement)自体がどのみちFalseになってしまい、
    ガードの有無が観測できない）。そのため、_handle_image_or_table_item
    自体を直接呼び、builder.elementsへ意図的に「キャプションを挟まず
    ラベル付き済みFigureElementだけを直前に置いた」状況を作って検証する
    （キャプション文がsplit_sentencesで空になった場合等、実データでも
    理論上到達しうる状態）。"""
        already_labeled = FigureElement(image_path=Path("images/fig7.jpg"), fig_kind="figure", number=7, labeled=True)
        builder = pdf_mineru_runner._PageBuilder(page_idx=0)
        builder.elements = [already_labeled]

        item = {
            "img_path": "images/fig9.jpg",
            "image_caption": ["Fig. 8: Second figure.", "Fig. 9: Third figure."],
        }
        pdf_mineru_runner._handle_image_or_table_item(item, "image", Path("base"), builder)

        # 既にラベル付け済みのFigure(7)は番号・ラベル状態とも一切変更されない
        assert already_labeled.number == 7
        assert already_labeled.labeled is True

        figures = [e for e in builder.elements if isinstance(e, FigureElement)]
        assert [f.number for f in figures] == [7, 8]


    def test_analyze_structure_assigns_unlabeled_fallback_number_when_no_caption_found(self):
        """キャプションが1つも検出できない画像・表要素は、Fig./Tableの実番号
    ではなく、ページ内で共有される未ラベル連番（unlabeled_seq）でフォール
    バックされ、labeled=Falseとしてマークされることを確認する。この
    unlabeled_seqカウンタは画像・表・数式の間で共有される（実際に画像と
    表を1つずつ用意し、番号が1,2と連続することで確認する）。この
    フォールバック分岐は、これまで一度もテストされていなかった。"""
        items = [
            {"type": "image", "img_path": "images/a.jpg", "page_idx": 0},
            {"type": "table", "img_path": "images/b.jpg", "page_idx": 0},
        ]
        doc = analyze_structure(items, images_base=Path("base"))
        figures = [e for e in doc.pages[0].elements if isinstance(e, FigureElement)]
        assert [(f.fig_kind, f.number, f.labeled) for f in figures] == [
            ("figure", 1, False),
            ("table", 2, False),
        ]




class TestBuildDocument:
    def test_build_document_writes_markdown_and_saves_figure_image(self, tmp_path):
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

        md_paths = build_document(structured_doc, output_dir=output_dir, first_page=1, last_page=1)

        assert [p.name for p in md_paths] == ["page_01_en.md"]
        text = md_paths[0].read_text(encoding="utf-8")
        assert "[P1-TITLE] A Title" in text
        assert "[P1-HEADING-1.introduction] 1. INTRODUCTION" in text
        assert "[P1-S1-1.introduction-S1] Body sentence." in text
        assert "![P1-FIG1](images/fig_p1_1.png) [P1-FIG1]" in text
        assert "[P1-FIG1-CAPTION-S1] Fig. 1: Example." in text
        assert (output_dir / "images" / "fig_p1_1.png").exists()


    def test_build_document_preserves_multiline_text_and_reparses_without_loss(self, tmp_path):
        """UnknownElement.text等に改行を含む生テキスト（MinerUのcode_body等
    由来）が渡された場合、書き出すMarkdown上ではそのまま複数の物理行に
    分かれて人間が読みやすい状態を保ちつつ、md_tag_parser.parse_page_file
    で読み直した際には`[TAG]`で始まらない続きの行が直前unitへ再結合され、
    本文が欠落しないことを確認する（sample1.pdf Appendix A.1のプロンプト
    例文で、code_body由来の複数行テキストの2行目以降が読み飛ばされて
    欠落していた回帰）。"""
        page = PageContent(
            page_number=8,
            elements=[
                UnknownElement(
                    raw_type="code",
                    text="Orthographic top-down residential floor plan,   \n"
                    "walls-only skeleton map.\n"
                    "CAD-like vector look.",
                ),
            ],
        )
        structured_doc = StructuredDocument(pages=[page])
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        md_paths = build_document(structured_doc, output_dir=output_dir, first_page=8, last_page=8)

        # 書き出したMarkdown自体は、可読性のため複数の物理行に分かれたまま。
        text = md_paths[0].read_text(encoding="utf-8")
        lines = text.splitlines()
        assert lines[0] == "[P8-UNKNOWN-code-1] Orthographic top-down residential floor plan,   "
        assert lines[1] == "walls-only skeleton map."
        assert lines[2] == "CAD-like vector look."

        # 実際に翻訳ステップが読み直す経路では、2〜3行目が直前unitへ再結合され、
        # 1つのunitに欠落なく収まっているか。
        units = parse_page_file(md_paths[0])
        assert len(units) == 1
        assert units[0].en_text == (
            "Orthographic top-down residential floor plan, walls-only skeleton map. CAD-like vector look."
        )
        assert units[0].ja_text == units[0].en_text  # 非翻訳対象unitはja_textもen_textと同一


    def test_build_document_numbers_duplicate_unknown_elements_on_same_page(self, tmp_path):
        """同一ページ内に同じraw_typeのUnknownElementが複数出現した場合、
    タグが完全に重複せずページ内連番で区別されるかを確認する
    （sample1.pdf Appendix A.1に実際に存在する、"code"種別のプロンプト
    例文2件が両方とも[P8-UNKNOWN-code]という同一タグになっていた回帰）。
    タグの一意性自体はページ番号で既に担保されるため、連番はページ単位
    （セクション単位ではなく）で振る。"""
        page = PageContent(
            page_number=8,
            elements=[
                UnknownElement(raw_type="code", text="First prompt."),
                UnknownElement(raw_type="code", text="Second prompt."),
                UnknownElement(raw_type="header", text="Running Title"),
            ],
        )
        structured_doc = StructuredDocument(pages=[page])
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        md_paths = build_document(structured_doc, output_dir=output_dir, first_page=8, last_page=8)
        text = md_paths[0].read_text(encoding="utf-8")

        assert "[P8-UNKNOWN-code-1] First prompt." in text
        assert "[P8-UNKNOWN-code-2] Second prompt." in text
        assert "[P8-UNKNOWN-header-1] Running Title" in text  # raw_typeが違えば別カウンタ


    def test_build_document_equation_without_image_omits_image_line_but_keeps_latex(self, tmp_path):
        """image_pathがNone（vlm-engineバックエンド等、切り出し画像を持たない
    数式）の場合、画像行(![...])は出力せず、LATEX行だけを出力する。
    最終PDFの数式描画はLATEX行（KaTeX）側だけで完結するため、これで
    表示上の欠落は起きない（pdf_renderer.py参照）。"""
        page = PageContent(
            page_number=1,
            elements=[EquationElement(latex="x = 1", image_path=None, number=1)],
        )
        structured_doc = StructuredDocument(pages=[page])
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        md_paths = build_document(structured_doc, output_dir=output_dir, first_page=1, last_page=1)

        text = md_paths[0].read_text(encoding="utf-8")
        assert "![P1-EQ1]" not in text
        assert "[P1-EQ1-LATEX] $$x = 1$$" in text
        assert not (output_dir / "images").exists() or not list((output_dir / "images").glob("*"))


class TestProcessPdfValidation:
    def test_process_pdf_rejects_page_range_beyond_total_pages(self, tmp_path):
        """指定した開始・終了ページがPDFの実際の総ページ数を超える場合、
    MinerUを一切起動する前にValueErrorになることを確認する（process_pdf
    のこの入力検証は、これまで一度もテストされていなかった）。"""
        pdf_path = tmp_path / "sample.pdf"
        _make_blank_pdf(pdf_path, page_count=2)
        with pytest.raises(ValueError):
            process_pdf(pdf_path, tmp_path / "output", start_page=1, end_page=5)



class TestSentenceAndTextUtils:
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
            # 人名等の単体アルファベットのイニシャル（"J."）の直後でも誤分割
            # しないことを確認する（_ABBREVIATIONSの固定リストには無い
            # パターンのため、_INITIAL_REという別の判定に依存している。
            # mutation testingで_INITIAL_REの判定を丸ごと取り除いても
            # test_stage2.py・全体スイートともに1件も失敗しないことを
            # 確認した上で追加した回帰テスト）。
            (
                "This work was done by J. Smith and colleagues.",
                ["This work was done by J. Smith and colleagues."],
            ),
            ("", []),
        ],
    )
    def test_split_sentences_handles_abbreviations(self, text, expected):
        """略語（Fig., Sec., e.g. など）・単体イニシャル（J.）の直後で
        誤分割しないことを確認する単体テスト。"""
        assert split_sentences(text) == expected


    @pytest.mark.parametrize(
        "text,expected",
        [
            ("1. INTRODUCTION", ("1", "introduction")),
            ("2.1. Preliminaries", ("2.1", "preliminaries")),
            ("ABSTRACT", None),  # 章番号が無いので見出しとして扱わない
        ],
    )
    def test_heading_regex_and_slugify(self, text, expected):
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
    def test_parse_caption_label(self, text, expected):
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
    def test_wrap_bare_greek_letters(self, text, expected):
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
    def test_wrap_bare_letter_equals_expressions(self, text, expected):
        """未保護の"1文字の変数 = 値"形式が$...$で自動的に囲まれるかの単体テスト。"""
        assert wrap_bare_letter_equals_expressions(text) == expected


    @pytest.mark.parametrize(
        "word,expected",
        [
            ("dataefficient", "data-efficient"),
            ("welllocalized", "well-localized"),
            ("efficient", None),  # 既知の単語はそのまま（分割しない）
            ("xyzxyzxyz", None),  # 分割点の両側とも既知語にならない未知語はそのまま
        ],
    )
    def test_split_merged_compound(self, word, expected):
        """辞書に無い単語のうち、両側とも辞書に載っている分割点が見つかった
    場合のみハイフンを復元するかの単体テスト（これまで一度も直接
    テストされていなかった）。"""
        assert split_merged_compound(word) == expected


    @pytest.mark.parametrize(
        "text,expected",
        [
            ("We use a dataefficient method.", "We use a data-efficient method."),
            ("The welllocalized region is highlighted.", "The well-localized region is highlighted."),
            ("This is a normal sentence with no merged words.", "This is a normal sentence with no merged words."),
            # $...$で囲まれた数式スパン内は対象外（LaTeXコマンドの
            # \mathcal等が誤って複合語と判定されるのを防ぐ）。
            ("already protected $mathcalxx$ term.", "already protected $mathcalxx$ term."),
        ],
    )
    def test_restore_merged_hyphens(self, text, expected):
        """MinerUが改行時に除去したハイフンが復元されるかの単体テスト
    （これまで一度も直接テストされていなかった。analyze_structure経由の
    間接的な確認のみだった）。"""
        assert restore_merged_hyphens(text) == expected


    def test_is_known_word_single_letter_quirk(self):
        """docstringに明記された既知の制約（pyspellcheckerはアルファベット
    1文字を編集距離1以内の候補が必ず見つかるため常に"既知"と判定する）を
    実際に動かして裏取りする（これまで一度もテストされていなかった）。
    is_known_wordを1文字の実在単語判定に使ってはならないという注意書きが、
    実際に正しいことを確認する。"""
        assert is_known_word("z") is True  # "z"自体は英単語ではないが既知扱いになる
        assert is_known_word("efficient") is True
        assert is_known_word("xyzxyzxyz") is False


# --- 工程(2)後半の実データ確認（process_pdf経由でMinerU+analyze_structureを --
# 実際に実行し、複雑な実データに対する挙動を確認する。sample0.pdfのMinerU
# 解析結果はcache/配下に永続化されており、2回目以降は数秒で完了する） ---




class TestAnalyzeStructureRealDataSmoke:
    @pytest.mark.parametrize("pdf_path,start_page,end_page,run_id,range_label", _REAL_SAMPLE_SCENARIOS)
    def test_analyze_structure_on_real_cached_mineru_content(self, pdf_path, start_page, end_page, run_id, range_label):
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
        cached = pdf_mineru_runner.load_cached_items(pdf_path, start_0idx, end_0idx, range_label=range_label)
        if cached is None:
            pytest.skip(
                f"{run_id} のMinerUキャッシュが無いためスキップ"
                "（一度process_pdfを実行すると生成される）"
            )

        items, images_base = cached
        page_offset = start_0idx if start_0idx is not None else 0
        doc = analyze_structure(items, images_base=images_base, page_offset=page_offset)

        assert doc.pages, "ページが1つも構築されていない"
        translatable_sentences = [
            (sentence_id, sentence)
            for page in doc.pages
            for element in page.elements
            if isinstance(element, (TextBlockElement, CaptionElement))
            for sentence, sentence_id in zip(element.sentences, element.sentence_ids)
        ]
        assert translatable_sentences, "翻訳対象文が1つも抽出されていない"
        for sentence_id, sentence in translatable_sentences:
            assert sentence_id
            assert sentence.strip()



class TestStructureOnRealSamples:
    def test_extract_and_number_sentences(self, processed):
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


    def test_front_matter_labels_are_not_counted_as_sentences(self, processed):
        """タイトル・著者・所属は本文の文としてナンバリングされず、専用ラベルになっているか。"""
        page1_text = processed["texts"]["page_01_en.md"]
        lines = page1_text.splitlines()
        assert lines[0] == "[P1-TITLE] EASYCONTROLEDGE: A FOUNDATION-MODEL FINE-TUNING FOR EDGE DETECTION"
        assert lines[1].startswith("[P1-AUTHORS] Hiroki Nakamura")
        assert lines[2].startswith("[P1-AFFIL]")
        # ABSTRACT見出し自体は本文の文として扱われる（章ラベルは"abstract"、文としてナンバリングされる）
        assert lines[3] == "[P1-S1-abstract-S1] ABSTRACT"


    def test_chapter_headings_get_dedicated_labels(self, processed):
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


    def test_cross_page_paragraph_is_kept_on_starting_page(self, processed):
        """段落が物理ページをまたいでいても、MinerUが1つの段落としてまとめた場合は
    開始ページ側のMarkdownに収まる（本ツールが許容している既知の挙動）。"""
        page1_text = processed["texts"]["page_01_en.md"]
        page2_text = processed["texts"]["page_02_en.md"]

        # 元は物理2ページ目にある(2)(3)の列挙が、1ページ目の段落として結合されている
        assert "(2) We add an edge-specific pixel loss" in page1_text
        assert "(3) At inference time, we adopt a Classifier-Free Guidance" in page1_text
        assert "(2) We add an edge-specific pixel loss" not in page2_text


    def test_figure_caption_is_separated_from_body_text(self, processed):
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


    def test_images_are_extracted_as_png(self, processed):
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


    def test_figure_reference_is_placed_in_page2_markdown(self, processed):
        """画像領域に参照用ID [P2-FIG1] が割り当てられ、Markdownに埋め込まれているか。"""
        page2_text = processed["texts"]["page_02_en.md"]
        fig_lines = [line for line in page2_text.splitlines() if line.startswith("![P2-FIG")]
        assert len(fig_lines) == 1
        m = FIGURE_ID_RE.match(fig_lines[0])
        assert m is not None, f"unexpected figure line format: {fig_lines[0]!r}"
        assert m.group(3) == "fig_p2_1.png"

        # page1には図が存在しない
        assert "![P1-FIG" not in processed["texts"]["page_01_en.md"]


    def test_all_three_equations_are_detected_with_latex(self, processed):
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


    def test_inline_math_is_wrapped_in_dollar_signs(self, processed):
        """文中に埋め込まれたインライン数式が、MinerUの数式認識により$...$で
    構造化されているか（2箇所の固定例による回帰用spot check）。"""
        page2_text = processed["texts"]["page_02_en.md"]
        assert "$p ( y \\mid x )$" in page2_text
        # 単位行列を表す"I"（英語の代名詞"I"と同形）も、正しく数式側に含まれていること
        assert "\\mathbf { I }" in page2_text


    def test_header_and_footer_noise_excluded(self, processed):
        """arXiv IDなどのノイズが本文に含まれていないか。"""
        full_text = "\n".join(processed["texts"].values())
        assert "arXiv:" not in full_text



_UNKNOWN_ELEMENT_TAG_RE = re.compile(r"^\[P\d+-UNKNOWN-([^\]]+?)-\d+\]", re.MULTILINE)


def _find_unknown_element_raw_types(texts: dict[str, str]) -> set[str]:
    """page_XX_en.mdの束から、MinerUが構造解析（工程(2)後半）で未対応の要素種別
    （_handle_unknown_itemへフォールバックしたraw_type）の集合を洗い出す。

    _NOISE_TYPES（意図的に捨てるノイズ）に該当しない、MinerUが返す
    その他全ての未対応type（"header"・"page_number"等）が対象になる。
    翻訳エンジンに一切依存しない（MinerUキャッシュだけで完結する）ため、
    1つの許可リストで管理できる。
    """
    raw_types: set[str] = set()
    for text in texts.values():
        raw_types.update(_UNKNOWN_ELEMENT_TAG_RE.findall(text))
    return raw_types


# 未対応要素種別の許可リスト。既知・許容できるraw_typeのみをここに追加する
# （運用は数式漏れの許可リストと同じ: 目視確認の上でのみ追加し、機械的な
# 追加は禁止）。空集合のままなら、構造解析（工程(2)後半）が未対応の要素種別を
# 1つでも生成した時点でテストが失敗し、人間の確認を促す。
KNOWN_UNKNOWN_ELEMENT_RAW_TYPES: set[str] = set()
KNOWN_UNKNOWN_ELEMENT_RAW_TYPES_SAMPLE1: set[str] = {
    # sample1.pdf Appendix A.1に実在する画像生成プロンプト例文2件。
    # MinerUの見た目ベースの分類でコードブロックと判定されているが、
    # 実際は自然言語のプロンプト文であり、本物のソースコードとは違い
    # 一律の自動翻訳ルールを適用するのは別のリスクがあるため、
    # 目視確認の上で許容（翻訳されず原文のまま表示される）。
    "code",
}
KNOWN_UNKNOWN_ELEMENT_RAW_TYPES_SAMPLE2: set[str] = set()
KNOWN_UNKNOWN_ELEMENT_RAW_TYPES_SAMPLE3: set[str] = set()


def _assert_no_unexpected_unknown_elements(texts: dict[str, str], known: set[str]) -> None:
    unexpected = _find_unknown_element_raw_types(texts) - known
    assert not unexpected, (
        "構造解析（工程(2)後半）が未対応の要素種別としてフォールバックした"
        f"raw_typeのうち、許可リストに無いものが見つかりました: {sorted(unexpected)}。"
        "実際のpage_XX_en.md・原文PDFを目視確認し、"
        "(a) 本来ノイズとして除外すべきなら_NOISE_TYPES（または専用ハンドラ）を追加、"
        "(b) 翻訳対象の本文として扱うべきなら専用ハンドラを追加、"
        "(c) 実害が無い既知の断片なら本関数の許可リストへ追加、のいずれかで対応すること。"
    )




class TestUnknownElementsAndSentenceNumbering:
    def test_no_unexpected_unknown_elements_in_sample0(self, processed):
        """sample0.pdfで、MinerUが未対応の要素種別を生成していないか
    （見つかれば、本文への意図しない紛れ込み・ノイズ除外漏れの可能性がある）。"""
        _assert_no_unexpected_unknown_elements(processed["texts"], KNOWN_UNKNOWN_ELEMENT_RAW_TYPES)


    def test_no_unexpected_unknown_elements_in_sample1(self, processed_sample1):
        """sample1.pdf版。"""
        _assert_no_unexpected_unknown_elements(processed_sample1["texts"], KNOWN_UNKNOWN_ELEMENT_RAW_TYPES_SAMPLE1)


    def test_no_unexpected_unknown_elements_in_sample2(self, processed_sample2):
        """sample2.pdf版。"""
        _assert_no_unexpected_unknown_elements(processed_sample2["texts"], KNOWN_UNKNOWN_ELEMENT_RAW_TYPES_SAMPLE2)


    def test_no_unexpected_unknown_elements_in_sample3(self, processed_sample3):
        """sample3.pdf（印刷ページラベル55〜60）版。"""
        _assert_no_unexpected_unknown_elements(processed_sample3["texts"], KNOWN_UNKNOWN_ELEMENT_RAW_TYPES_SAMPLE3)


    def test_sentence_ids_are_sequential_per_page(self, processed):
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




class TestTableCaptionExtraction:
    def test_table_captions_sample1(self, processed_sample1):
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


    def test_table_captions_sample2(self, processed_sample2):
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




class TestTableImagesAndBookHeadings:
    def test_table_images_are_extracted_as_png_sample1(self, processed_sample1):
        _assert_table_pngs_extracted(processed_sample1)


    def test_table_images_are_extracted_as_png_sample2(self, processed_sample2):
        _assert_table_pngs_extracted(processed_sample2)


    def test_unnumbered_headings_in_real_book_pdf(self, tmp_path):
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




