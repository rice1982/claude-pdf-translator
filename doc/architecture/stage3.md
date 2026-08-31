# stage3.md — `mainCode/stage3/stage3.py`

## 1. 概要

`mainCode/stage3/stage3.py`は工程(3)「構造化・タグ処理」を担う。

やっていることを一言でいうと、工程(2)が出力したタグ付きMarkdown（`[P1-S2-abstract-S2] text`のような行冒頭の識別タグを持つ）を解析し、タグIDと本文の1対1対応を保ったまま`shared.DocUnit`の順序付きリストへ変換した上で、翻訳直前に必要な仕上げ（参考文献除外・文書文脈組み立て・数式スパンの保護）まで行う処理である。

ページ構造・章立てを一切解釈しない後続の翻訳ステップが、タグ種別（見出し／メタ情報／本文文）だけを見て処理を分岐できるようにするのがこのファイルの責務。

- **解析**（`parse_page_file`/`parse_output_dir`/`_classify`）: タグ付きMarkdownの各行を、タグ文字列から判定した種別（`kind`）を持つ`DocUnit`へ変換する。
- **文脈組み立て・除外**（`build_document_context`/`exclude_references_section`/`_section_slug`）: DeepLの`context`引数用にタイトル+Abstractから文書全体の文脈を組み立て、参考文献セクションを翻訳対象から除外する。
- **仕上げ**（`normalize`/`protect_units`）: 翻訳直前のDocUnitを組み立てる工程(3)の最終ステップ。`normalize`は本文中の`\textless`/`\textgreater`正規化、`protect_units`は翻訳対象unitの数式スパンのプレースホルダ退避を行う。どちらも実際の変換（`protect`/`restore`）は`mainCode/shared/shared.py`へ委譲し、このファイル自身はそれを呼ぶだけ（退避・復元処理自体は工程(6)の複数関数からも使われるため`shared.py`に置かれている）。
- **入口**（`prepare_translation_input`）: `parse_output_dir`→本文の数式正規化→`exclude_references_section`→`build_document_context`→`protect_units`をこの順で実行する、工程(3)全体を代表する単一の入出力を持つ関数。既存のタグ付きMarkdownからの再実行にも使える独立した入口として、`whole_pipeline.main()`以外からも呼べる。

## 2. 構成要素（4グループ）

このファイルに独自`class`定義は無い（データを運ぶ型を持つグループは無く、4グループとも関数のみ）。

- **解析（関数）**: `parse_page_file`（1ページ分のMarkdown→DocUnitリスト）・`parse_output_dir`（output_dir配下の全ページを結合。内部に`_page_number`というネスト関数を持つ）・`_classify`（タグ文字列→kind判定）。
- **文脈組み立て・除外（関数）**: `build_document_context`（タイトル+Abstractから文脈文字列を組み立て）・`exclude_references_section`（参考文献セクションを翻訳対象から除外）・`_section_slug`（両者が使う内部ヘルパー）。
- **仕上げ（関数）**: `normalize`（`\textless`/`\textgreater`正規化）・`protect_units`（数式スパンの保護）。
- **入口（関数）**: `prepare_translation_input`。

## 3. prepare_translation_input()の処理フロー

### 3.1 関数依存関係図

```
prepare_translation_input()
├── parse_output_dir()
│     ├── _page_number()                 … sorted()のkeyとして、ファイルごとに1回（ネスト関数）
│     └── parse_page_file()              … page_*_en.mdファイルごとに1回
│           └── _classify()                … タグ行ごとに1回
├── normalize()                        … 翻訳対象unitのen_textごとに1回（対象kindのみ）
├── exclude_references_section()
│     └── _section_slug()                … unitごとに1回
├── build_document_context()
└── protect_units()
```

### 3.2 実行フローの設計

`prepare_translation_input`は「解析（`parse_output_dir`）→数式正規化（`normalize`、unitごと）→参考文献除外（`exclude_references_section`）→文書文脈組み立て（`build_document_context`）→数式保護（`protect_units`）」をこの順に直接呼ぶだけの薄いオーケストレーターで、結合用の中間関数は無い。`normalize`はこのファイル内では`prepare_translation_input`からのみ、`_TRANSLATABLE_KINDS`（title/heading/body_sentence/caption_sentence、翻訳対象になりうるkindと同じ集合）に該当するunitに限定して呼ばれる。数式正規化を参考文献除外より先に行うことで、後段の`protect_units`が扱う`en_text`は既に`\textless`/`\textgreater`正規化済みの状態になる。

### 3.3 エラー処理方針

`output_dir`配下に`page_*_en.md`が1つも見つからない場合、`parse_output_dir`が空リストを返した時点で`prepare_translation_input`自身が`SystemExit`を送出する。後続の全ステップ（数式正規化・参考文献除外・文脈組み立て・数式保護）が扱うデータが無い以上、ここで即座に打ち切る。

## 4. 構成要素リファレンス

各項目は先頭に`種別`（関数／データ構造／ヘルパークラス）を持つ。このファイルの構成要素はすべて関数で、3.1節の依存関係図と同じ順に並べている。各項目の「グループ」は2節の4グループ（解析／文脈組み立て・除外／仕上げ／入口）のどれに属するかを示す。データ構造・ヘルパークラス・独自の`class`定義は無い。

### prepare_translation_input

- **種別**: 関数
- **グループ**: 入口
- **呼び出し元**: `mainCode/whole_pipeline/whole_pipeline.py`の`main()`（工程(2)`process_pdf`の直後）。既存のタグ付きMarkdownからの再実行にも使える独立した入口としても呼べる。
- **入力**: `output_dir`（`page_*_en.md`が格納されたディレクトリ）
- **出力**: `(翻訳対象のDocUnitリスト, 文書文脈文字列)`のタプル
- **処理内容**: 3節参照。
- **テスト対象**: `test_prepare_translation_input_raises_when_no_markdown_files`で`page_*_en.md`が無い場合に`SystemExit`になることを、`test_prepare_translation_input_runs_full_pipeline_and_normalizes_only_specific_kinds`で解析→正規化→除外→文脈組み立て→保護の一連の流れと、数式正規化が対象kindのunitだけに適用されることを直接検証している。全体テスト（`testCode/test_whole_pipeline.py`）・統合テスト（`testCode/test_integration.py`）からもエンドツーエンドで検証されている。

### parse_output_dir

- **種別**: 関数
- **グループ**: 解析
- **呼び出し元**: `prepare_translation_input()`
- **入力**: `output_dir`（`page_*_en.md`が格納されたディレクトリ）
- **出力**: `DocUnit`の順序付きリスト
- **処理内容**: `output_dir`配下から`page_*_en.md`にマッチするファイルを`glob`で探し、内部のネスト関数`_page_number`（ファイル名から抽出したページ番号）をソートキーにページ順へ並べた上で、各ファイルを`parse_page_file`で解析し1本のリストへ結合する。
- **テスト対象**: `test_parse_output_dir_combines_pages_in_page_number_order`で、`page_02_en.md`を先に書き込んでも`page_01_en.md`の内容が先頭に来る（ファイル名のページ番号順に結合される）ことを確認している。

### _page_number

- **種別**: 関数
- **グループ**: 解析
- **呼び出し元**: `parse_output_dir`内部のみ（`sorted()`のkeyとして、ファイルごとに呼ばれるネスト関数）
- **入力**: `path`（Markdownファイルのパス）
- **出力**: ファイル名から抽出したページ番号（`int`）。`page_(\d+)_en`にマッチしない場合は`0`
- **処理内容**: ファイル名から正規表現でページ番号を取り出す、`parse_output_dir`専用のソートキー関数。
- **テスト対象**: 専用の直接テストは無い。`parse_output_dir`経由（`test_parse_output_dir_combines_pages_in_page_number_order`）で間接的に検証される。

### parse_page_file

- **種別**: 関数
- **グループ**: 解析
- **呼び出し元**: `parse_output_dir()`（`page_*_en.md`ファイルごとに1回）
- **入力**: `path`（1ページ分のタグ付きMarkdownファイルのパス）
- **出力**: `DocUnit`のリスト
- **処理内容**: ファイルを1行ずつ読み、画像行（`![tag](path) [tag]`形式）は`figure_image`/`equation_image`のDocUnitへ、タグ行（`[tag] body`形式）は`_classify`でkindを判定した上でDocUnitへ変換する。`[TAG]`で始まらない行（空行・画像行を除く）は新規unitにせず、直前のDocUnitのen_text（非翻訳対象unitならja_textも同様に）へ半角スペース区切りで連結する（工程(2)が改行を含むテキストを複数の物理行として書き出すケースへの対応。連結先が無い場合は従来通り無視する）。
- **テスト対象**: `test_parse_page_file_classifies_tag_kinds_and_translatability`で、title/authors/affil/heading/body_sentence/caption_sentence/figure_image/equation_latexという全kindの判定とtranslatable値を、`test_parse_page_file_classifies_unrecognized_tag_format_as_unknown`で未知のタグ形式が"unknown"かつ翻訳対象外になることを、`test_parse_page_file_ignores_orphan_continuation_line_at_file_start`でファイル先頭に連結先の無い継続行が出現した場合は無視されることを、`test_parse_page_file_joins_continuation_line_to_preceding_unit`で継続行が直前unitへ正しく連結されることを、それぞれ確認している。

### _classify

- **種別**: 関数
- **グループ**: 解析
- **呼び出し元**: `parse_page_file()`（タグ行ごとに1回）
- **入力**: `tag`（タグ文字列。例:`"P1-S1-abstract-S1"`）
- **出力**: kind文字列（`"equation_latex"`|`"title"`|`"authors"`|`"affil"`|`"heading"`|`"unknown"`|`"caption_sentence"`|`"body_sentence"`のいずれか）
- **処理内容**: タグ文字列の形式を上から順に判定する優先順位付きのパターンマッチ（末尾が`-LATEX`のものを最優先で`equation_latex`とし、以下`TITLE`/`AUTHORS`/`AFFIL`/`HEADING-`/`UNKNOWN-`/`-CAPTION-S\d+`/`S\d+-`の順）で、いずれにも一致しなければ`"unknown"`にフォールバックする。
- **テスト対象**: タグ文字列からkindを判定するロジックそのものを直接対象にした単体テストは無く、`parse_page_file`経由（`test_parse_page_file_classifies_tag_kinds_and_translatability`・`test_parse_page_file_classifies_unrecognized_tag_format_as_unknown`）で間接的に検証される。

### normalize

- **種別**: 関数
- **グループ**: 仕上げ
- **呼び出し元**: `prepare_translation_input()`（`_TRANSLATABLE_KINDS`に該当するunitのen_textごとに1回）
- **入力**: `text`（正規化対象の文字列）
- **出力**: 正規化後の文字列
- **処理内容**: `protect`→`restore`のラウンドトリップを利用して、数式スパン（`$...$`/`$$...$$`）の内側にだけ含まれる`\textless`/`\textgreater`を`<`/`>`へ正規化する。数式スパンの外側にある同じ文字列や、数式を含まない文字列はそのまま変更されない。
- **テスト対象**: `test_normalize_converts_textless_textgreater_only_inside_math_spans`で、`\textless`/`\textgreater`が数式スパンの内側でだけ正規化され外側では変更されないことを、`test_math_protection_round_trips_all_real_inline_math`（sample0.pdfの実データ、`processed`fixture使用）で、実際のページに含まれる全数式スパン（インライン24+ディスプレイ3の27スパン）が`\textless`/`\textgreater`正規化を除いて壊れずラウンドトリップすることを確認している。

### exclude_references_section

- **種別**: 関数
- **グループ**: 文脈組み立て・除外
- **呼び出し元**: `prepare_translation_input()`
- **入力**: `units`（`DocUnit`のリスト。書き換え対象）
- **出力**: なし（`units`内の該当unitの`translatable`/`ja_text`を書き換える副作用のみ）
- **処理内容**: `_section_slug`で各unitの章スラッグを求め、`_REFERENCES_SLUG_RE`に一致する（章スラッグ全体が"references"、または"5.references"のようにドット区切りの末尾要素が"references"）unitを翻訳対象から除外し、原文をそのまま`ja_text`へコピーする。著者名等の固有名詞が翻訳エンジンによって書き換えられるのを防ぐための処理。
- **テスト対象**: `test_exclude_references_section_marks_units_non_translatable`で、参考文献セクションの見出し・本文文の両方が`translatable=False`になり`ja_text`に原文がコピーされる一方、無関係なセクションのunitは変更されないことを、`test_exclude_references_section_is_case_insensitive_and_requires_exact_word`で、大文字小文字を区別しない一方"reference"（単数形）のような部分一致では除外されないことを確認している。

### _section_slug

- **種別**: 関数
- **グループ**: 文脈組み立て・除外
- **呼び出し元**: `exclude_references_section()`（unitごとに1回）
- **入力**: `unit`（`DocUnit`）
- **出力**: 章スラッグ文字列。`heading`/`body_sentence`以外のkind、またはタグ形式が一致しない場合は`None`
- **処理内容**: タグ文字列から、見出し（`P\d+-HEADING-(.+)`）または本文文（`P\d+-S\d+-(.+)-S\d+`）の章スラッグ部分を正規表現で取り出す。`exclude_references_section`の参考文献判定にのみ使う内部ヘルパー。
- **テスト対象**: 専用の直接テストは無い。`exclude_references_section`経由（`test_exclude_references_section_marks_units_non_translatable`・`test_exclude_references_section_is_case_insensitive_and_requires_exact_word`）で間接的に検証される。

### build_document_context

- **種別**: 関数
- **グループ**: 文脈組み立て・除外
- **呼び出し元**: `prepare_translation_input()`
- **入力**: `units`（`DocUnit`のリスト）
- **出力**: 文書全体の文脈文字列（タイトル＋Abstract、DeepLの`context`引数用）
- **処理内容**: `kind == "title"`のunitのen_textをタイトルとして1つ取り、タグに`"-abstract-"`（ハイフン区切りの完全一致で、"abstractive"のような接頭辞一致とは区別する）を含む`body_sentence`をAbstract文として連結する。タイトル・Abstractのどちらか（または両方）が存在しない場合は、存在する側だけで組み立てる（両方無ければ空文字列）。
- **テスト対象**: `test_build_document_context_uses_title_and_abstract_sentences`で標準的な組み立てを、`test_build_document_context_falls_back_when_title_or_abstract_missing`でタイトル・Abstractの片方または両方が欠けた場合のフォールバックを、`test_build_document_context_does_not_match_section_slug_prefixed_with_abstract`で"abstractive"のような"abstract"を接頭辞に持つ別セクションが誤って混同されないことを確認している。

### protect_units

- **種別**: 関数
- **グループ**: 仕上げ
- **呼び出し元**: `prepare_translation_input()`（工程(3)の最終ステップ）
- **入力**: `units`（`DocUnit`のリスト。書き換え対象）
- **出力**: なし（`unit.protected_en_text`/`unit.math_spans`へ書き込む副作用のみ）
- **処理内容**: 翻訳対象（`translatable=True`）のunitごとに`protect`を適用し、結果を`unit.protected_en_text`/`unit.math_spans`へ書き込む。`unit.en_text`自体は変更しない。翻訳エンジン（`call_deepl`）はここで設定された`protected_en_text`/`math_spans`をそのまま使い、自身では二度と`protect`を呼ばない。
- **テスト対象**: `test_protect_units_sets_protected_text_only_for_translatable_units`で、翻訳対象unitだけ`protected_en_text`/`math_spans`が設定され、非翻訳対象unitはDocUnitの既定値（空文字列/空リスト）のまま変更されないことを確認している。

## 5. 関連ドキュメント

- `doc/architecture.md` §2「パイプラインの7工程」: 7工程モデルの正データ。工程(3)が「タグ付きMarkdown→翻訳直前のDocUnit」への仕上げ役である位置づけを説明する。
- `doc/architecture.md`の「5. データ構造」節: 5.2節（タグ付きMarkdown）→5.3節（DocUnit）への変換の詳細（`_classify`が担う`DocUnit.kind`の決定を含む）。6節の`mainCode/stage3/stage3.py`の項も参照。
- `doc/architecture/shared.md`: `protect`/`restore`の実装詳細（`normalize`・`protect_units`が委譲する先）。
