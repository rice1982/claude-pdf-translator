"""MinerU実行結果（content_list.json + 画像）のローカルキャッシュ。

PDFの中身が変わらず、かつMinerUのバージョンが変わらない限り、過去の
MinerU実行結果を再利用してサブプロセス起動コストを省く。テスト・開発
ループの高速化のみを目的とした最適化であり、本番の翻訳パイプラインの
正しさには一切関与しない（読み込み・保存いずれかに失敗した場合は常に
「キャッシュ不使用の通常実行」にフォールバックする）。

``pdf_mineru_runner``とは循環importを避けるため、``MinerUOutput``型には
依存せず、``items``（``list[dict]``）と``images_base``（``Path``）を
そのままやり取りする。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from mineru_version import MinerUVersionError, get_mineru_version

# キャッシュのバグで本来のMinerU実行やテストが失敗する事態を避けるため、
# 読み込み・保存関連の失敗は原則としてこのモジュール内で握りつぶす。


def _cache_enabled() -> bool:
    # 呼び出しのたびに環境変数を読む（モジュール読み込み時に固定すると、
    # テストでの`monkeypatch.setenv`が反映されずテストしにくくなるため）。
    return os.environ.get("MINERU_CACHE_DISABLE", "") != "1"


_REPO_ROOT = Path(__file__).resolve().parent
_CACHE_ROOT = _REPO_ROOT / "cache"

# キャッシュの保存形式自体を将来変更した際に、古い形式のキャッシュを
# 機械的に無効化するためのスキーマバージョン。
_CACHE_SCHEMA_VERSION = 1


def _run_id(
    pdf_path: Path, start_page: int | None, end_page: int | None, range_label: str | None = None
) -> str:
    stem = pdf_path.stem
    if range_label is not None:
        # range_labelは呼び出し側（translate_paper.describe_page_range）が
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


def _cache_dir(
    pdf_path: Path, start_page: int | None, end_page: int | None, range_label: str | None = None
) -> Path:
    return _CACHE_ROOT / _run_id(pdf_path, start_page, end_page, range_label) / "mineru_cache"


def _compute_pdf_hash(pdf_path: Path) -> str:
    hasher = hashlib.sha256()
    with pdf_path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_cached_items(
    pdf_path: Path,
    start_page: int | None,
    end_page: int | None,
    range_label: str | None = None,
) -> tuple[list[dict], Path] | None:
    """キャッシュがあり、かつPDFの中身・MinerUバージョンが一致すれば
    ``(items, images_base)``を返す。キャッシュが無い・古い・壊れている
    場合は``None``を返し、呼び出し側に通常実行を促す。

    ``range_label``はフォルダ名の組み立てにのみ使う（save_cache参照）。
    キャッシュの正当性判定自体は従来通りstart_page/end_page（と
    pdf_sha256・mineru_version）で行う。
    """
    if not _cache_enabled():
        return None

    cache_dir = _cache_dir(pdf_path, start_page, end_page, range_label)
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
    ):
        return None

    try:
        items = json.loads(content_list_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    return items, cache_dir


def save_cache(
    pdf_path: Path,
    start_page: int | None,
    end_page: int | None,
    items: list[dict],
    images_base: Path,
    range_label: str | None = None,
) -> None:
    """MinerUの実行結果をキャッシュへ保存する。

    一時ディレクトリへ書き込んでから``rename``でアトミックに差し替える
    ことで、書き込み途中の中断による破損キャッシュの混入を防ぐ。

    ``range_label``を指定すると、フォルダ名が``{stem}_{range_label}``
    （例:"sample3_label55-60"）になり、生のページ番号ベースの命名
    （``{stem}_p{開始}_{終了}``）より人間が読みやすくなる。省略時は従来通り。
    """
    if not _cache_enabled():
        return

    try:
        pdf_hash = _compute_pdf_hash(pdf_path)
        version = get_mineru_version()
    except (OSError, MinerUVersionError):
        return

    cache_dir = _cache_dir(pdf_path, start_page, end_page, range_label)
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

        meta = {
            "cache_schema_version": _CACHE_SCHEMA_VERSION,
            "pdf_sha256": pdf_hash,
            "mineru_version": version,
            "start_page": start_page,
            "end_page": end_page,
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
