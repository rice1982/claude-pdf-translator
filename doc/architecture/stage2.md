# stage2.md — `mainCode/stage2/stage2.py`

## 1. 概要

`mainCode/stage2/stage2.py`は工程(2)「PDF解析」を担う、7工程中もっとも複雑なファイルである。

やっていることを一言でいうと、工程(1)が確定した物理ページ範囲のPDFをMinerU（外部の解析ツール）に子プロセスで通して生の構造化データ（content_list）と画像を取得し、その生データを本ツール独自の文単位ID体系（例: `[P1-S1-abstract-S1]`）を持つタグ付きMarkdown（`page_XX_en.md`）へ変換して書き出す処理である（実際の翻訳は行わない。それは工程(4)）。

MinerU実行という唯一の重い外部処理を含むため、性質の異なる複数種類のコード（下記）が1ファイルに同居している。

- **中間表現**: MinerU実行結果と構造解析結果を受け渡すためだけの9個のdataclass（`TextBlockElement`等）。工程(2)内で完結し、原文Markdownへ書き出された時点で役目を終える。
- **テキストユーティリティ**: PDF/MinerUの出力形式に関する知識を一切持たない、独立した純粋な文字列変換関数群（文分割・改行ハイフン復元・裸のギリシャ文字や「1文字の変数=値」形式の断片の自動数式保護等）。構造解析から呼ばれるが、他のどのモジュールにも依存しない末端。
- **キャッシュ**（`load_cached_items`/`save_cache`/`get_mineru_version`等）: 同一条件での再実行を高速化するためだけの、速度最適化専用のコード。読み込み・保存いずれの失敗も本番の正しさに影響させず、常に通常実行へフォールバックする。
- **本体**（`run_mineru`）: MinerUをサブプロセスとして実行し、生の構造化JSON（content_list）と画像群を取得する。失敗時はフォールバックせず`MinerURunError`を送出する。
- **構造解析**（`analyze_structure`とその内部ヘルパー）: MinerUのcontent_list（意味解釈を持たない生データ）を、独自ID体系を持つ`StructuredDocument`へ変換する。要素の判定はMinerUが付与する`type`フィールドによる汎用的な分岐のみで行い、未知の`type`や解析失敗は`UnknownElement`へフォールバックさせる。
- **文書組み立て**（`build_document`とその内部ヘルパー）: `StructuredDocument`を原文のままページ別Markdownとして書き出す。画像の保存もここで行う。
- **入口**（`process_pdf`）: 上記を「MinerU実行→構造解析→成果物結合」の順に呼ぶだけの薄いオーケストレーター。CLIエントリポイントもあわせて提供する。実際の翻訳（工程(4)）はこの時点では行わず、タグ付きMarkdown生成後に文書全体を見てからまとめて行う（DeepLの文脈パラメータを活用するための意図的な分離）。

## 2. 構成要素（7グループ）

- **中間表現（データ構造）**: `TextBlockElement`・`FigureElement`・`EquationElement`・`LabeledElement`・`HeadingElement`・`CaptionElement`・`UnknownElement`・`PageContent`・`StructuredDocument`の9個のdataclass。
- **テキストユーティリティ（関数）**: `split_sentences`・`wrap_bare_greek_letters`（ネスト関数`run_repl`・`char_repl`を含む）・`wrap_bare_letter_equals_expressions`（ネスト関数`run_repl`を含む）・`restore_merged_hyphens`（ネスト関数`repl`を含む）・`split_merged_compound`・`is_known_word`・`slugify_section_name`・`parse_caption_label`・`_apply_outside_math_spans`。
- **キャッシュ（関数）**: `get_mineru_version`・`load_cached_items`・`save_cache`と内部ヘルパー（`_cache_enabled`・`_run_id`・`_mineru_cache_subdir_name`・`_cache_dir`・`_compute_pdf_hash`）。`MinerUVersionError`（例外）も同グループ。
- **本体（関数・データ構造）**: `run_mineru`。`MinerURunError`（例外）・`MinerUOutput`（結果の受け渡し用dataclass）も同グループ。
- **構造解析（関数・ヘルパークラス）**: `analyze_structure`と内部ヘルパー（`_build_pages`・`_assign_sentence_ids`・`_handle_list_item`・`_handle_image_or_table_item`・`_handle_equation_item`・`_handle_text_item`・`_handle_unknown_item`・`_extract_raw_text`・`_normalize_math_text`・`_PageBuilder`）。
- **文書組み立て（関数）**: `build_document`と内部ヘルパー（`_render_page_markdown`・`_save_element_image`・`_figure_label`）。
- **入口（関数）**: `process_pdf`・`main`。

## 3. process_pdf()の処理フロー

### 3.1 関数依存関係図

```
process_pdf()
├── run_mineru()                          … MinerU実行
│     ├── load_cached_items()               （キャッシュ命中時はMinerU実行自体をスキップ）
│     │     ├── _cache_enabled()
│     │     ├── _cache_dir()
│     │     │     ├── _run_id()
│     │     │     └── _mineru_cache_subdir_name()
│     │     ├── _compute_pdf_hash()
│     │     └── get_mineru_version()
│     └── save_cache()                      （キャッシュ不命中時のみ。子関数は load_cached_items と同じ4種）
├── analyze_structure()                   … 構造解析
│     ├── _build_pages()
│     │     ├── _PageBuilder()                （ページごとに1インスタンス）
│     │     ├── _handle_list_item()
│     │     │     └── _normalize_math_text()
│     │     │           ├── wrap_bare_letter_equals_expressions()   … テキストユーティリティ
│     │     │           │     ├── run_repl()                          … $...$ラップのre.subコールバック（ネスト関数）
│     │     │           │     └── _apply_outside_math_spans()
│     │     │           ├── wrap_bare_greek_letters()               … テキストユーティリティ
│     │     │           │     ├── run_repl()                          … $...$ラップのre.subコールバック（ネスト関数）
│     │     │           │     │     └── char_repl()                    … ギリシャ文字→TeXコマンドのre.subコールバック（ネスト関数）
│     │     │           │     └── _apply_outside_math_spans()        （name-only）
│     │     │           └── restore_merged_hyphens()                … テキストユーティリティ
│     │     │                 ├── repl()                              … ハイフン復元のre.subコールバック（ネスト関数）
│     │     │                 │     └── split_merged_compound()
│     │     │                 │           └── is_known_word()
│     │     │                 └── _apply_outside_math_spans()         （name-only）
│     │     ├── _handle_image_or_table_item()
│     │     │     ├── _normalize_math_text()   （name-only、上記参照）
│     │     │     ├── parse_caption_label()     … テキストユーティリティ
│     │     │     └── split_sentences()         … テキストユーティリティ
│     │     ├── _handle_equation_item()
│     │     ├── _handle_text_item()
│     │     │     ├── _normalize_math_text()   （name-only）
│     │     │     ├── slugify_section_name()    … テキストユーティリティ
│     │     │     └── split_sentences()         （name-only）
│     │     └── _handle_unknown_item()
│     │           └── _extract_raw_text()
│     └── _assign_sentence_ids()
└── build_document()                      … 成果物結合
      └── _render_page_markdown()
            ├── _save_element_image()
            └── _figure_label()

main()
└── process_pdf()   … CLIエントリポイント
```

`_normalize_math_text`（構造解析グループ）は、PDF/MinerUの出力形式に関する知識を一切持たないテキストユーティリティ群（`wrap_bare_letter_equals_expressions`／`wrap_bare_greek_letters`／`restore_merged_hyphens`／`split_merged_compound`／`is_known_word`／`_apply_outside_math_spans`）への橋渡し役で、`_handle_list_item`・`_handle_image_or_table_item`・`_handle_text_item`の3ハンドラから呼ばれる（サブツリーは初出の`_handle_list_item`下にのみ展開）。`split_sentences`・`slugify_section_name`・`parse_caption_label`もテキストユーティリティ群で、構造解析ハンドラからのみ呼ばれる。構造解析ハンドラは9個の中間表現dataclass（`TextBlockElement`等）を生成し、`_PageBuilder`（構造解析グループの可変状態ヘルパー）に溜める。

### 3.2 実行フローの設計

`process_pdf`は「MinerU実行（`run_mineru`）→構造解析（`analyze_structure`）→成果物結合（`build_document`）」をこの順に直接呼ぶだけの薄いオーケストレーターで、結合用の中間関数は無い。ページ範囲（`start_page`/`end_page`）は`--start`/`--end`（1始まり）指定時のみMinerUへ0始まりの絶対ページ範囲として渡され、`analyze_structure`にはこの範囲の先頭からの相対ページ番号を絶対ページ番号へ戻すための`page_offset`が渡される。

### 3.3 エラー処理方針

`run_mineru`が送出する`MinerURunError`は`process_pdf`内で捕捉されず、そのまま呼び出し元（`whole_pipeline.main()`）へ伝播する。後続のステップに渡す生データが一切無いため、この失敗のみは他の解析失敗（`UnknownElement`へのフォールバック等）とは異なり、フォールバックせず明確なエラーとして扱う（`whole_pipeline.main()`側で`SystemExit`へ正規化される。詳細は`doc/architecture/whole_pipeline.md`「3.4 エラー処理方針」参照）。

## 4. 構成要素リファレンス

各項目は先頭に`種別`（関数／データ構造／ヘルパークラス）を持つ。関数・ヘルパークラスを3.1節の依存関係図と同じ順（入口`process_pdf`を先頭に、そこから呼ばれる順を深さ優先でたどる順）に並べ、その後にデータ構造（`MinerUOutput`と9個の中間表現dataclass）を依存関係図での登場順にまとめて記載する。各項目の「グループ」は2節の7グループ（中間表現／テキストユーティリティ／キャッシュ／本体／構造解析／文書組み立て／入口）のどれに属するかを示す。`_normalize_math_text`が呼ぶテキストユーティリティ群・`_apply_outside_math_spans`・`split_sentences`は3.1節の図に複数回現れるが、項目は最初の登場箇所に1つだけ置く。`MinerURunError`・`MinerUVersionError`はいずれも本体が`pass`のみの単純な例外クラスのため、独立した項目は設けず、送出する関数（`run_mineru`・`get_mineru_version`）の項目内で言及するに留める。

### process_pdf

- **種別**: 関数
- **グループ**: 入口
- **呼び出し元**: `mainCode/whole_pipeline/whole_pipeline.py`の`main()`（工程(1)`resolve_page_range`の直後）。同ファイルの`main()`（CLIエントリポイント）からも呼ばれる。
- **入力**: `pdf_path`・`output_dir`・`start_page`/`end_page`（1始まり、省略時はPDF全体）・`range_label`（cache/フォルダ命名用、省略可）・`mineru_backend`（`"pipeline"`/`"vlm-engine"`、既定`"pipeline"`）
- **出力**: 生成されたページ別Markdownファイルパスのリスト（`page_XX_en.md`、ページ順。副作用として`images/*.png`も出力）
- **処理内容**: 「MinerU実行（`run_mineru`）→構造解析（`analyze_structure`）→成果物結合（`build_document`）」をこの順に直接呼ぶだけの薄いオーケストレーター。まずfitzでPDFの総ページ数を取得してページ範囲を検証し、範囲指定時はMinerUへ0始まりの絶対ページ範囲を渡す。MinerUの`items`が返す`page_idx`は指定範囲内での相対値（先頭が常に0）になるため、絶対ページ番号へ戻すための`page_offset`を`analyze_structure`へ渡す。詳細は3節参照。
- **テスト対象**: `test_process_pdf_rejects_page_range_beyond_total_pages`で不正なページ範囲が`ValueError`になることを直接検証している。それ以外は`conftest.py`のセッション共有フィクスチャ（sample0〜3.pdfそれぞれに対する`process_pdf`呼び出し）を通じて`testCode/test_stage2.py`の構造解析系テスト群から間接的に、全体テスト（`testCode/test_whole_pipeline.py`）からエンドツーエンドで検証されている。

### run_mineru

- **種別**: 関数
- **グループ**: 本体
- **呼び出し元**: `process_pdf()`（範囲指定の有無で引数が変わるのみ）
- **入力**: `pdf_path`・`work_dir`（MinerU生出力の作業ディレクトリ、通常は一時ディレクトリ）・`start_page`/`end_page`（0始まり・両端含む、Noneなら先頭/末尾）・`range_label`（cache/フォルダ命名用、省略可）・`backend`（`SUPPORTED_MINERU_BACKENDS`のいずれか、既定`"pipeline"`）
- **出力**: 呼び出し元へは`MinerUOutput`（content_list要素列＋画像パス解決の基準ディレクトリ）を返す。ディスク上では、`content_list.json`と`images/`をMinerU生出力から取り込み、`save_cache`が指紋`meta.json`を添えて、キャッシュフォルダ（`cache/<実行識別子>/mineru_cache[_<backend>]/`）に**フォルダ構造**としてまとめて置く。中身は次の3つ:
    - `content_list.json` … ページ横断のフラットな要素列（`MinerUOutput.items`の中身。プロジェクトが実際に読むMinerU生出力）
    - `images/` … 図表・数式の切り出し画像を収めたフォルダ
    - `meta.json` … このキャッシュがどんな条件で作られたかの指紋（PDFの内容ハッシュ・ページ範囲・MinerUバージョン・バックエンド・キャッシュ形式バージョン）。MinerUの出力ではなく`save_cache`が添えるデータで、`load_cached_items`が「このキャッシュを再利用してよいか」の判定に読む
- **処理内容**: `backend`が非対応なら`ValueError`。まず`load_cached_items`でキャッシュを引き、命中すればMinerU実行自体をスキップして返す。不命中なら`python -m mineru.cli.client`をサブプロセス実行し（`pipeline`のみ`--method auto`を付与）、`work_dir/<stem>/*/<stem>_content_list.json`をglobで探して読み、`save_cache`で保存してから返す。プロセス異常終了・出力ファイル未検出はいずれも`MinerURunError`（後続へ渡す生データが無いためフォールバックしない）。ページ範囲指定時、返る`items`の`page_idx`は範囲内相対値（先頭0）になる。
- **テスト対象**: `TestMinerURunner`が担当する。`test_run_mineru_invokes_subprocess_and_parses_content_list`で基本動作、`test_run_mineru_rejects_unsupported_backend`で`ValueError`、`test_run_mineru_pipeline_backend_passes_method_auto`/`test_run_mineru_vlm_engine_backend_omits_method_and_finds_backend_specific_subfolder`でbackend別のコマンド組み立てとサブフォルダ探索、`test_run_mineru_uses_cache_on_second_call`/`test_run_mineru_cache_invalidated_by_different_page_range`/`test_run_mineru_reruns_after_version_change`でキャッシュ連携、`test_run_mineru_raises_when_subprocess_fails`/`test_run_mineru_raises_when_output_file_missing`で`MinerURunError`を確認している。

### load_cached_items

- **種別**: 関数
- **グループ**: キャッシュ
- **呼び出し元**: `run_mineru()`
- **入力**: `pdf_path`・`start_page`/`end_page`・`range_label`（フォルダ名組み立て用）・`backend`
- **出力**: `(items, images_base)`のタプル、またはキャッシュが無い・古い・壊れている場合は`None`
- **処理内容**: 「今から`run_mineru`が実行しようとしている条件で、以前作ったMinerU出力をそのまま使い回せるか」を判定する関数。キャッシュフォルダ内の`meta.json`（そのキャッシュがどんな条件で作られたかの指紋。MinerUの出力ではなく`save_cache`が添えるデータ）を読み、そこに記録された条件 ― PDFの内容ハッシュ・ページ範囲・MinerUバージョン・バックエンド（`backend`キーを持たない旧形式のキャッシュは`pipeline`実行分として突き合わせる）・キャッシュ形式バージョン ― が現在の要求と全一致すれば、隣の`content_list.json`を読んで`(items, cache_dir)`を返す（＝`run_mineru`はMinerU実行をスキップできる。この`cache_dir`が画像パス解決の基準ディレクトリ＝`images_base`になる）。1つでも食い違う・キャッシュが無い・ファイルが壊れている、のいずれでも`None`を返し、`run_mineru`に通常のMinerU実行をさせる。
- **テスト対象**: `TestMinerUCache`が担当する。`test_load_returns_none_when_no_cache`/`test_save_then_load_roundtrip`で往復、`test_cache_isolated_by_page_range`/`test_cache_isolated_by_backend`で条件別の分離、`test_load_returns_none_when_pdf_content_changes`/`test_load_returns_none_when_mineru_version_changes`/`test_load_returns_none_on_corrupted_content_list_json`で無効化、`test_cache_disabled_via_env_var`で`MINERU_CACHE_DISABLE=1`による無効化を確認している。

### _cache_enabled

- **種別**: 関数
- **グループ**: キャッシュ
- **呼び出し元**: `load_cached_items()`・`save_cache()`
- **入力**: なし
- **出力**: `bool`（環境変数`MINERU_CACHE_DISABLE`が`"1"`でなければ`True`）
- **処理内容**: 呼び出しのたびに`os.environ`を読む（モジュール読み込み時に固定するとテストの`monkeypatch.setenv`が反映されないため）。
- **テスト対象**: `test_cache_disabled_via_env_var`（`TestMinerUCache`）で、`MINERU_CACHE_DISABLE=1`のときキャッシュの読み書きが行われないことを確認している。

### _cache_dir

- **種別**: 関数
- **グループ**: キャッシュ
- **呼び出し元**: `load_cached_items()`・`save_cache()`
- **入力**: `pdf_path`・`start_page`/`end_page`・`range_label`・`backend`
- **出力**: `cache/<run_id>/<subdir>`という`Path`（MinerUキャッシュの格納先ディレクトリ）
- **処理内容**: `_CACHE_ROOT`（リポジトリルート直下の`cache/`）に、`_run_id`が返す実行識別子と`_mineru_cache_subdir_name`が返すサブフォルダ名（既定backendは`mineru_cache`、それ以外は`mineru_cache_<backend>`）を連結する。
- **テスト対象**: 専用の直接テストは無い。`TestMinerUCache`の`test_cache_dir_name_differs_by_backend`（サブフォルダ名がbackendで変わること）・`test_cache_isolated_by_page_range`経由で間接的に検証される。

### _run_id

- **種別**: 関数
- **グループ**: キャッシュ
- **呼び出し元**: `_cache_dir()`
- **入力**: `pdf_path`・`start_page`/`end_page`・`range_label`（省略可）
- **出力**: `cache/`直下のフォルダ名に使う実行識別子文字列
- **処理内容**: `range_label`があれば`<stem>_<range_label>`（例:`sample3_label55-60`）、無ければページ範囲未指定なら`<stem>`、指定ありなら`<stem>_p<開始>_<終了>`。`range_label`は人間可読なフォルダ名のためだけで、キャッシュの正当性判定は`meta.json`の`start_page`/`end_page`で行う。
- **テスト対象**: 専用の直接テストは無い。`TestMinerUCache`のキャッシュ分離テスト経由で間接的に検証される。

### _mineru_cache_subdir_name

- **種別**: 関数
- **グループ**: キャッシュ
- **呼び出し元**: `_cache_dir()`
- **入力**: `backend`
- **出力**: MinerU実行結果を格納するサブフォルダ名
- **処理内容**: 既定backend（`"pipeline"`）は`"mineru_cache"`、それ以外は`"mineru_cache_<backend>"`。backendが異なればcontent_listの形式・認識精度も異なるため同じフォルダで混在させない。
- **テスト対象**: `test_cache_dir_name_differs_by_backend`（`TestMinerUCache`）で、`"pipeline"`と`"vlm-engine"`で別サブフォルダになることを確認している。

### _compute_pdf_hash

- **種別**: 関数
- **グループ**: キャッシュ
- **呼び出し元**: `load_cached_items()`・`save_cache()`
- **入力**: `pdf_path`
- **出力**: PDFファイル内容のSHA-256十六進文字列
- **処理内容**: PDFを1MiBずつ読みながらSHA-256を計算する。キャッシュキーの一部として、PDFの中身が変わったら自動でキャッシュが無効化されるようにするためのもの。
- **テスト対象**: 専用の直接テストは無い。`test_load_returns_none_when_pdf_content_changes`・`test_save_is_noop_when_pdf_hash_fails`（`TestMinerUCache`）経由で間接的に検証される。

### get_mineru_version

- **種別**: 関数
- **グループ**: キャッシュ
- **呼び出し元**: `load_cached_items()`・`save_cache()`
- **入力**: なし
- **出力**: インストール済み`mineru`パッケージのバージョン文字列
- **処理内容**: `importlib.metadata.version("mineru")`を返す。パッケージが見つからない場合は`MinerUVersionError`を送出し、キャッシュを無効化する（別バージョンのキャッシュ使い回しを防ぐ）。
- **テスト対象**: `test_get_mineru_version_raises_on_missing_package`（`TestMinerUCache`）で`MinerUVersionError`を直接確認。`test_load_returns_none_when_mineru_version_changes`・`test_run_mineru_reruns_after_version_change`でバージョン変化時の無効化を確認している。

### save_cache

- **種別**: 関数
- **グループ**: キャッシュ
- **呼び出し元**: `run_mineru()`（キャッシュ不命中でMinerUを実行した後）
- **入力**: `pdf_path`・`start_page`/`end_page`・`items`・`images_base`・`range_label`・`backend`
- **出力**: なし（副作用としてキャッシュディレクトリを作成）
- **処理内容**: `_cache_enabled`が偽なら何もしない。一時ディレクトリへ`content_list.json`・`images/`サブフォルダのみ・`meta.json`（スキーマ版・pdf_sha256・mineru_version・start/end・backend）を書き、`rename`でアトミックに差し替える。`OSError`（パス長超過等）はMinerU実行自体の成功結果に影響させず握りつぶす。
- **テスト対象**: `TestMinerUCache`の`test_save_then_load_roundtrip`・`test_cache_isolated_by_*`で正常保存、`test_save_is_noop_when_pdf_hash_fails`でハッシュ計算失敗時に何も書かないことを確認している。

### analyze_structure

- **種別**: 関数
- **グループ**: 構造解析
- **呼び出し元**: `process_pdf()`（`run_mineru`の直後）
- **入力**: `items`（`MinerUOutput.items`）・`images_base`（`img_path`解決の基準ディレクトリ）・`page_offset`（`--start`でPDF途中から処理した場合の絶対ページオフセット、0始まり、既定0）
- **出力**: `StructuredDocument`（ページ別要素列）
- **処理内容**: MinerU の生JSON（`items`：本文も数式も図表キャプションも種別だけ付いてフラットに並んだ、意味解釈のないブロック列）を、下流の翻訳パイプラインが扱える形へ整える。各要素を「翻訳する文（本文・キャプション）」と「翻訳しない要素（数式・図表画像・著者名・見出し）」に仕分け（ヘッダー・フッター・ノンブル等の紙面ノイズは要素にせず捨てる）、本文・キャプションは文単位に分割し、翻訳する各文に一意で安定したID（`[P1-S4-1.introduction-S1]` 等）を付与する。画像・数式は MinerU の相対パス `img_path` を `images_base` 基準で解決して要素に持たせる。実装は2ステップで、`_build_pages` が `page_idx` ごとに要素を種別判定して中間表現（`TextBlockElement` 等）へ振り分けページ単位の `PageContent` を作り、`_assign_sentence_ids` がその本文・キャプション文へ表示用IDを付ける。結果は `StructuredDocument`（ページ別の要素列を持つ Python オブジェクト）として返す。
- **テスト対象**: `TestAnalyzeStructure`（前付け・番号付き/無し見出し・図表キャプション分離・ノイズ除外・脚注の翻訳対象化・`UnknownElement`フォールバック・数式のlatex保持/空要素破棄・リスト項目・キャプションのbackfill等）、`TestAnalyzeStructureRealDataSmoke`（`cache/`凍結のMinerU実データを通すスモーク、parametrize）、`TestStructureOnRealSamples`（`processed`fixture経由の実データ検証）が担当する。

### _build_pages

- **種別**: 関数
- **グループ**: 構造解析
- **呼び出し元**: `analyze_structure()`
- **入力**: `items`・`images_base`・`page_offset`
- **出力**: `PageContent`のリスト（ページ番号順）
- **処理内容**: `items`を走査し、`page_idx`ごとに`_PageBuilder`を用意する。`type`が`_NOISE_TYPES`（`aside_text`/`header`/`footer`/`page_number`）なら捨てる。それ以外は`type`に応じて`_handle_image_or_table_item`（image/table/chart）・`_handle_equation_item`・`_handle_list_item`・`_handle_text_item`（text/page_footnote）・`_handle_unknown_item`（未知種別）へ振り分け、いずれかの処理中に例外が出ても`_handle_unknown_item`へフォールバックして全体を止めない。`page_number = page_idx + 1 + page_offset`。
- **テスト対象**: 専用の直接テストは無い。`analyze_structure`経由（`TestAnalyzeStructure`のノイズ除外・フォールバック系テスト等）で検証される。

### _PageBuilder

- **種別**: ヘルパークラス
- **グループ**: 構造解析
- **役割**: 1ページ分の要素列を組み立てる間の作りかけ状態（要素列・前付けラベルの割当済み数・未ラベル図表の連番）を1箇所に集約する。`_build_pages`のハンドラ関数群の間で個別の引数として引き回さずに済ませるためのクラス。`has_heading()`で`HeadingElement`を追加済みか（前付けラベル打ち切り判定用）を返す。
- **状態**:

| フィールド | 型 | 初期値 | 意味・可変性 |
|---|---|---|---|
| `page_idx` | `int` | コンストラクタ引数 | 0始まりの相対ページ番号。`__init__`後は不変 |
| `page_offset` | `int` | `0` | 絶対ページ番号復元用オフセット。`__init__`後は不変 |
| `elements` | `list` | `[]` | そのページの要素dataclass（`TextBlockElement`等）の蓄積先。ハンドラが順次append |
| `front_matter_count` | `int` | `0` | 割当済みの前付けラベル数。加算のみ |
| `unlabeled_seq` | `int` | `0` | 番号読み取り不可の図表・数式のフォールバック連番。加算のみ |

- **メソッド**:

| メソッド | 返り値 | 概要 | 状態変更 |
|---|---|---|---|
| `has_heading()` | `bool` | `elements`に`HeadingElement`が既に含まれるか（前付けラベル打ち切り判定用） | 参照のみ |

- **ライフサイクル**:
    - **生成**: `_build_pages()`が要素ループ内で`builders.setdefault(page_idx, _PageBuilder(page_idx, page_offset))`により、`page_idx`ごとに1インスタンス生成して`dict`に保持する。
    - **更新**: 各`_handle_*`ハンドラ（`_handle_list_item`・`_handle_image_or_table_item`・`_handle_equation_item`・`_handle_text_item`・`_handle_unknown_item`）が`elements`へ要素をappend。`_handle_text_item`が`front_matter_count`を、`_handle_image_or_table_item`・`_handle_equation_item`が`unlabeled_seq`を加算する。
    - **消費**: `_build_pages()`末尾で`PageContent(page_number=…, elements=b.elements)`として`elements`を取り出し、インスタンスは破棄される。
- **テスト対象**: 専用の直接テストは無い。`analyze_structure`経由（前付けラベル・未ラベル連番を検証する`TestAnalyzeStructure`のテスト）で間接的に検証される。

### _handle_list_item

- **種別**: 関数
- **グループ**: 構造解析
- **呼び出し元**: `_build_pages()`（`type == "list"`のとき）
- **入力**: `item`（MinerU要素）・`builder`（`_PageBuilder`）
- **出力**: なし（副作用として`builder.elements`へ`TextBlockElement`を追加）
- **処理内容**: `list_items`の各項目を`_normalize_math_text`にかけ、1項目＝1文として`TextBlockElement`にまとめる。MinerUが既に項目単位に分割済みのため独自の文分割（`split_sentences`）はかけない（参考文献のように文中にピリオドを含む短い項目で誤分割しやすいため）。
- **テスト対象**: `test_analyze_structure_handles_list_items_as_individual_sentences`（`TestAnalyzeStructure`）で、リスト項目が個別の文として扱われることを確認している。

### _normalize_math_text

- **種別**: 関数
- **グループ**: 構造解析
- **呼び出し元**: `_handle_list_item()`・`_handle_image_or_table_item()`・`_handle_text_item()`（本文・キャプション由来の生テキストごと）
- **入力**: `text`
- **出力**: 未保護の数式的表現を`$...$`保護し、結合語ハイフンを復元した文字列
- **処理内容**: `restore_merged_hyphens(wrap_bare_greek_letters(wrap_bare_letter_equals_expressions(text)))`。「1文字の変数 = 値」→裸のギリシャ文字→結合語ハイフンの順に適用する（この順で、先の2関数が挿入する`$`が`restore_merged_hyphens`の数式スパン除外判定へ正しく反映される）。PDF/MinerUの知識を持たないテキストユーティリティ群への橋渡し役。
- **テスト対象**: `test_analyze_structure_auto_protects_bare_greek_and_letter_equals_expressions`（`TestAnalyzeStructure`）・`test_inline_math_is_wrapped_in_dollar_signs`（`TestStructureOnRealSamples`、実データ）で、地の文の裸のギリシャ文字・`t = 1`形式が`$...$`で保護されることを確認している。

### wrap_bare_letter_equals_expressions

- **種別**: 関数
- **グループ**: テキストユーティリティ
- **呼び出し元**: `_normalize_math_text()`（最初に適用）
- **入力**: `text`
- **出力**: `$...$`で保護されていない「1文字の変数 = 値」形式（例:`t = 1`）を`$...$`に包んだ文字列
- **処理内容**: `_BARE_LETTER_EQUALS_RE`（`\b[a-zA-Z]\s*=\s*[a-zA-Z0-9]+\b`）にマッチする箇所を、`_apply_outside_math_spans`で既存の数式スパンを避けながら`$...$`で囲む。`a = b`が実在の英文として自然に使われることは無いため自動保護してよい（この翻訳前保護により工程(6)はこの形を扱わない）。
- **テスト対象**: `test_wrap_bare_letter_equals_expressions`（`TestSentenceAndTextUtils`、parametrize）で保護対象・非対象を直接確認している。

### run_repl（wrap_bare_letter_equals_expressions内）

- **種別**: 関数
- **グループ**: テキストユーティリティ
- **呼び出し元**: `wrap_bare_letter_equals_expressions()`内部のみ（`_apply_outside_math_spans`へ`repl`引数として渡され、マッチごとに呼ばれるネスト関数）
- **入力**: `m`（`re.Match`。`_BARE_LETTER_EQUALS_RE`にマッチした「1文字の変数 = 値」形式）
- **出力**: マッチ文字列全体を`$...$`で囲んだ文字列（`f"${m.group(0)}$"`）
- **処理内容**: マッチ箇所をそのまま`$...$`で包むだけの置換コールバック。
- **テスト対象**: 専用の直接テストは無い。親`wrap_bare_letter_equals_expressions`経由（`test_wrap_bare_letter_equals_expressions`）で検証される。

### _apply_outside_math_spans

- **種別**: 関数
- **グループ**: テキストユーティリティ
- **呼び出し元**: `wrap_bare_letter_equals_expressions()`・`wrap_bare_greek_letters()`・`restore_merged_hyphens()`
- **入力**: `text`・`pattern`（`re.Pattern`）・`repl`（置換関数）
- **出力**: `$...$`で囲まれた数式スパンを除いた部分にだけ`pattern.sub(repl, ...)`を適用した文字列
- **処理内容**: `_INLINE_MATH_RE`（`\$[^$]*\$`）で数式スパンを走査し、スパンの外側の断片にのみ置換を適用してスパン自体はそのまま連結する。上記3変換が共有する「既存スパンには手を触れず地の文だけ置換する」構造をまとめた共通ヘルパー。
- **テスト対象**: 専用の直接テストは無い。`test_wrap_bare_greek_letters`・`test_wrap_bare_letter_equals_expressions`・`test_restore_merged_hyphens`（`TestSentenceAndTextUtils`）が「既存の`$...$`を二重に包まない/壊さない」ケースを通じて間接的に検証する。

### wrap_bare_greek_letters

- **種別**: 関数
- **グループ**: テキストユーティリティ
- **呼び出し元**: `_normalize_math_text()`（2番目に適用）
- **入力**: `text`
- **出力**: 保護されていないギリシャ文字をTeXコマンド形式（例:`γ`→`\gamma`）に変換して`$...$`に包んだ文字列
- **処理内容**: `_GREEK_RUN_RE`で連続するギリシャ文字を捉え、`_GREEK_TO_TEX_COMMAND`（KaTeX 0.16.11の記号定義から抽出したUnicode→コマンド名の対応表）で各文字をコマンドへ変換して連結し（対応表に無い大文字は元の文字のまま）、`$...$`で囲む。英語の地の文にギリシャ文字が実在の単語として使われることは無いため自動保護してよい。
- **テスト対象**: `test_wrap_bare_greek_letters`（`TestSentenceAndTextUtils`、parametrize）で単体・連続・既存スパン内非対象を直接確認している。

### run_repl（wrap_bare_greek_letters内）

- **種別**: 関数
- **グループ**: テキストユーティリティ
- **呼び出し元**: `wrap_bare_greek_letters()`内部のみ（`_apply_outside_math_spans`へ`repl`引数として渡されるネスト関数）
- **入力**: `m`（`re.Match`。連続するギリシャ文字の並び）
- **出力**: 各文字を`char_repl`でTeXコマンドへ変換して連結し、`$...$`で囲んだ文字列
- **処理内容**: マッチしたギリシャ文字の並びに`_GREEK_LETTER_RE.sub(char_repl, ...)`を適用して1文字ずつコマンド化してから`$...$`で包む置換コールバック。
- **テスト対象**: 専用の直接テストは無い。親`wrap_bare_greek_letters`経由（`test_wrap_bare_greek_letters`）で検証される。

### char_repl（wrap_bare_greek_letters内）

- **種別**: 関数
- **グループ**: テキストユーティリティ
- **呼び出し元**: `wrap_bare_greek_letters()`内部の`run_repl`から（`_GREEK_LETTER_RE.sub`のコールバックとして1文字ごとに呼ばれるネスト関数）
- **入力**: `m`（`re.Match`。ギリシャ文字1文字）
- **出力**: `_GREEK_TO_TEX_COMMAND`に有ればTeXコマンド文字列（例:`\gamma`）、無ければ元の文字のまま
- **処理内容**: 1文字のUnicodeギリシャ文字をKaTeX互換のTeXコマンド名へ引くだけの置換コールバック。対応表に無い文字（一部の大文字）は素通しする。
- **テスト対象**: 専用の直接テストは無い。親`wrap_bare_greek_letters`経由（`test_wrap_bare_greek_letters`）で検証される。

### restore_merged_hyphens

- **種別**: 関数
- **グループ**: テキストユーティリティ
- **呼び出し元**: `_normalize_math_text()`（最後に適用）
- **入力**: `text`
- **出力**: MinerUが改行時に落としたハイフンを復元した文字列（例:`dataefficient`→`data-efficient`）
- **処理内容**: `_MERGED_WORD_RE`（`\b[a-z]{6,}\b`、小文字のみに限定し固有名詞・モデル名の誤爆を避ける）にマッチする語を`split_merged_compound`にかけ、分割できたものだけハイフンを戻す。`_apply_outside_math_spans`で数式スパン内のLaTeXコマンド（`\mathcal`等、英字6文字以上連続）を壊さないようにする。
- **テスト対象**: `test_restore_merged_hyphens`（`TestSentenceAndTextUtils`、parametrize）で復元対象・非対象を直接確認している。

### repl（restore_merged_hyphens内）

- **種別**: 関数
- **グループ**: テキストユーティリティ
- **呼び出し元**: `restore_merged_hyphens()`内部のみ（`_apply_outside_math_spans`へ`repl`引数として渡されるネスト関数）
- **入力**: `m`（`re.Match`。`_MERGED_WORD_RE`にマッチした小文字6文字以上の語）
- **出力**: `split_merged_compound`が分割できればハイフン入りの語、できなければ元の語のまま
- **処理内容**: マッチ語を`split_merged_compound`にかけ、戻り値が`None`でなければそれを、`None`なら原語を返す置換コールバック。
- **テスト対象**: 専用の直接テストは無い。親`restore_merged_hyphens`経由（`test_restore_merged_hyphens`）で検証される。

### split_merged_compound

- **種別**: 関数
- **グループ**: テキストユーティリティ
- **呼び出し元**: `restore_merged_hyphens()`内部のネスト関数`repl`（マッチ語ごと）
- **入力**: `word`
- **出力**: `左-右`形式の文字列、または分割できなければ`None`
- **処理内容**: `is_known_word`で辞書に有る語（実在の1語）は`None`を返して原形維持。無い語は、両側とも辞書に載る分割点を`MERGED_WORD_MIN_HALF_LEN`（3）文字以上の位置で左から探し、最初に見つかったものでハイフンを入れる。
- **テスト対象**: `test_split_merged_compound`（`TestSentenceAndTextUtils`、parametrize）で分割成功・実在語の非分割を直接確認している。

### is_known_word

- **種別**: 関数
- **グループ**: テキストユーティリティ
- **呼び出し元**: `split_merged_compound()`
- **入力**: `word`
- **出力**: `bool`（英単語として辞書`_SPELL`（`pyspellchecker`）に存在するか）
- **処理内容**: `word.lower() in _SPELL`。注意: `pyspellchecker`はアルファベット1文字を常に「既知」と判定するため、1文字の実在単語判定には使えない。
- **テスト対象**: `test_is_known_word_single_letter_quirk`（`TestSentenceAndTextUtils`）で、1文字が常にTrueになる既知の癖を明示的に確認している。

### _handle_image_or_table_item

- **種別**: 関数
- **グループ**: 構造解析
- **呼び出し元**: `_build_pages()`（`type`が`image`/`table`/`chart`のとき）
- **入力**: `item`・`item_type`・`images_base`・`builder`
- **出力**: なし（副作用として`builder.elements`へ`FigureElement`・`CaptionElement`を追加）
- **処理内容**: `image_caption`/`table_caption`/`chart_caption`を`_normalize_math_text`にかけ、`parse_caption_label`でFig./Table番号を持つキャプション群に整理する。番号付きキャプションを2件以上検出した場合は、直前に追加した未ラベル図へ遡ってキャプションを割り当て直す（レイアウト検出が隣接図表のキャプションを1ブロックへ誤結合するケースへの対応）。キャプション文の分割は`split_sentences`。番号が読めなければ`builder.unlabeled_seq`のフォールバック連番を振る。
- **テスト対象**: `test_analyze_structure_separates_figure_and_caption_with_number`・`test_analyze_structure_backfills_caption_to_previous_unlabeled_figure_on_split_caption_block`・`test_handle_image_or_table_item_backfill_does_not_touch_already_labeled_figure`・`test_analyze_structure_assigns_unlabeled_fallback_number_when_no_caption_found`（`TestAnalyzeStructure`）、`TestTableCaptionExtraction`（実データ）が担当する。

### parse_caption_label

- **種別**: 関数
- **グループ**: テキストユーティリティ
- **呼び出し元**: `_handle_image_or_table_item()`（キャプション文ごと）
- **入力**: `text`
- **出力**: `(種別, 番号)`のタプル（種別は`"figure"`/`"table"`）、キャプション形式でなければ`None`
- **処理内容**: `CAPTION_RE`（`^(fig(?:ure)?|table)\.?\s*(\d+)\s*[:.]`、大小無視）にマッチした場合のみ、先頭語で種別を、数字で番号を読む。
- **テスト対象**: `test_parse_caption_label`（`TestSentenceAndTextUtils`、parametrize）で各形式・非該当を直接確認している。

### split_sentences

- **種別**: 関数
- **グループ**: テキストユーティリティ
- **呼び出し元**: `_handle_image_or_table_item()`（キャプション）・`_handle_text_item()`（本文段落）
- **入力**: `text`（1段落分程度の英文）
- **出力**: 文単位に分割した文字列のリスト（空文字列は除外）
- **処理内容**: `_SENTENCE_BOUNDARY_RE`で`.!?`＋空白＋大文字/数字/引用開始という境界を探し、直前トークンが`_ABBREVIATIONS`（`e.g.`/`et al.`/`Fig.`等）または`_INITIAL_RE`（`A.`のようなイニシャル）なら分割しない。
- **テスト対象**: `test_split_sentences_handles_abbreviations`（`TestSentenceAndTextUtils`、parametrize）で略語による誤分割の抑制を直接確認している。

### _handle_equation_item

- **種別**: 関数
- **グループ**: 構造解析
- **呼び出し元**: `_build_pages()`（`type == "equation"`のとき）
- **入力**: `item`・`images_base`・`builder`
- **出力**: なし（副作用として`builder.elements`へ`EquationElement`を追加）
- **処理内容**: `text`から前後の`$$`を剥がし、空なら要素ごと捨てる。`EQUATION_TAG_RE`（`\\tag\{(\d+)\}`）が見つかればそれを元論文の式番号として使い（ページをまたいでもリセットしない）、無ければ`builder.unlabeled_seq`のフォールバック連番。`img_path`が無いbackend（vlm-engine等）では`image_path=None`のままにする（最終PDFの描画は常にlatex側＝KaTeX）。
- **テスト対象**: `test_analyze_structure_equation_without_img_path_keeps_latex`・`test_analyze_structure_equation_with_empty_text_and_no_img_path_is_dropped`（`TestAnalyzeStructure`）、`test_all_three_equations_are_detected_with_latex`（`TestStructureOnRealSamples`）が担当する。

### _handle_text_item

- **種別**: 関数
- **グループ**: 構造解析
- **呼び出し元**: `_build_pages()`（`type`が`text`/`page_footnote`のとき）
- **入力**: `item`・`builder`・`unnumbered_heading_seq`（文書全体で共有する番号無し見出し用の連番カウンタ。1要素のリストに包んだ可変整数）
- **出力**: なし（副作用として`builder.elements`へ`LabeledElement`/`HeadingElement`/`TextBlockElement`のいずれかを追加）
- **処理内容**: `_normalize_math_text`後、PDF全体の1ページ目冒頭・最初の見出し出現前は前付け（`FRONT_MATTER_LABELS`＝TITLE/AUTHORS/AFFIL）として`LabeledElement`。`text_level`が1/2のブロックは見出しとして、`HEADING_RE`が合えば章番号・章名を、合わなければ`unnumbered_heading_seq`から合成章番号（`u1`,`u2`,…）を振って`HeadingElement`（ただし`ABSTRACT`という見出しだけは本文1文として扱いID体系の一貫性を保つ）。それ以外は`split_sentences`して`TextBlockElement`。
- **テスト対象**: `test_analyze_structure_assigns_front_matter_and_numbered_heading_labels`・`test_analyze_structure_assigns_synthetic_ids_to_unnumbered_headings`・`test_analyze_structure_treats_page_footnote_as_translatable_text`（`TestAnalyzeStructure`）、`test_unnumbered_headings_in_real_book_pdf`（`TestTableImagesAndBookHeadings`）が担当する。

### slugify_section_name

- **種別**: 関数
- **グループ**: テキストユーティリティ
- **呼び出し元**: `_handle_text_item()`（見出しの章名スラッグ生成）
- **入力**: `title_text`
- **出力**: 章ラベル用の短い英字スラッグ（例:`Preliminaries`→`preliminaries`）
- **処理内容**: 先頭の連続英字を`re.match`で取り出して小文字化する。無ければ`"section"`。
- **テスト対象**: `test_heading_regex_and_slugify`（`TestSentenceAndTextUtils`、parametrize）で`HEADING_RE`のマッチと合わせて直接確認している。

### _handle_unknown_item

- **種別**: 関数
- **グループ**: 構造解析
- **呼び出し元**: `_build_pages()`（未対応の`type`、または既知種別の処理中に例外が出た場合）
- **入力**: `item`・`item_type`・`builder`・`reason`（フォールバック理由、省略可）
- **出力**: なし（副作用として`builder.elements`へ`UnknownElement`を追加）
- **処理内容**: `_extract_raw_text`でベストエフォートの生テキストを取り、`logger.warning`を出して`UnknownElement`（raw_type・text・reason）を追加する。1要素の解釈失敗でドキュメント全体を止めないためのフォールバック。
- **テスト対象**: `test_analyze_structure_falls_back_to_unknown_element_on_error`（`TestAnalyzeStructure`）で例外時のフォールバック、`test_build_document_numbers_duplicate_unknown_elements_on_same_page`（`TestBuildDocument`）で同一ページ複数UnknownのID採番、`test_no_unexpected_unknown_elements_in_sample*`（`TestUnknownElementsAndSentenceNumbering`）で実データに想定外のUnknownが出ないことを確認している。

### _extract_raw_text

- **種別**: 関数
- **グループ**: 構造解析
- **呼び出し元**: `_handle_unknown_item()`
- **入力**: `item`
- **出力**: 要素からベストエフォートで取り出したテキスト。何も無ければ空文字列
- **処理内容**: `_TEXT_FIELD_CANDIDATES`（`text`/`table_body`/`code_body`）→`_LIST_FIELD_CANDIDATES`（`list_items`）→`_CAPTION_FIELD_CANDIDATES`の順にフィールドを試し、最初に見つかった非空の内容を返す。未知の種別でも中身を拾えるようにするため複数試す。
- **テスト対象**: 専用の直接テストは無い。`_handle_unknown_item`経由（`test_analyze_structure_falls_back_to_unknown_element_on_error`等）で間接的に検証される。

### _assign_sentence_ids

- **種別**: 関数
- **グループ**: 構造解析
- **呼び出し元**: `analyze_structure()`（`_build_pages`の直後）
- **入力**: `pages`（`PageContent`のリスト、書き換え対象）
- **出力**: なし（副作用として`TextBlockElement`/`CaptionElement`の`sentence_ids`を設定）
- **処理内容**: 本文文には`[P{page}-S{ページ内通し}-{章ラベル}-S{章内通し}]`を付ける。ページ内通し番号は各ページ先頭でリセット、章内通し番号は`HeadingElement`が変わった時のみリセット（同じ章が次ページに続く場合は継続）。最初の章番号付き見出しが現れるまでは`ABSTRACT_SECTION_ID`（`abstract`）を章ラベルに使う。キャプション文には`[P{page}-{FIG|TABLE}{n}-CAPTION-S{通し}]`。タイトル・著者・図・見出し自体はカウントに含めない。
- **テスト対象**: `test_sentence_ids_are_sequential_per_page`（`TestUnknownElementsAndSentenceNumbering`）・`test_extract_and_number_sentences`（`TestStructureOnRealSamples`）でページ内・章内の通し番号の付き方を確認している。

### build_document

- **種別**: 関数
- **グループ**: 文書組み立て
- **呼び出し元**: `process_pdf()`（`analyze_structure`の直後、工程(2)の最終ステップ）
- **入力**: `doc`（`StructuredDocument`）・`output_dir`・`first_page`/`last_page`（1始まり・両端含む、出力対象のページ範囲）
- **出力**: 生成したMarkdownファイルパスのリスト（ページ順）
- **処理内容**: `output_dir/images/`を作り、`first_page`〜`last_page`の全ページ（要素が1つも無いページも空の`PageContent`として）について`_render_page_markdown`でMarkdownを組み立て、`page_{n:02d}_en.md`として書き出す。
- **テスト対象**: `TestBuildDocument`が担当する。`test_build_document_writes_markdown_and_saves_figure_image`で本文・画像行の書き出しとPNG保存、`test_build_document_preserves_multiline_text_and_reparses_without_loss`で複数行テキストが工程(3)で再解析しても失われないこと、`test_build_document_numbers_duplicate_unknown_elements_on_same_page`で同一ページの重複Unknownの連番、`test_build_document_equation_without_image_omits_image_line_but_keeps_latex`で画像なし数式の出力を確認している。

### _render_page_markdown

- **種別**: 関数
- **グループ**: 文書組み立て
- **呼び出し元**: `build_document()`（ページごと）
- **入力**: `page`（`PageContent`）・`images_dir`
- **出力**: 1ページ分のMarkdownテキスト（画像の保存も行う）
- **処理内容**: 要素の型ごとに行を出し分ける。`FigureElement`/`EquationElement`は`_save_element_image`でPNGを保存し`![id](path) [id]`行（数式は続けて`[id-LATEX] $$...$$`行、`image_path`が`None`なら画像行を省略）、`LabeledElement`/`HeadingElement`は`[id] text`、`CaptionElement`/`TextBlockElement`は文ごとに`[sent_id] sentence`。同一ページに同じ`raw_type`の`UnknownElement`が複数あるとタグが重複するため、ページ内連番`unknown_seq`でタグを一意化する。
- **テスト対象**: 専用の直接テストは無い。`build_document`経由（`test_build_document_numbers_duplicate_unknown_elements_on_same_page`が`unknown_seq`分岐を、他の`TestBuildDocument`が各要素の行形式を）で検証される。

### _save_element_image

- **種別**: 関数
- **グループ**: 文書組み立て
- **呼び出し元**: `_render_page_markdown()`（`FigureElement`/`EquationElement`ごと）
- **入力**: `element`・`images_dir`・`page_number`
- **出力**: Markdownから参照する相対パス（`images/<filename>`）
- **処理内容**: 種別（figure/table/eq）とラベル有無に応じたファイル名（例:`fig_p1_1.png`、未ラベルは`fig_p1_unlabeled1.png`）を決め、`PIL.Image`でRGB変換してPNG保存する。
- **テスト対象**: `test_build_document_writes_markdown_and_saves_figure_image`（`TestBuildDocument`）・`test_images_are_extracted_as_png`（`TestStructureOnRealSamples`）で、画像が実際にPNGとして出力されることを確認している。

### _figure_label

- **種別**: 関数
- **グループ**: 文書組み立て
- **呼び出し元**: `_render_page_markdown()`（`FigureElement`/`EquationElement`ごと）
- **入力**: `fig_kind`（`"figure"`/`"table"`/`"equation"`）・`number`・`labeled`
- **出力**: ラベル文字列（例:`FIG3`、未ラベルは`FIG-UNLABELED3`）
- **処理内容**: 種別ごとの接頭辞（FIG/TABLE/EQ）に番号を付けるだけ。`labeled`が偽なら`-UNLABELED`を挟む。
- **テスト対象**: 専用の直接テストは無い。`_render_page_markdown`経由（`TestBuildDocument`）でタグ文字列の一部として検証される。

### main

- **種別**: 関数
- **グループ**: 入口
- **呼び出し元**: なし（`process_pdf`単体の動作確認・デバッグ用の簡易CLIエントリポイント。`python mainCode/stage2/stage2.py <pdf> <output_dir>`として直接実行する）
- **入力**: なし（`sys.argv`をargparseで解釈。`pdf_path`/`output_dir`/`--start`/`--end`/`--mineru-backend`を受け取る）
- **出力**: なし（`process_pdf`が返すMarkdownファイルパス一覧を標準出力へ列挙する）
- **処理内容**: CLI引数を解析して`process_pdf`を呼び、結果のパス一覧を`print`するだけ。`mainCode/whole_pipeline/whole_pipeline.py`にも同名の`main()`（7工程すべてを統括する本番用の本体）があるが別の関数であり、互いに呼び合うことは無い（`whole_pipeline.main()`は`process_pdf`を直接呼ぶ）。
- **テスト対象**: 専用テストは無い（CLIレベルのエンドツーエンドテストは実施していない。`whole_pipeline.py`の`main()`と同様の扱い）。

### MinerUOutput

- **種別**: データ構造
- **グループ**: 本体
- **役割**: `run_mineru`の実行結果（キャッシュ命中/不命中によらず同じ型）を持ち回るdataclass。
- **フィールド**:

| フィールド | 型 | 役割 |
|---|---|---|
| `items` | `list[dict]` | `content_list.json`の中身（ページ横断のフラットな要素リスト） |
| `images_base` | `Path` | `items`中の`img_path`（相対パス）を解決するための基準ディレクトリ |

- **使われ方**: `run_mineru`が返し、`process_pdf`が`analyze_structure`へ`items`・`images_base`を渡す。ページ範囲指定時、`items`の`page_idx`は範囲内相対値（先頭0）。

### StructuredDocument

- **種別**: データ構造
- **グループ**: 中間表現
- **役割**: `analyze_structure`の出力。ページ別の要素列を保持する最上位の型。
- **フィールド**:

| フィールド | 型 | 役割 |
|---|---|---|
| `pages` | `list[PageContent]` | ページ別の要素列 |

- **使われ方**: `analyze_structure`が返し、`build_document`が`first_page`〜`last_page`の範囲で走査してMarkdownへ書き出す。工程(2)内で完結し、原文Markdown書き出し後は役目を終える。

### PageContent

- **種別**: データ構造
- **グループ**: 中間表現
- **役割**: 1ページ分の要素列（読み順）。
- **フィールド**:

| フィールド | 型 | 役割 |
|---|---|---|
| `page_number` | `int` | 物理ページ番号（1始まり、`page_offset`加算済み） |
| `elements` | `list` | そのページの要素（`TextBlockElement`等）を読み順に並べたリスト |

- **使われ方**: `_build_pages`が生成し、`_assign_sentence_ids`が`elements`内の文へIDを付け、`build_document`（要素の無いページは空の`PageContent`を用意）→`_render_page_markdown`がレンダリングする。

### TextBlockElement

- **種別**: データ構造
- **グループ**: 中間表現
- **役割**: 本文の段落（複数文に分割済み）。翻訳対象の本文テキスト。
- **フィールド**:

| フィールド | 型 | 役割 |
|---|---|---|
| `sentences` | `list[str]` | 分割済みの文 |
| `sentence_ids` | `list[str]` | 各文のID（例:`"P1-S3-1.introduction-S2"`）。`_assign_sentence_ids`が設定 |
| `kind` | `str`（既定`"text_block"`） | 要素種別の識別子 |

- **使われ方**: `_handle_list_item`・`_handle_text_item`が生成、`_assign_sentence_ids`が`sentence_ids`を設定、`_render_page_markdown`が`[sent_id] sentence`行として出力する。

### FigureElement

- **種別**: データ構造
- **グループ**: 中間表現
- **役割**: 図・表。翻訳対象外（画像そのものは翻訳できない。テキストは`CaptionElement`が担当）。
- **フィールド**:

| フィールド | 型 | 役割 |
|---|---|---|
| `image_path` | `Path` | MinerUが切り出した画像へのパス |
| `fig_kind` | `str`（既定`"figure"`） | `"figure"` \| `"table"` |
| `number` | `int \| None` | 元論文のFig./Table番号（キャプションから読み取る） |
| `labeled` | `bool`（既定`True`） | `False`なら`number`は読み取れなかったためのフォールバック連番 |
| `kind` | `str`（既定`"figure"`） | 要素種別の識別子 |

- **使われ方**: `_handle_image_or_table_item`が生成（キャプションのbackfillで`fig_kind`/`number`/`labeled`が後から書き換わることがある）、`_render_page_markdown`が`_save_element_image`でPNG保存し画像行を出力する。

### EquationElement

- **種別**: データ構造
- **グループ**: 中間表現
- **役割**: 数式。MinerUが生成した構文的に正しいLaTeXをそのまま保持する。翻訳対象外。
- **フィールド**:

| フィールド | 型 | 役割 |
|---|---|---|
| `latex` | `str` | 数式のLaTeXテキスト（前後の`$$`は剥がした状態） |
| `image_path` | `Path \| None` | 切り出し画像へのパス。vlm-engine等では`None`（描画は常にlatex側＝KaTeX） |
| `number` | `int \| None` | `\tag{n}`から読んだ式番号、または未ラベル時のフォールバック連番 |
| `labeled` | `bool`（既定`True`） | `False`なら`number`はフォールバック連番 |
| `kind` | `str`（既定`"equation"`） | 要素種別の識別子 |

- **使われ方**: `_handle_equation_item`が生成、`_render_page_markdown`が`image_path`があれば画像行を、常に`[id-LATEX] $$...$$`行を出力する。

### LabeledElement

- **種別**: データ構造
- **グループ**: 中間表現
- **役割**: タイトル・著者名・所属など、文としてナンバリングしない前付け要素。既定では翻訳対象外。
- **フィールド**:

| フィールド | 型 | 役割 |
|---|---|---|
| `text` | `str` | 前付けテキスト |
| `label` | `str` | `"TITLE"` \| `"AUTHORS"` \| `"AFFIL"` |
| `kind` | `str`（既定`"labeled"`） | 要素種別の識別子 |

- **使われ方**: `_handle_text_item`が、1ページ目冒頭・最初の見出し出現前のブロックに`FRONT_MATTER_LABELS`を出現順に割り当てて生成、`_render_page_markdown`が`[P{page}-{label}] text`行として出力する。

### HeadingElement

- **種別**: データ構造
- **グループ**: 中間表現
- **役割**: 章・節見出し。本文の文カウントには含めず単体ラベルとして扱う。翻訳対象外。
- **フィールド**:

| フィールド | 型 | 役割 |
|---|---|---|
| `text` | `str` | 見出しテキスト |
| `section_num` | `str` | 章番号（例:`"1"`、`"2.1"`）。番号無し見出しは合成番号（`"u1"`等） |
| `section_name` | `str` | 章名スラッグ（例:`"introduction"`。`slugify_section_name`が生成） |
| `kind` | `str`（既定`"heading"`） | 要素種別の識別子 |

`section_id`プロパティが`{section_num}.{section_name}`を返し、`_assign_sentence_ids`の章ラベル・`_render_page_markdown`の見出しタグに使われる。

- **使われ方**: `_handle_text_item`が`text_level`1/2のブロックから生成、`_assign_sentence_ids`が章の切れ目として通し番号をリセットする。

### CaptionElement

- **種別**: データ構造
- **グループ**: 中間表現
- **役割**: 図表のキャプション。元論文のFig./Table番号に紐づけて文単位でナンバリングする。翻訳対象の本文テキスト。
- **フィールド**:

| フィールド | 型 | 役割 |
|---|---|---|
| `sentences` | `list[str]` | 分割済みのキャプション文 |
| `number` | `int` | 紐づく図表の番号 |
| `fig_kind` | `str`（既定`"figure"`） | `"figure"` \| `"table"` |
| `sentence_ids` | `list[str]` | 各文のID（`_assign_sentence_ids`が`[P{page}-FIG{n}-CAPTION-S{通し}]`（表は`TABLE{n}`）形式で設定） |
| `kind` | `str`（既定`"caption"`） | 要素種別の識別子 |

- **使われ方**: `_handle_image_or_table_item`が対応する`FigureElement`の直後に生成、`_render_page_markdown`が文ごとに`[sent_id] sentence`行として出力する。

### UnknownElement

- **種別**: データ構造
- **グループ**: 中間表現
- **役割**: 構造解析ステップが解釈できなかった要素のフォールバック表現。翻訳対象外（構造不明のため安全側に倒す）。
- **フィールド**:

| フィールド | 型 | 役割 |
|---|---|---|
| `raw_type` | `str` | MinerUが付与した元の要素種別（例:`"code"`） |
| `text` | `str` | フォールバック時に出力する生テキスト。取得できなければ空文字列 |
| `reason` | `str`（既定`""`） | フォールバックに至った理由（デバッグ用。例外メッセージ等） |
| `kind` | `str`（既定`"unknown"`） | 要素種別の識別子 |

- **使われ方**: `_handle_unknown_item`が生成（未知種別、または既知種別の処理中の例外時）、`_render_page_markdown`がページ内連番`unknown_seq`でタグを一意化して`[P{page}-UNKNOWN-{raw_type}-{n}] text`行として出力する。

## 5. 関連ドキュメント

- `doc/architecture.md` §2「パイプラインの7工程」: 7工程モデルの正データ。工程(2)がMinerU実行という唯一の重い外部処理を含む位置づけを説明する。
- `doc/architecture.md`の「5. データ構造」節: 工程(2)が生成する7種類のPDF解析側の型（5.1節）と、タグ付きMarkdown（5.2節）への変換の詳細。6節の`mainCode/stage2/stage2.py`の項も参照。
- `doc/architecture/stage1.md`: `process_pdf`が受け取る`start_page`/`end_page`を、CLI引数（`--chapter`/`--start-label`等）から確定する工程(1)「ページ範囲の決定」。
- `doc/architecture/stage3.md`: 工程(2)が出力するタグ付きMarkdown（`page_XX_en.md`）を読み取り、翻訳直前の`DocUnit`列へ変換する工程(3)「構造化・タグ処理」。
- `CLAUDE.md`「テスト・実行運用規定」項目6: MinerU実行結果のローカルキャッシュ運用（キーの構成要素・フォールバック方針・バージョン更新時の扱い）。
