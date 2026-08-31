# stage1.md — `mainCode/stage1/stage1.py`

## 1. 概要

`mainCode/stage1/stage1.py`は工程(1)「ページ範囲の決定」を担う。

やっていることを一言でいうと、CLIから渡された5つのページ範囲指定引数（`--start`/`--end`/`--start-label`/`--end-label`/`--chapter`）を優先順位に従って1本化し、`stage2.process_pdf`が要求する物理ページ番号（1始まり）のペアへ変換する処理である。

`--chapter`/`--start-label`/`--end-label`という、物理ページ番号以外の指定方法を物理ページ番号へ変換する部分は、同じ種類の責務を持つ2つの独立したアルゴリズムに分かれており、その2つをCLI引数の優先順位に従って呼び分ける入口関数（`resolve_page_range`）とあわせて1ファイルにまとめている。

- **印刷ページラベル系**（`resolve_physical_page`/`resolve_physical_page_range`）: fitzの`Page.get_label()`（本の見た目のページ数字。表紙は`"cov"`、前付けはローマ数字等）を扱う。
- **章(目次)系**（`parse_chapter_spec`/`resolve_chapter_page_range`）: fitzの`get_toc()`（しおり構造）を扱う。
- **入口**（`resolve_page_range`）: `--start`/`--end`（物理ページ番号）・`--start-label`/`--end-label`（印刷ページラベル）・`--chapter`（章指定）という5つのCLI引数の優先順位（物理ページ指定 > 印刷ラベル指定 > 章指定）を判断し、上記2グループのどちらか一方を呼ぶ。

いずれも「`--start`/`--end`以外の方法で範囲を指定したい」という同じ目的のための代替手段であり、`shared/`データ型のような「他の複数工程から呼ばれる」性質ではなく、あくまで工程(1)内部で完結する実装のため、共通モジュールへは分離せず1ファイルに同居させている。章(目次)系の`_is_body_page`は、印刷ページラベル系と同じ`get_label()`を使って前付け判定を行っており、両者には元々わずかな結合がある（3.3節参照）。

## 2. 構成要素（3グループ）

このファイルに独自`class`定義は無い（データを運ぶ型を持つグループは無く、3グループとも関数のみ）。

- **印刷ページラベル系（関数）**: `resolve_physical_page`（1ページ分の変換）・`resolve_physical_page_range`（開始・終了2ページ分をまとめて変換）。
- **章(目次)系（関数）**: `parse_chapter_spec`（章指定文字列→章番号リスト）・`_is_body_page`（前付け判定）・`resolve_chapter_page_range`（章番号→ページ範囲。内部に`_end_page_for`というネスト関数を持つ）。
- **入口（関数）**: `resolve_page_range`。CLI引数の優先順位判断のみを行い、実際の変換は上記2グループへ委譲する。

## 3. resolve_page_range()の処理フロー

### 3.1 関数依存関係図

```
resolve_page_range()
├── resolve_physical_page_range()   … --start-label/--end-label指定時のみ
│     └── resolve_physical_page()     （start_label用・end_label用に最大2回）
└── resolve_chapter_page_range()    … 上記が両方とも未指定で--chapter指定時のみ
      ├── parse_chapter_spec()
      ├── _is_body_page()             （目次の最上位階層項目ごとに、前付け判定用）
      └── _end_page_for()             （ネスト関数。指定章ごとに終了ページを計算）
```

`resolve_physical_page_range`と`resolve_chapter_page_range`はどちらも`resolve_page_range`からのみ呼ばれ、互いを呼ぶことはない（3.3節の結合は`get_label()`という共通データソースの再利用であって、関数同士の呼び出し関係ではない）。

### 3.2 優先順位の設計

`resolve_page_range`は次の優先順位でページ範囲を1本化する: 物理ページ指定（`--start`/`--end`） > 印刷ラベル指定（`--start-label`/`--end-label`） > 章指定（`--chapter`）。同じ境界（開始または終了）に対して物理ページ番号と印刷ページラベルの両方が指定された場合（例: `--start`と`--start-label`を同時指定）は、指定が矛盾するため`ValueError`にする。`--chapter`と他の指定が同時に来た場合は、`--chapter`を無視して他方を優先する（エラーにはしない）。

### 3.3 印刷ページラベル系と章系の結合

`_is_body_page`は、章(目次)系の関数でありながら印刷ページラベル系と同じ`fitz.Page.get_label()`を使い、対象ページの印刷ページ番号が算用数字かどうかを判定する。これは、書籍PDF（`sample3.pdf`等）で表紙・目次・序文などの前付けにローマ数字の印刷ページ番号が振られ、本文の算用数字ページ番号がその後から始まる構造に対応するためで、印刷ページラベルの情報を持たない文書（学術論文PDF等）では常に`False`を返す（＝この判定自体を使わないよう呼び出し側で制御する）。

## 4. 構成要素リファレンス

各項目は先頭に`種別`（関数／データ構造／ヘルパークラス）を持つ。このファイルの構成要素はすべて関数で、3.1節の依存関係図と同じ順（入口の`resolve_page_range`を先頭に、そこから呼ばれる順）に並べている。各項目の「グループ」は2節の3グループ（印刷ページラベル系／章(目次)系／入口）のどれに属するかを示す。独自`class`定義は`PageLabelResolutionError`・`ChapterResolutionError`の2つの例外クラスのみで、データ構造（dataclass）・ヘルパークラスは無いため、これらは独立した項目を設けず、各例外を送出する関数の項目内で言及するに留める。

### resolve_page_range

- **種別**: 関数
- **グループ**: 入口
- **呼び出し元**: `mainCode/whole_pipeline/whole_pipeline.py`の`main()`（`_require_pdf_exists`の直後）
- **入力**: `pdf_path`・`chapter`（`--chapter`、未指定なら`None`）・`start`/`end`（`--start`/`--end`、未指定なら`None`）・`start_label`/`end_label`（`--start-label`/`--end-label`、未指定なら`None`）
- **出力**: `(開始物理ページ, 終了物理ページ)`のタプル。5引数すべて未指定なら`(None, None)`
- **処理内容**: 3.2節の優先順位でページ範囲を1本化する。`--start`と`--start-label`（または`--end`と`--end-label`）の同時指定は`ValueError`。印刷ページラベルが指定されていれば`resolve_physical_page_range`で解決し、`--chapter`のみ指定なら`resolve_chapter_page_range`で解決する。ページ範囲・章のいずれも指定が無ければ`(None, None)`を返す（呼び出し側で全ページ処理として扱われる）。
- **テスト対象**: `test_resolve_page_range_conflicting_start_and_start_label_raises`/`test_resolve_page_range_conflicting_end_and_end_label_raises`で矛盾指定のエラーを、`test_resolve_page_range_combines_physical_start_with_end_label`/`test_resolve_page_range_combines_start_label_with_physical_end`で異なる境界同士の組み合わせでも値が失われない回帰を、`test_resolve_page_range_prefers_start_over_chapter`/`test_resolve_page_range_prefers_label_over_chapter`（`sample3.pdf`使用）/`test_resolve_page_range_falls_back_to_chapter_when_no_page_range_given`で優先順位を、`test_resolve_page_range_returns_none_when_nothing_specified`で未指定時の挙動を確認している。

### resolve_physical_page_range

- **種別**: 関数
- **グループ**: 印刷ページラベル系
- **呼び出し元**: `resolve_page_range()`（`start_label`/`end_label`のいずれかが指定された場合のみ）
- **入力**: `pdf_path`・`start_label`（開始印刷ページラベル、Noneなら変換しない）・`end_label`（終了印刷ページラベル、Noneなら変換しない）
- **出力**: `(開始物理ページ, 終了物理ページ)`のタプル。各要素は対応するラベルがNoneならNone
- **処理内容**: `start_label`/`end_label`それぞれについて`resolve_physical_page`を呼び出し、印刷ページラベルを物理ページ番号へ変換する。両方Noneの場合はPDFを一切開かずに`(None, None)`を返す。
- **テスト対象**: `test_resolve_physical_page_range_handles_label_gap`で、印刷ページ番号にギャップ（欠番）がある場合でも開始・終了ラベルから物理ページ範囲へ正しく変換されることを、`test_resolve_physical_page_range_returns_none_when_both_labels_omitted`で両方Noneの場合にPDFを開かず`(None, None)`を返すことを確認している。

### resolve_physical_page

- **種別**: 関数
- **グループ**: 印刷ページラベル系
- **呼び出し元**: `resolve_physical_page_range()`（start_label用・end_label用にそれぞれ）
- **入力**: `pdf_path`（入力PDFファイルのパス）・`label`（変換したい印刷ページラベル1つ。例:`"cov"`,`"i"`,`"xviii"`,`"36"`）
- **出力**: 対応する物理ページ番号（int、1始まり）
- **処理内容**: fitzでPDFを開き全ページの`get_label()`を走査し、大文字小文字を区別せず一致するページを探す。表紙ページはfitzが返す実際のラベルが`"Cover"`（フルワード）であることが多いため、利用者が短縮形`"cov"`を指定した場合は`"cov"`で始まるラベルにも一致させる。見つからなければ`PageLabelResolutionError`を送出する。
- **テスト対象**: `test_resolve_physical_page_for_sample3`で`"cov"`/`"COV"`/`"Cover"`/`"i"`/`"xviii"`/`"55"`/`"60"`等のラベルが正しい物理ページ番号に変換されることを、`test_resolve_physical_page_case_insensitive_and_cov_shorthand_on_synthetic_pdf`で同じ性質を合成PDFでも決定的に検証している。`test_resolve_physical_page_unknown_label_raises`で存在しないラベルを指定した場合に`PageLabelResolutionError`になることを、`test_resolve_physical_page_without_page_labels_raises`で印刷ページラベル情報を持たないPDFではエラーになることを確認している。

### resolve_chapter_page_range

- **種別**: 関数
- **グループ**: 章(目次)系
- **呼び出し元**: `resolve_page_range()`（`--chapter`のみ指定された場合）
- **入力**: `pdf_path`・`chapter_spec`（章指定文字列、`parse_chapter_spec`参照）
- **出力**: `(開始物理ページ, 終了物理ページ)`のタプル（両端含む、1始まり）。複数章指定時はそれらすべてを含む最小の連続ページ範囲
- **処理内容**: 目次(TOC)最上位階層（レベル最小値）の項目のうち、`_is_body_page`で前付けを除外した`body_pages`（無ければ全項目にフォールバック）を章の開始ページ群とし、`parse_chapter_spec`が返す章番号に対応する範囲を計算する。章番号は目次の物理ページ順ではなく「TOCリストに登場する順」で振られる。終了ページは`_end_page_for`（開始ページ集合の大小関係のみを使う）で求める。目次情報が無いPDFや範囲外の章番号を指定した場合は`ChapterResolutionError`。
- **テスト対象**: `test_resolve_chapter_page_range_computes_ranges_from_toc`で章の終了ページが次の章の開始ページの直前として正しく計算されることを、`test_resolve_chapter_page_range_numbers_chapters_by_toc_list_order`でTOCの並び順が物理ページ順と異なる場合の章番号割り当てを、`test_resolve_chapter_page_range_duplicate_top_level_pages_do_not_crash`で同じ物理ページを指す重複ブックマークがあってもクラッシュしないことを、`test_resolve_chapter_page_range_requires_toc`で目次が無いPDFのエラーを、`test_resolve_chapter_page_range_out_of_range`で範囲外の章番号のエラーを、いずれも合成PDFで確認している。実データ確認は`test_resolve_chapter_page_range_skips_roman_numeral_front_matter`（`sample3.pdf`、前付け除外の確認。詳細は`_is_body_page`の項）。

### parse_chapter_spec

- **種別**: 関数
- **グループ**: 章(目次)系
- **呼び出し元**: `resolve_chapter_page_range()`
- **入力**: `spec`（章指定文字列。例:`"1,2"`,`"1-2"`,`"1,3-4"`）
- **出力**: 昇順ソート済み・重複除去済みの章番号リスト（`list[int]`）
- **処理内容**: カンマ・ハイフン区切りの章指定を展開する。空文字列・`"0"`・非数値・逆順範囲（例:`"2-1"`）等の不正形式は`ChapterResolutionError`にする。
- **テスト対象**: `test_parse_chapter_spec_valid`（parametrize、8ケース）で正常な章指定が正しく章番号リストに変換されることを、`test_parse_chapter_spec_invalid`（parametrize、6ケース）で不正な指定が`ChapterResolutionError`になることを確認している。

### _is_body_page

- **種別**: 関数
- **グループ**: 章(目次)系
- **呼び出し元**: `resolve_chapter_page_range()`（目次の最上位階層項目ごとに、`body_pages`を組み立てるリスト内包表記から）
- **入力**: `doc`（開いた状態の`fitz.Document`）・`page_number_1based`（判定したい物理ページ番号、1始まり）
- **出力**: bool（本文＝算用数字の印刷ページラベルなら`True`）
- **処理内容**: 該当ページの印刷ページラベルが算用数字かどうかだけを判定する。「ラベルが一切無いPDF」と「ラベルはあるが対象ページが全て前付け（ローマ数字等）」のどちらも同じ`False`にしか見えないため、両ケースとも呼び出し側（`resolve_chapter_page_range`）の`body_pages`が空になるフォールバック分岐へ同じ経路で合流する。
- **テスト対象**: `test_resolve_chapter_page_range_skips_roman_numeral_front_matter`で、ローマ数字の前付けページが章の起点として数えられないことを（唯一の実データ確認、`sample3.pdf`）、`test_resolve_chapter_page_range_without_page_labels_uses_all_top_level_entries`・`test_resolve_chapter_page_range_falls_back_when_toc_entries_are_all_front_matter`で、「ラベル情報が無い」場合と「ラベル情報はあるが該当ページが全て前付け」という異なる入力経路のどちらからも同じフォールバック（最上位階層すべてを章として数える）に到達することを、それぞれ合成PDFで確認している。

### _end_page_for

- **種別**: 関数
- **グループ**: 章(目次)系
- **呼び出し元**: `resolve_chapter_page_range()`内部（ネスト関数、指定章ごとに呼ばれる）
- **入力**: `start_page`（終了ページを求めたい章の開始ページ番号）
- **出力**: その章の終了ページ番号（int）
- **処理内容**: `resolve_chapter_page_range`のクロージャとして、全章の開始ページ集合（`sorted_starts`）の中で`start_page`より大きい最小値の直前を返す（無ければPDF総ページ数）。TOCの並び順ではなく開始ページの大小関係だけを使うため、ブックマークが物理ページ順と一致しない文書（LaTeX由来のPDFでしおりの並びが崩れているケース等）でも安定して終了ページを決められる。
- **テスト対象**: `test_resolve_chapter_page_range_computes_ranges_from_toc`で、章の終了ページが次の章の開始ページの直前として正しく計算されること（合成TOCによる例: 章1→(1,3)、章2→(4,8)）を確認している。専用の単体テストは無く、`resolve_chapter_page_range`経由でのみ検証される。

## 5. 関連ドキュメント

- `doc/architecture.md` §2「パイプラインの7工程」: 7工程モデルの正データ。工程(1)全体の中でこのファイルの各関数が担う役割の位置づけを説明する。
- `doc/architecture.md`: プロジェクト全体のディレクトリ構成・呼び出し木図・モジュール依存関係の中でのこのファイルの位置づけ（§6にこのファイルがなぜこの関数群を持つかの要約あり）。
