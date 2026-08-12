"""MinerUのバージョン文字列取得ユーティリティ（キャッシュキー生成用）。"""

from __future__ import annotations

import importlib.metadata


class MinerUVersionError(RuntimeError):
    """MinerUのバージョンが取得できない場合に送出する例外。"""


def get_mineru_version() -> str:
    """インストール済みMinerUパッケージのバージョン文字列を返す。

    キャッシュキーの構成要素として使うため、``mineru``パッケージが
    正しくインストールされていない環境では例外にしてキャッシュを無効化
    する（誤って別バージョンのキャッシュを使い回すことを防ぐ）。
    """
    try:
        return importlib.metadata.version("mineru")
    except importlib.metadata.PackageNotFoundError as exc:
        raise MinerUVersionError(
            "mineruパッケージのバージョンが取得できません（未インストールの可能性）。"
        ) from exc
