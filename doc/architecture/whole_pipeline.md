# whole_pipeline.md — `mainCode/whole_pipeline/whole_pipeline.py`

## 1. 概要

`mainCode/whole_pipeline/whole_pipeline.py`は、`python translate_paper.py <PDF>`というCLI実行の実体（エントリポイント本体）である。ルート直下の`translate_paper.py`は、`mainCode`配下が絶対import（`mainCode.xxx.yyy`）で相互参照するため、`mainCode/whole_pipeline/whole_pipeline.py`を直接実行するとプロジェクトルートがsys.pathに乗らずimportに失敗する問題を避けるための薄い橋渡し役であり、実処理はすべてこのファイルの`main()`にある。

`main()`は、CLI引数の解析から工程(1)〜(7)の入口関数をこの順に直接呼び出し、対訳版・英語版・日本語版の3種類のPDFを生成するところまでを一貫して担う、パイプライン全体の統括役である。

## 2. 構成要素（4グループ）

このファイルに独自`class`定義は無い（データを運ぶ型を持つグループは無く、4グループとも関数のみ）。

- **CLI起動処理（関数）**: 引数パーサーの組み立て（`_build_arg_parser`）・入力PDFの事前検証（`_require_pdf_exists`）。
- **出力先の決定（関数）**: `output_dir`・`snapshot_dir`の"パスを決める"だけで、ファイルへの書き込みは一切行わない純粋関数群（`describe_page_range`・`default_output_dir`・`_resolve_output_dir`・`_resolve_snapshot_dir`）。
- **実行記録の書き出し（関数）**: 決定済みの`snapshot_dir`へ、各工程の完了直後に実際にファイルを書き込む関数群（`_write_structured_snapshot`・`_write_translation_snapshot`・`_write_restore_snapshot`）。`snapshot_dir`の決定は1回（`_resolve_snapshot_dir`）だが、書き出しは工程(3)〜(5)の完了ごとに3回（`03_structured/`・`04_deepl_output/`・`05_restored/`）行われる1対多の関係のため、「決定」グループとは対応するペアを作らずグループごと分離している。
- **入口（関数）**: `main`。CLI引数の解析から工程(1)〜(7)の入口関数をこの順に直接呼び出す、パイプライン全体の統括役。上記3グループの関数はいずれも`main`からのみ呼ばれる。

## 3. main()の処理フロー

### 3.1 関数依存関係図

このファイル内での呼び出し関係は以下の通りで、これを正とする（4節の「呼び出し元」記載はここからの要約）。

```
main()
├── _build_arg_parser()
├── _require_pdf_exists()
├── describe_page_range()
├── _resolve_output_dir()
│     └── default_output_dir()          … --output_dir省略時のみ
├── _resolve_snapshot_dir()
├── _write_structured_snapshot()
├── _write_translation_snapshot()
└── _write_restore_snapshot()
```

`main()`以外の8関数はすべて`main()`からのみ呼ばれ、ファイル内で複数箇所から呼ばれる関数は無い。ファイル内で唯一の非自明な依存が`_resolve_output_dir() → default_output_dir()`で、`--output_dir`省略時のみ後者が呼ばれる条件付き呼び出しになっている。

なお、`main()`はこの他に他ファイルの工程入口関数（`resolve_page_range`・`process_pdf`・`prepare_translation_input`・`translate_units`・`apply_restore`・`stage6_postprocess`・`render_units_to_pdfs`、いずれも工程(1)〜(7)に対応）と、横断的なログ出力関数`_log`（`mainCode/shared/shared.py`の`log`）も呼ぶ。

### 3.2 直接呼び出しの設計

`main()`は工程(4)（`translate_units`）・工程(5)（`apply_restore`）・工程(6)（`stage6_postprocess`）を、他のどの工程とも同じく結合用のラッパーを介さず直接呼ぶ。`run_translation()`のような専用の中間関数は無く、翻訳エンジンからの生の応答（`raw_results`）をcache/へ記録として保存する処理も`main()`自身が`translate_units()`の直後に直接行う。

### 3.3 実行記録スナップショットの1対多関係

`_resolve_snapshot_dir`は`pdf_path`・`range_label`が両方指定されている場合のみ、翻訳エンジンとの送受信内容を記録する保存先のパスを1回だけ決める（`prepare_translation_input`の直後）。以降、工程(3)〜(5)それぞれの完了直後に`_write_structured_snapshot`（`03_structured/`）・`_write_translation_snapshot`（`04_deepl_output/`）・`_write_restore_snapshot`（`05_restored/`）が順に呼ばれ、`snapshot_dir`が`None`の場合（タグ付きMarkdownからの再開等、対象PDFが不明な場合）はいずれも呼ばれない。

### 3.4 エラー処理方針

`resolve_page_range`が送出する`ChapterResolutionError`・`PageLabelResolutionError`、`translate_units`（工程(4)、`call_deepl`経由）が送出する`TranslationBackendError`はいずれも`main()`が捕捉し、分かりやすい日本語メッセージを添えた`SystemExit`へ正規化する。入力PDFの不在も同様に`_require_pdf_exists`が事前チェックし、`SystemExit`に統一する（詳細は4節`_require_pdf_exists`の項参照）。

## 4. 構成要素リファレンス

各項目は先頭に`種別`（関数／データ構造／ヘルパークラス）を持つ。このファイルの構成要素はすべて関数で、3.1節の依存関係図と同じ順に並べている。各項目の「グループ」は2節の4グループ（CLI起動処理／出力先の決定／実行記録の書き出し／入口）のどれに属するかを示す。データ構造・ヘルパークラス・独自の`class`定義は無い。

### main

- **種別**: 関数
- **グループ**: 入口
- **呼び出し元**: なし（CLIエントリポイント）
- **入力**: なし（`sys.argv`をargparseで解釈）
- **出力**: なし
- **処理内容**: 標準出力のUTF-8化・`.env`の読み込み → CLI引数解析（`_build_arg_parser`） → 入力PDF存在確認（`_require_pdf_exists`） → ページ範囲決定（`resolve_page_range`、工程(1)） → 範囲記述子・出力先決定（`describe_page_range`・`_resolve_output_dir`） → PDF解析（`process_pdf`、工程(2)） → 構造化・数式保護仕上げ（`prepare_translation_input`、工程(3)） → スナップショット保存先決定・書き出し（`_resolve_snapshot_dir`・`_write_structured_snapshot`） → 翻訳実行（`translate_units`、工程(4)）・書き出し（`_write_translation_snapshot`） → 数式復元（`apply_restore`、工程(5)）・書き出し（`_write_restore_snapshot`） → 数式保護（`stage6_postprocess`、工程(6)） → PDF生成（`render_units_to_pdfs`、工程(7)） → 生成PDFパス一覧をログ出力。
- **テスト対象**: `main`自体を直接呼び出すテストは無い（CLIレベルのエンドツーエンドテストは実施していない）。個々の呼び出し先はそれぞれのテストで検証される。

### _build_arg_parser

- **種別**: 関数
- **グループ**: CLI起動処理
- **呼び出し元**: `main()`
- **入力**: なし
- **出力**: `argparse.ArgumentParser`（`pdf_path`/`output_dir`/`--chapter`/`--start`/`--end`/`--start-label`/`--end-label`/`--mineru-backend`を定義済み）
- **処理内容**: `main()`のCLI引数パーサーを組み立てる。各引数のhelp文字列自体に説明を持たせている。
- **テスト対象**: 直接のテストは無い。

### _require_pdf_exists

- **種別**: 関数
- **グループ**: CLI起動処理
- **呼び出し元**: `main()`（argparseの直後、`resolve_page_range`より前）
- **入力**: `pdf_path`（入力PDFファイルのパス）
- **出力**: なし
- **処理内容**: `Path(pdf_path).is_file()`で入力PDFファイルが実在するかを確認する。`resolve_page_range`/`process_pdf`内部（fitzでのPDF読み込み等）は存在しないパスに対して生の`FileNotFoundError`を送出するが、それだと他の入力エラー（章指定・印刷ページラベルの解決失敗等）とは違い分かりにくいトレースバックがそのまま表示されてしまう。この事前チェックにより、他のエラーと同じ「分かりやすい日本語メッセージ＋`SystemExit`」の形に統一する。
- **テスト対象**: `test_require_pdf_exists_raises_for_missing_file`で、存在しないパスを渡した場合にfitzの生の`FileNotFoundError`ではなく「入力PDFファイルが見つかりません: ...」という`SystemExit`になることを、`test_require_pdf_exists_passes_for_existing_file`で、実在するパス（sample0.pdf）では何も送出しない（正常系）ことを確認している。

### describe_page_range

- **種別**: 関数
- **グループ**: 出力先の決定
- **呼び出し元**: `main()`
- **入力**: `chapter`・`start`・`end`・`start_label`・`end_label`（いずれも`resolve_page_range`に渡すのと同じ、解決前のCLI引数）
- **出力**: `cache/`・`output/`配下のフォルダ名に使う人間可読な範囲記述子（str。例:`"full"`、`"chapter1-2"`、`"p66-71"`、`"label55-60"`）
- **処理内容**: `resolve_page_range`と同じ引数を受け取り、同じ優先順位（印刷ページラベル・物理ページ番号 > 章指定 > 指定なし＝`"full"`）で表示用の記述子を組み立てる。`resolve_page_range`とは責務を分離しており（あちらは物理ページ番号への"解決"、こちらは"命名"のみ）、`resolve_page_range`の戻り値の型（2要素タプル）を変更せずに済む。この記述子は`main()`経由で`process_pdf`・MinerUキャッシュへ渡され、`cache/`配下のMinerUキャッシュフォルダ名（`<PDFのstem>_<range_label>`）や、DeepL翻訳との実際の送受信内容の保存先フォルダ名にもなる。
- **テスト対象**: `test_describe_page_range`（parametrize、6ケース）で、章指定・物理ページ指定・印刷ページラベル指定・指定なしそれぞれの記述子が正しく組み立てられること、および優先順位が`resolve_page_range`と一致することを確認している。

### _resolve_output_dir

- **種別**: 関数
- **グループ**: 出力先の決定
- **呼び出し元**: `main()`
- **入力**: `args`（argparseの解析結果）、`range_label`（`describe_page_range`の戻り値）
- **出力**: 出力先ディレクトリ（`args.output_dir`が指定済みならそのまま、省略時は`default_output_dir`が組み立てたPath）
- **処理内容**: `args.output_dir`が指定済みならそのまま返す。省略時（`None`）のみ`default_output_dir`を呼んで自動生成し、その旨をログ出力する。
- **テスト対象**: 直接のテストは無い（`default_output_dir`自体は`test_default_output_dir_builds_manual_naming_convention`で検証されるが、`_resolve_output_dir`経由の呼び出しを検証するテストは無い）。

### default_output_dir

- **種別**: 関数
- **グループ**: 出力先の決定
- **呼び出し元**: `_resolve_output_dir()`（`--output_dir`省略時のみ）
- **入力**: `pdf_path`・`range_label`（`describe_page_range`の戻り値）・`timestamp`（省略時は呼び出し時刻。テストから固定値を注入するための引数）
- **出力**: 自動生成された出力先ディレクトリのPath（`output/manual_{PDF名}_{range_label}_{timestamp}`）
- **処理内容**: CLI引数の`output_dir`が省略された場合にのみ呼ばれる。`doc/testExplain.txt`が定める人間による手動実行（「本実行」）の命名規則と同じ形式で自動生成することで、実行のたびに出力先を手入力する必要をなくす。
- **テスト対象**: `test_default_output_dir_builds_manual_naming_convention`で、固定の`timestamp`を注入した場合に期待通りのPathが組み立てられることを確認している。

### _resolve_snapshot_dir

- **種別**: 関数
- **グループ**: 出力先の決定
- **呼び出し元**: `main()`（`prepare_translation_input`の直後）
- **入力**: `pdf_path`（対象の入力PDFファイルのパス、不明な場合は`None`）・`range_label`（`describe_page_range`の戻り値、不明な場合は`None`）
- **出力**: スナップショット保存先のPath（`pdf_path`・`range_label`が両方指定されている場合のみ。`cache/<PDFのstem>_<range_label>/real_deepl_output_<タイムスタンプ>/`）、それ以外は`None`
- **処理内容**: `pdf_path`・`range_label`が両方指定されている場合のみ、翻訳エンジンとの送受信内容を記録するスナップショット保存先のパスを決める。ここではパスを決めるだけで、ファイルへの書き込みは一切行わない（書き込みは`_write_structured_snapshot`以降の各関数が担う）。片方でも指定が無い場合（タグ付きMarkdownからの再開等、対象PDFが不明な場合）は`None`を返し、以降の記録保存もすべて行われない。
- **テスト対象**: `test_resolve_snapshot_dir_returns_none_when_pdf_path_or_range_label_missing`で片方欠落時に`None`を返すこと、`test_resolve_snapshot_dir_builds_cache_path_with_deepl_folder_name`でパス構成（`cache/<PDF名>_<範囲記述子>/real_deepl_output_*`）を直接確認している。全体テスト（`test_run_pipeline_end_to_end_with_real_deepl`）も、`pdf_path`/`range_label`指定時に実際に`cache/`配下へスナップショットが書き出されることを間接的に検証している。

### _write_structured_snapshot

- **種別**: 関数
- **グループ**: 実行記録の書き出し
- **呼び出し元**: `main()`（`snapshot_dir`が`None`でない場合のみ、`prepare_translation_input`の直後）
- **入力**: `output_dir`（`page_*_en.md`が格納されたディレクトリ）・`snapshot_dir`（`_resolve_snapshot_dir`が決めた保存先）・`document_context`（`prepare_translation_input`の戻り値）
- **出力**: なし（副作用としてファイルを書き出すのみ）
- **処理内容**: 工程(3)完了時点の状態（`document_context`・`page_*_en.md`）を`snapshot_dir/03_structured/`へ書き出す。
- **テスト対象**: `test_write_structured_snapshot_writes_context_and_copies_pages`で`document_context.txt`と`page_*_en.md`のコピーが書き出されることを直接確認している。全体テスト（`test_run_pipeline_end_to_end_with_real_deepl`）も、`pdf_path`/`range_label`指定時に実際に`cache/`配下へ書き出されることを間接的に検証している。

### _write_translation_snapshot

- **種別**: 関数
- **グループ**: 実行記録の書き出し
- **呼び出し元**: `main()`（`snapshot_dir`が`None`でない場合のみ、`translate_units`の直後）
- **入力**: `snapshot_dir`・`raw_results`（`translate_units`が返した、`unit.tag`をキーにした`RawTranslationResult`辞書）
- **出力**: なし（副作用としてファイルを書き出すのみ）
- **処理内容**: `raw_results`をJSON化して`snapshot_dir/04_deepl_output/raw_deepl_results.json`へ記録として保存する（工程(4)自体の処理ではなく、実行記録の保存機能）。
- **テスト対象**: `test_write_translation_snapshot_writes_raw_results_json`で`04_deepl_output/raw_deepl_results.json`が`RawTranslationResult`のJSON表現で書き出されることを直接確認している。全体テスト（`test_run_pipeline_end_to_end_with_real_deepl`）も、`main()`と同じ関数の並びを通してこの関数を呼び、生成されたJSONファイルの実在を間接的に検証している。

### _write_restore_snapshot

- **種別**: 関数
- **グループ**: 実行記録の書き出し
- **呼び出し元**: `main()`（`snapshot_dir`が`None`でない場合のみ、`apply_restore`の直後）
- **入力**: `units`（`apply_restore`適用済みのDocUnitの列）・`snapshot_dir`（書き出し先ディレクトリ）
- **出力**: なし
- **処理内容**: `snapshot_dir`が指定された場合のみ`main()`が呼ぶ、cache/への記録保存を担う薄いヘルパー。工程(6)の保護処理適用前（`apply_restore`直後）のDocUnitスナップショットを`snapshot_dir/05_restored/`へ書き出す。`units_raw.json`（オフライン回帰テストの入力になる）と、同じ状態の`page_XX_en.md`/`page_XX_ja.md`を`write_translated_pages`（`mainCode/stage6/stage6.py`）を呼んで書き出す。`output_dir`側の同名ファイルはこの後`write_translated_pages`（工程(6)本体）により保護後の内容で上書きされるため、保護前の状態を確認したい場合はこちら（`05_restored`）を参照する。
- **テスト対象**: `test_write_restore_snapshot_records_pre_protection_state`で、`apply_restore`→`_write_restore_snapshot`の順に直接呼び、`snapshot_dir`指定時に`units_raw.json`と`page_XX_en.md`/`page_XX_ja.md`が、保護処理適用前の状態のまま実際に書き出されることを確認している。全体テスト（`snapshot_dir`指定時のみ間接的に検証）についても同様。

## 5. 関連ドキュメント

- `doc/architecture.md` §2「パイプラインの7工程」: 7工程モデルの正データ。工程(1)〜(7)全体の中でこのファイルの各関数が担う役割の位置づけを説明する。
- `doc/architecture.md`: プロジェクト全体のディレクトリ構成・呼び出し木図（4.1節）・モジュール依存関係（4.2節）の中でのこのファイルの位置づけ。
