"""CLIエントリーポイント。実体は mainCode/whole_pipeline/whole_pipeline.py にある。

本コード一式（stage1〜6・shared・whole_pipeline/）は
mainCode/ フォルダにまとめられている。mainCode 配下のモジュールはお互いを
`mainCode.xxx`という絶対importで参照し合っているため、`python
mainCode/whole_pipeline/whole_pipeline.py <PDF>` のように直接実行すると
プロジェクトルートがsys.pathに乗らずimportに失敗する。このファイルは、
`python translate_paper.py <PDF>` という呼び出し方を保つための、
プロジェクトルート直下の薄い橋渡し役に過ぎない。
"""
from __future__ import annotations

from mainCode.whole_pipeline.whole_pipeline import main

if __name__ == "__main__":
    main()
