"""mineru_cache / mineru_version、および run_mineru()へのキャッシュ組み込みの
単体テスト。実MinerUは一切起動しない（subprocess.runをモック化する）。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import fitz  # PyMuPDF
import pytest

import mineru_cache
import pdf_mineru_runner
from mineru_version import MinerUVersionError


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """開発者の実キャッシュ（cache/）を汚染しないよう、キャッシュ保存先を
    テストごとの一時ディレクトリへ差し替える。"""
    monkeypatch.setattr(mineru_cache, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(mineru_cache, "get_mineru_version", lambda: "test-version-1")


def _make_pdf(path: Path, page_count: int = 1) -> None:
    doc = fitz.open()
    for _ in range(page_count):
        doc.new_page()
    doc.save(str(path))
    doc.close()


def _make_images_base(tmp_path: Path, name: str = "images_base") -> Path:
    images_base = tmp_path / name
    (images_base / "images").mkdir(parents=True)
    (images_base / "images" / "fig1.jpg").write_bytes(b"fake-image-bytes")
    return images_base


ITEMS = [{"type": "text", "text": "hello", "text_level": None, "page_idx": 0}]


def test_load_returns_none_when_no_cache(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path)

    assert mineru_cache.load_cached_items(pdf_path, None, None) is None


def test_save_then_load_roundtrip(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path)
    images_base = _make_images_base(tmp_path)

    mineru_cache.save_cache(pdf_path, None, None, ITEMS, images_base)
    cached = mineru_cache.load_cached_items(pdf_path, None, None)

    assert cached is not None
    items, images_dir = cached
    assert items == ITEMS
    assert (images_dir / "images" / "fig1.jpg").read_bytes() == b"fake-image-bytes"


def test_cache_isolated_by_page_range(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path, page_count=3)
    images_base = _make_images_base(tmp_path)

    mineru_cache.save_cache(pdf_path, 0, 1, ITEMS, images_base)

    assert mineru_cache.load_cached_items(pdf_path, 0, 1) is not None
    assert mineru_cache.load_cached_items(pdf_path, 1, 2) is None
    assert mineru_cache.load_cached_items(pdf_path, None, None) is None


def test_load_returns_none_when_pdf_content_changes(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path, page_count=1)
    images_base = _make_images_base(tmp_path)
    mineru_cache.save_cache(pdf_path, None, None, ITEMS, images_base)
    assert mineru_cache.load_cached_items(pdf_path, None, None) is not None

    # 同じパスに別内容のPDFを書き直す（著者の改訂・別ファイルへの差し替え等を想定）。
    _make_pdf(pdf_path, page_count=5)

    assert mineru_cache.load_cached_items(pdf_path, None, None) is None


def test_load_returns_none_when_mineru_version_changes(tmp_path, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path)
    images_base = _make_images_base(tmp_path)

    monkeypatch.setattr(mineru_cache, "get_mineru_version", lambda: "1.0.0")
    mineru_cache.save_cache(pdf_path, None, None, ITEMS, images_base)
    assert mineru_cache.load_cached_items(pdf_path, None, None) is not None

    monkeypatch.setattr(mineru_cache, "get_mineru_version", lambda: "2.0.0")
    assert mineru_cache.load_cached_items(pdf_path, None, None) is None


def test_load_returns_none_on_corrupted_content_list_json(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path)
    images_base = _make_images_base(tmp_path)
    mineru_cache.save_cache(pdf_path, None, None, ITEMS, images_base)

    cache_dir = mineru_cache._cache_dir(pdf_path, None, None)
    (cache_dir / "content_list.json").write_text("{not valid json", encoding="utf-8")

    assert mineru_cache.load_cached_items(pdf_path, None, None) is None


def test_save_is_noop_when_pdf_hash_fails(tmp_path, monkeypatch):
    pdf_path = tmp_path / "does_not_exist.pdf"
    images_base = _make_images_base(tmp_path)

    # ファイルが存在しない場合、_compute_pdf_hashはOSErrorを送出するため、
    # save_cacheは例外を伝播させず静かに何もしない。
    mineru_cache.save_cache(pdf_path, None, None, ITEMS, images_base)

    assert not (tmp_path / "cache").exists()


def test_cache_disabled_via_env_var(tmp_path, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path)
    images_base = _make_images_base(tmp_path)

    mineru_cache.save_cache(pdf_path, None, None, ITEMS, images_base)
    assert mineru_cache.load_cached_items(pdf_path, None, None) is not None

    monkeypatch.setenv("MINERU_CACHE_DISABLE", "1")
    # 既にキャッシュが存在していても、無効化フラグが優先される。
    assert mineru_cache.load_cached_items(pdf_path, None, None) is None

    mineru_cache.save_cache(pdf_path, 0, 0, ITEMS, images_base)
    assert mineru_cache.load_cached_items(pdf_path, 0, 0) is None
    monkeypatch.delenv("MINERU_CACHE_DISABLE")
    assert mineru_cache.load_cached_items(pdf_path, 0, 0) is None  # 無効化中は保存自体されていない


def test_get_mineru_version_raises_on_missing_package(monkeypatch):
    import importlib.metadata

    import mineru_version

    def _raise(_name):
        raise importlib.metadata.PackageNotFoundError()

    monkeypatch.setattr(importlib.metadata, "version", _raise)
    with pytest.raises(MinerUVersionError):
        mineru_version.get_mineru_version()


def _install_fake_mineru_subprocess(monkeypatch, call_counter: list[int]):
    def _fake_run(command, check, capture_output):
        call_counter[0] += 1
        # コマンドは [..., "--path", pdf_path, "--output", work_dir, ...]
        pdf_path = Path(command[command.index("--path") + 1])
        work_dir = Path(command[command.index("--output") + 1])
        stem = pdf_path.stem
        auto_dir = work_dir / stem / "auto"
        (auto_dir / "images").mkdir(parents=True)
        (auto_dir / "images" / "fig1.jpg").write_bytes(b"fake-image-bytes")
        (auto_dir / f"{stem}_content_list.json").write_text(
            json.dumps(ITEMS), encoding="utf-8"
        )
        return MagicMock(returncode=0)

    monkeypatch.setattr(pdf_mineru_runner.subprocess, "run", _fake_run)


def test_run_mineru_uses_cache_on_second_call(tmp_path, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path)
    call_counter = [0]
    _install_fake_mineru_subprocess(monkeypatch, call_counter)

    work_dir_1 = tmp_path / "work1"
    work_dir_1.mkdir()
    result_1 = pdf_mineru_runner.run_mineru(pdf_path, work_dir_1)
    assert call_counter[0] == 1
    assert result_1.items == ITEMS

    work_dir_2 = tmp_path / "work2"
    work_dir_2.mkdir()
    result_2 = pdf_mineru_runner.run_mineru(pdf_path, work_dir_2)
    assert call_counter[0] == 1  # 2回目はキャッシュヒットでsubprocessが呼ばれない
    assert result_2.items == ITEMS
    assert (result_2.images_base / "images" / "fig1.jpg").exists()


def test_run_mineru_reruns_after_version_change(tmp_path, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path)
    call_counter = [0]
    _install_fake_mineru_subprocess(monkeypatch, call_counter)

    monkeypatch.setattr(mineru_cache, "get_mineru_version", lambda: "1.0.0")
    pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work1")
    assert call_counter[0] == 1

    monkeypatch.setattr(mineru_cache, "get_mineru_version", lambda: "2.0.0")
    (tmp_path / "work2").mkdir()
    pdf_mineru_runner.run_mineru(pdf_path, tmp_path / "work2")
    assert call_counter[0] == 2
