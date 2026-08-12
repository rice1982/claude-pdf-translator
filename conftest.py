"""pytest全体で共有する設定。"""

from __future__ import annotations

import shutil
from pathlib import Path


def pytest_sessionfinish(session: object, exitstatus: int) -> None:
    """テスト終了後、__pycache__ディレクトリを削除する。

    .pytest_cache（--lf/--ffで使う直前の失敗記録）は削除対象外とする。
    venv/配下は対象外（依存パッケージの再インストールを誘発しないため）。
    """
    root = Path(__file__).resolve().parent
    for path in root.rglob("__pycache__"):
        if "venv" in path.parts:
            continue
        shutil.rmtree(path, ignore_errors=True)
