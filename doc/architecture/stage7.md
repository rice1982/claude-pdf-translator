# stage7.md — `mainCode/stage7/stage7.py`

## 1. 概要

`mainCode/stage7/stage7.py`は工程(7)「PDF生成」を担う。

やっていることを一言でいうと、工程(6)まで仕上げた翻訳済みの`DocUnit`列を受け取り、見出し・段落・図表といった表示単位（`Block`）へ再編成したうえでHTML/CSSを組み立て、ヘッドレスのChromium（Playwright経由）で「PDFに印刷」して、対訳版・英語版・日本語版の3つのPDFファイルを生成する処理である（インライン数式はブラウザ上でKaTeXが実際の数式として描画する）。

性質の異なる3種類のコードを1ファイルにまとめている。

- **本体**（`build_blocks`/`render_all_pdfs`等）: 翻訳済みの`DocUnit`列を表示単位（見出し・段落・図表）にまとめ、①対訳版 ②英語版のみ ③日本語版のみ の3種類のHTMLを組み立ててPlaywright（Chromium）でPDF化する。対訳版は文ペアをTable Rowで結合し、CSSの`break-inside: avoid`を各行に適用することで、左右の高さズレと文の途中でのページ跨ぎを防止する。日本語の文字化け対策として、CSSのfont-familyにNoto Sans JP系のフォントスタックを指定する。
- **入口**（`render_units_to_pdfs`）: `build_blocks`と`render_all_pdfs`をこの順でまとめて実行する、工程(7)全体を代表する単一の入出力を持つ統括関数。他のどの`mainCode`モジュールにも依存せず、この工程内で完結する。
- **KaTeXアセット**（`load_katex_assets`）: PDF内でインライン数式をKaTeXにより実際の数式として描画するための、オフライン埋め込み用アセット読み込み。`vendor/katex/`にベンダリング済みのKaTeX本体・auto-render拡張・woff2フォント一式を読み込み、フォントをbase64のdata URIへ変換した自己完結なCSSを組み立てる。実行時にCDNへアクセスする必要がないため、オフラインでもPDF生成が失敗しない。

工程(7)だけが、`DocUnit`をもう一度`Block`（段落・見出し・図表などの表示単位）へ再編成する。`DocUnit.kind`（10種）を`Block.kind`（6種）へ意図的に粗く再分類する対応関係は`doc/architecture.md`「5.5 Block」参照。

## 2. 構成要素（8グループ）

グループはコードの`# ===`バナーと1対1で対応し、依存関係のボトムアップ順（呼ばれる側のグループが先、`render_units_to_pdfs`が最後）に並ぶ。`_wrap_html`は対訳版・単一言語版の両レンダラから呼ばれるため、それらのグループより前に置く。

- **Block型（データ構造）**: `Block`（dataclass。PDFレンダリング用の表示単位）。
- **本体共通（関数）**: `_heading_level`・`_paragraph_key`・`_image_data_uri`・`build_blocks`（ネスト関数`flush`を含む）・`_esc`（HTMLエスケープ、対訳版・単一言語版の双方から使われる共通ヘルパー）。
- **KaTeXアセット（関数）**: `_inline_css`（ネスト関数`_inline_block`を含む）・`load_katex_assets`。
- **HTML/CSSテンプレート（関数）**: `_wrap_html`（`@page`・フォントスタック・KaTeX CSS/JS・版ごとのCSSを含む完全なHTML文書で本文を包む）。
- **対訳版HTML（関数）**: `_bilingual_row`・`_render_bilingual_block`・`render_bilingual_html`。
- **単一言語版HTML（関数）**: `_sentence_text`・`_render_mono_block`・`render_mono_html`。
- **PDF出力（関数）**: `render_all_pdfs`（Playwright経由のPDF化）。
- **入口（関数）**: `render_units_to_pdfs`。

## 3. render_units_to_pdfs()の処理フロー

### 3.1 関数依存関係図

```
render_units_to_pdfs()
├── build_blocks()
│     ├── flush()                        … 段落境界ごと（ネスト関数）
│     ├── _heading_level()
│     ├── _paragraph_key()
│     └── _image_data_uri()              … figure_imageのunitごと
└── render_all_pdfs()
      ├── render_bilingual_html()              … 1回
      │     ├── _render_bilingual_block()       … blockごとに1回
      │     │     ├── _bilingual_row()
      │     │     └── _esc()
      │     └── _wrap_html()
      │           └── load_katex_assets()       … @lru_cacheで実質1回のみ実行
      │                 └── _inline_css()
      │                       └── _inline_block()   … @font-faceブロックごと（ネスト関数）
      └── render_mono_html()                    … lang="en"／"ja"の2回
            ├── _render_mono_block()             … blockごとに1回
            │     ├── _sentence_text()
            │     └── _esc()
            └── _wrap_html()
                  └── load_katex_assets()       … （@lru_cacheによりキャッシュ済み・再実行なし）
                        └── _inline_css()
                              └── _inline_block()
```

### 3.2 実行フローの設計

`render_units_to_pdfs`は「グルーピング（`build_blocks`）→PDF化（`render_all_pdfs`）」をこの順に直接呼ぶだけの薄い委譲で、結合用の中間関数は無い。`build_blocks`はPlaywrightを必要としない軽量な純粋関数（`DocUnit`列を見出し・段落・図表の`Block`列へ再編成するだけ）であるのに対し、`render_all_pdfs`はPlaywright（Chromium）を起動する重い処理であり、この2つを明確に分離することで`build_blocks`単体を高速にテストできるようにしている（`render_units_to_pdfs`自体には専用テストを設けず、間に副作用・順序依存・分岐を一切挟まない2行の委譲であることをもって代えている。`doc/architecture/stage5.md`の`apply_restore`と同様の「ラッパーの分割基準」については`project_stage_audit_session`の知見を参照）。

`render_all_pdfs`は対訳版・英語版・日本語版の3PDFを1つのブラウザインスタンス（`p.chromium.launch()`）・1つのページ（`browser.new_page()`）を使い回しながら順に生成する。ページごとに`set_content`でHTMLを差し替え、`emulate_media(media="print")`でCSSの`@page`ルールを有効化してからPDF化する。HTML文字列の組み立て自体は`render_all_pdfs`ではなく各`render_*_html`（対訳版なら`render_bilingual_html`、単一言語版なら`render_mono_html`）が担い、その内部で最後に`_wrap_html`が`<head>`・CSS・KaTeXアセットを付けて完全なHTML文書に仕上げる。`load_katex_assets`は`@lru_cache(maxsize=1)`によりプロセス内で実質1回だけディスクI/Oを行い、3回の`_wrap_html`呼び出し（`render_bilingual_html`から1回・`render_mono_html`から2回）全てで同じインライン化済みアセットを再利用する。

### 3.3 エラー処理方針

`render_all_pdfs`は`try`/`finally`で`browser.close()`のみを保証し、Playwright側の例外（ページ読み込み失敗・PDF化失敗等）はそのまま呼び出し元（`whole_pipeline.main()`）へ伝播する（工程(2)の`MinerURunError`のような専用の例外型への正規化は行わない）。`build_blocks`はunitの`kind`が未知の値（`unknown`）であってもフォールバックの`paragraph`ブロックとして扱い、例外を送出しない。

## 4. 構成要素リファレンス

各項目は先頭に`種別`（関数／データ構造／ヘルパークラス）を持つ。まず関数を3.1節の依存関係図と同じ順（入口`render_units_to_pdfs`を先頭に、そこから呼ばれる順を深さ優先でたどる順）に並べ、その後にデータ構造`Block`を記載する。各項目の「グループ」は2節の8グループ（Block型／本体共通／KaTeXアセット／HTML/CSSテンプレート／対訳版HTML／単一言語版HTML／PDF出力／入口）のどれに属するかを示す。`_esc`・`_wrap_html`・`load_katex_assets`系は対訳版・単一言語版の両ブランチから呼ばれ3.1節の図に2度現れるが、項目は最初の登場箇所に1つだけ置く。

### render_units_to_pdfs

- **種別**: 関数
- **グループ**: 入口
- **呼び出し元**: `mainCode/whole_pipeline/whole_pipeline.py`の`main()`（工程(6)`postprocess`の直後。パイプラインの最終ステップ）
- **入力**: `units`（翻訳済み`DocUnit`のリスト）・`output_dir`（画像の読み込み元・PDFの書き出し先。`str`可）・`log`（進捗ログコールバック、既定`mainCode.shared.shared.log`）
- **出力**: 生成されたPDFファイルパスのリスト（`paper_bilingual.pdf`/`paper_en.pdf`/`paper_ja.pdf`）
- **処理内容**: `build_blocks`（`DocUnit`列→`Block`列のグルーピング）と`render_all_pdfs`（Playwright経由のPDF化）をこの順で呼ぶだけの2行の委譲。3.2節参照。
- **テスト対象**: 専用テストは無い（3.2節参照。`build_blocks`は`TestBuildBlocks`が、`render_all_pdfs`は`TestRenderAllPdfs`がそれぞれ単体で検証しており、両者を単純に直列実行するだけの`render_units_to_pdfs`自体を検証する固有のテストケースは無い）。全体テスト（`testCode/test_whole_pipeline.py`）・統合テスト（`testCode/test_integration.py`）からはエンドツーエンドで検証されている。

### build_blocks

- **種別**: 関数
- **グループ**: 本体共通
- **呼び出し元**: `render_units_to_pdfs()`（工程(7)の最初のステップ）
- **入力**: `units`（`postprocess`まで通した翻訳済み`DocUnit`のリスト）・`output_dir`（`_image_data_uri`が図表PNGを読む基準ディレクトリ）
- **出力**: `Block`のリスト（レンダリング用の表示単位列）
- **処理内容**: `units`を先頭から走査し`DocUnit.kind`ごとに`Block`へ振り分ける。`title`/`authors`・`affil`（→`meta`）/`heading`/`figure_image`（→`figure`）/`equation_latex`（→`equation`）は1 unit = 1 Block。連続する`body_sentence`/`caption_sentence`は`_paragraph_key`が同じ範囲をまとめて1つの`paragraph` Blockにする（キーが変わるか他種別の要素が挟まった時点で段落を確定＝内部クロージャ`flush`）。`equation_image`は`Block`を作らずスキップ（式番号を含む`equation_latex`側でKaTeX描画するため）。想定外の`kind`（`unknown`）は1文だけの`paragraph` Blockとして拾う。`heading`の`Block.level`は`_heading_level`が、`figure`の`Block.image_data_uri`は`_image_data_uri`が決める。Playwrightを必要としない軽量な純粋関数。
- **テスト対象**: `TestBuildBlocks`が、同一章内の連続`body_sentence`が1つの`paragraph`にまとまること（`test_build_blocks_groups_consecutive_body_sentences_into_one_paragraph`）、章番号の深さから見出しレベルが決まること（`_assigns_heading_level_from_section_number_depth`）、`equation_image`がスキップされ`equation_latex`は`equation` Blockとして残ること（`_skips_equation_image_and_keeps_equation_latex_block`）、`caption_sentence`も同じ`_paragraph_key`の仕組みでまとまること（`_groups_consecutive_caption_sentences_into_one_paragraph`、実データ回帰）、`image_rel_path`が`None`の`figure_image`が`image_data_uri=None`の`figure` Blockになること（`_figure_image_without_rel_path_has_no_data_uri`、実データ回帰）を確認している。

### flush（`build_blocks`内）

- **種別**: 関数
- **グループ**: 本体共通
- **呼び出し元**: `build_blocks()`（段落の切れ目ごと＝他種別の要素の追加前・別段落キーの文が来たとき・ループ末尾）
- **入力**: なし（`build_blocks`のローカル変数`current`（段落バッファ）・`current_key`・`blocks`を`nonlocal`で参照・再代入）
- **出力**: なし（副作用: `current`が空でなければ`Block(kind="paragraph", sentences=current)`を`blocks`へ追加し、`current`・`current_key`を空へ戻す）
- **処理内容**: 組み立て中の段落バッファに文が溜まっていれば`paragraph` Blockとして確定し、バッファと段落キーをリセットする。`current`が空なら何もしないため冗長に呼んでも安全。
- **テスト対象**: `build_blocks`のクロージャであり直接テストは無い。`TestBuildBlocks`の段落グルーピング系テストで間接的に検証される。

### _heading_level

- **種別**: 関数
- **グループ**: 本体共通
- **呼び出し元**: `build_blocks()`（`heading` kindのunitごと）
- **入力**: `tag`（見出しunitのタグ文字列。例: `P3-HEADING-2.1.4`）
- **出力**: HTML見出しレベルを表す`int`（2〜4）
- **処理内容**: `_HEADING_SUFFIX_RE`でタグ末尾の`P{page}-HEADING-`以降を取り出し、`.`区切りで先頭から連続する数字パートの個数（章番号の階層の深さ）を数える。`2 + max(深さ - 1, 0)`を`4`でキャップして返す（深さ1→h2、深さ2→h3、深さ3以上→h4）。タグがパターンに合わなければ`2`。
- **テスト対象**: `test_build_blocks_assigns_heading_level_from_section_number_depth`（深さ1・2でレベル2・3）、`test_build_blocks_caps_heading_level_at_four_for_deeply_nested_sections`（深さ3以上でも4・数字部分が無ければ2。`min(..., 4)`のキャップ分岐がmutation testingで未到達だったため追加）で直接検証されている。

### _paragraph_key

- **種別**: 関数
- **グループ**: 本体共通
- **呼び出し元**: `build_blocks()`（`body_sentence`/`caption_sentence` kindのunitごと）
- **入力**: `unit`（`DocUnit`）
- **出力**: 「同じ段落に属する文をまとめるためのキー」文字列
- **処理内容**: `caption_sentence`なら`_CAPTION_KEY_RE`でタグ末尾の`-S{n}`を除いた部分（例: `P3-FIG2-CAPTION`）を返す。それ以外は`_BODY_TAG_RE`でタグから`P{page}`と段落識別子（章ラベル）を取り出し`P{page}-{章ラベル}`（例: `P3-1.introduction`）を返す。どちらのパターンにも合わなければタグ文字列そのまま。タグには段落単位の情報が無いため、本文側は実質「ページ×章」、キャプション側は「ページ×図表」単位のグルーピングになる。
- **テスト対象**: 専用の直接テストは無い。`TestBuildBlocks`の段落グルーピング系テスト（`_groups_consecutive_body_sentences_into_one_paragraph`・`_groups_consecutive_caption_sentences_into_one_paragraph`）経由で間接的に検証される。

### _image_data_uri

- **種別**: 関数
- **グループ**: 本体共通
- **呼び出し元**: `build_blocks()`（`figure_image` kindで`image_rel_path`が非`None`のunitごと）
- **入力**: `output_dir`（基準ディレクトリ）・`rel_path`（画像の相対パス。例: `images/fig_p2_1.png`）
- **出力**: `data:image/png;base64,...`形式のdata URI文字列
- **処理内容**: `output_dir / rel_path`のPNGファイルをバイト列で読み、base64エンコードして`data:`URIに組み立てる。生成HTMLを外部ファイル参照なしの単体ファイルとしてブラウザへ渡せるようにするため。
- **テスト対象**: 専用の直接テストは無い。`test_build_blocks_figure_image_without_rel_path_has_no_data_uri`は「`image_rel_path`が`None`なら本関数を呼ばず`image_data_uri=None`」という逆側のみを確認しており、実ファイル読み込み経路そのものを検証するテストは無い（実データスモークテスト`test_render_all_pdfs_on_real_translated_sample`も`figure_image`を除外している）。

### render_all_pdfs

- **種別**: 関数
- **グループ**: PDF出力
- **呼び出し元**: `render_units_to_pdfs()`（工程(7)の2番目のステップ）
- **入力**: `blocks`（`build_blocks`が返した`Block`のリスト）・`output_dir`（PDFの書き出し先。`str`可）・`log`（進捗メッセージのコールバック、既定`print`）
- **出力**: 生成した3つのPDFファイルパスのリスト（`paper_bilingual.pdf`/`paper_en.pdf`/`paper_ja.pdf`の順）
- **処理内容**: まず`render_bilingual_html`・`render_mono_html`（en/ja）で3種類のHTML文字列を組み立てる。次に`sync_playwright()`でPlaywrightを起動し、ヘッドレスChromiumを1インスタンス・1ページだけ用意して3PDFで使い回す。各HTMLを`set_content(wait_until="load")`で読み込み（インライン化したKaTeXスクリプトの実行完了＝数式描画完了を待つ）、`emulate_media("print")`でCSSの`@page`ルールを有効化し、`page.pdf(print_background=True, prefer_css_page_size=True)`でPDF化する。`try`/`finally`で例外時も`browser.close()`を保証する。
- **テスト対象**: `TestRenderAllPdfs`が、`test_render_all_pdfs_produces_three_nonempty_pdfs_with_japanese_text`で3つのPDFが実際に生成され空でないこと・`paper_ja.pdf`に日本語テキスト（"こんにちは"）が実際に書き込まれていること（`fitz`で抽出）を、`test_render_all_pdfs_on_real_translated_sample`（parametrize: sample0/1/2/3）で`cache/`に凍結された実DeepL翻訳済み`DocUnit`（`figure_image`は除外）を通すスモークテストを行う（実DeepL許可がsample0/1のみのためsample2/3はキャッシュが無くskip）。全体テスト・統合テストからもエンドツーエンドで検証される。

### render_bilingual_html

- **種別**: 関数
- **グループ**: 対訳版HTML
- **呼び出し元**: `render_all_pdfs()`（対訳版HTMLの組み立てに1回）
- **入力**: `blocks`（`Block`のリスト）
- **出力**: 対訳版の完全なHTML文書（文字列）
- **処理内容**: 各`Block`を`_render_bilingual_block`でHTML断片へ変換し、空でない断片を連結する。それを`_wrap_html("対訳版", ..., bilingual=True)`に渡して`<head>`・CSS・KaTeXアセット込みの完全なHTML文書に仕上げる。
- **テスト対象**: 専用の直接テストは無い。対訳版PDF（`paper_bilingual.pdf`）は`TestRenderAllPdfs`で「生成され空でないこと」のみ検証され、対訳版HTMLの内容（英日の対応関係等）を直接検証するテストは無い。ブロックのHTMLエスケープは`TestRenderBlockEscaping`が`_render_bilingual_block`経由で検証する。

### _render_bilingual_block

- **種別**: 関数
- **グループ**: 対訳版HTML
- **呼び出し元**: `render_bilingual_html()`（`Block`ごとに1回）
- **入力**: `block`（`Block`）
- **出力**: その`Block` 1つ分の対訳版HTML断片（未知の`kind`は空文字列）
- **処理内容**: `block.kind`ごとに出し分ける。`title`は英日を1行の表にして両方`<strong>`（`title-block`クラス）、`heading`は英日を1行の表（`heading-block level-{2..4}`クラス）、`paragraph`は`block.sentences`の各文を`_bilingual_row`で「英｜日」の1行にして1つの`<table class="bilingual">`にまとめる、`meta`は対訳せず`unit.en_text`のみを`<div class="meta">`（表にしない）、`figure`は`block.image_data_uri`の画像を`<figure><img>`、`equation`は`unit.en_text`（LaTeX）を`<div class="equation">`（描画はブラウザ上のKaTeX、言語非依存なので左右に分けない）。テキストは全て`_esc`でHTMLエスケープする。
- **テスト対象**: `TestRenderBlockEscaping`が、`meta`ブロックのHTMLエスケープ（`test_meta_block_html_escapes_author_affiliation_text`。修正前は`meta`分岐だけ`_esc`呼び出しが漏れており、著者名・所属に`<`/`>`/`&`を含む場合に誤ってHTML解釈されるバグがあった回帰テスト）と、残り4種別（title/heading/paragraph/equation）も実際に`_esc`が呼ばれていること（`test_non_meta_blocks_also_html_escape_their_text`、parametrize、mutation testing由来）を直接検証している。

### _bilingual_row

- **種別**: 関数
- **グループ**: 対訳版HTML
- **呼び出し元**: `_render_bilingual_block()`（title/heading/paragraphの各行ごと）
- **入力**: `en_html`（英語側セルの中身）・`ja_html`（日本語側セルの中身）・`row_class`（`<tr>`に付けるクラス名、省略可）
- **出力**: `<tr><td class="en">…</td><td class="ja">…</td></tr>`形式の文字列
- **処理内容**: 英日を左右2セルに並べた表の1行を組み立てる。`row_class`があれば`<tr>`に付与する。左右のセル幅50%・`break-inside: avoid`（行の途中でのページ跨ぎ防止）はCSS側（`_wrap_html`の`table.bilingual`）で効かせる。
- **テスト対象**: 専用の直接テストは無い。`_render_bilingual_block`経由（`TestRenderBlockEscaping`）で間接的に検証される。

### _esc

- **種別**: 関数
- **グループ**: 本体共通
- **呼び出し元**: `_render_bilingual_block()`・`_render_mono_block()`（HTML本文へ値を差し込む箇所すべて）
- **入力**: `text`（エスケープ対象の文字列）
- **出力**: `<`・`>`・`&`等をエスケープしたHTML安全な文字列
- **処理内容**: `html.escape`の短縮エイリアス。`en_text`/`ja_text`は生テキスト（MinerUのOCR結果由来で`<`等を含みうる）のため、HTMLへ差し込む前に必ず通す。
- **テスト対象**: HTMLエスケープの正しさは`TestRenderBlockEscaping`が全6ブロック種別について`_render_bilingual_block`/`_render_mono_block`経由で検証している（`_esc`単体の直接テストは無い）。

### _wrap_html

- **種別**: 関数
- **グループ**: HTML/CSSテンプレート
- **呼び出し元**: `render_bilingual_html()`（1回）・`render_mono_html()`（en/jaで2回）
- **入力**: `title`（`<title>`用文字列。"対訳版"/"英語版"/"日本語版"）・`body`（レンダリング済みHTML断片の連結）・`bilingual`（対訳版用CSSを使うか）・`lang`（`<html lang>`の値、既定`"ja"`）
- **出力**: `<!doctype html>`から始まるページ全体のHTML文字列
- **処理内容**: `load_katex_assets`からインライン化済みKaTeX CSS・本体JS・auto-render拡張JSを取得し、`<head>`にKaTeX CSSと共通CSS（`@page` A4・フォントスタック`_FONT_STACK`・`.meta`/`.equation`/`figure`のスタイル・`bilingual`に応じた対訳版CSSか単一言語版CSS）、`<body>`に`body`とKaTeX描画スクリプト（`renderMathInElement`で`$...$`/`$$...$$`を数式化）を並べた完全なHTML文書を組み立てる。
- **テスト対象**: 専用の直接テストは無い。`render_bilingual_html`/`render_mono_html`経由で実行され、`TestRenderMonoHtml`が`render_mono_html`の渡す`<title>`が言語と対応していること（`test_render_mono_html_uses_matching_document_title_for_each_language`、mutation testing由来）を確認している。CSS・KaTeXスクリプトを含むページ全体の組み立てそのものや数式描画の見た目を検証するテストは無く、人間の目視確認に委ねる（`doc/testExplain.txt`の工程(7)「目視チェック実施状況」参照）。

### load_katex_assets

- **種別**: 関数
- **グループ**: KaTeXアセット
- **呼び出し元**: `_wrap_html()`（PDF 1本ごとに1回、計3回。`@lru_cache`により実処理は最初の1回のみ）
- **入力**: なし
- **出力**: `(インライン化済みCSS, katex.min.jsの内容, auto-render.min.jsの内容)`のタプル
- **処理内容**: `vendor/katex/`（`_VENDOR_DIR`）から`katex.min.css`・`katex.min.js`・`auto-render.min.js`を読み、CSSは`_inline_css`で`@font-face`のwoff2参照をbase64 data URIへ置き換える。`@lru_cache(maxsize=1)`でプロセス内で結果を1回だけ計算・キャッシュする。実行時にCDNへアクセスしないためオフラインでもPDF生成が失敗しない。
- **テスト対象**: `test_load_katex_assets_inlines_fonts_as_base64_data_uris`が、返るCSSに`data:font/woff2;base64,`が含まれ生の`url(fonts/`参照が1つも残らないこと・JS 2本が空でないことを直接確認している。

### _inline_css

- **種別**: 関数
- **グループ**: KaTeXアセット
- **呼び出し元**: `load_katex_assets()`（1回）
- **入力**: `css`（KaTeXのCSS文字列）・`fonts_dir`（woff2ファイルのあるディレクトリ）
- **出力**: `@font-face`のフォント参照をdata URIへ置き換えたCSS文字列
- **処理内容**: `_FONTFACE_RE`でCSS中の各`@font-face{...}`ブロックを見つけ、`_inline_block`で内部のwoff2参照を差し替える（`re.sub`）。
- **テスト対象**: 専用の直接テストは無い。`load_katex_assets`経由（`test_load_katex_assets_inlines_fonts_as_base64_data_uris`）で検証される。

### _inline_block（`_inline_css`内）

- **種別**: 関数
- **グループ**: KaTeXアセット
- **呼び出し元**: `_inline_css()`（`_FONTFACE_RE.sub`のコールバックとして`@font-face`ブロックごと）
- **入力**: `match`（1つの`@font-face`ブロックの正規表現マッチ）
- **出力**: woff2参照をdata URIへ差し替えた`@font-face{...}`文字列（woff2参照が無ければ元のまま）
- **処理内容**: ブロック内を`_SRC_WOFF2_RE`で走査して最初のwoff2参照を探し、`fonts_dir`から該当ファイルを読んでbase64 data URIを作り、`src:`行を`src:url({data_uri}) format("woff2")`へ置き換える。
- **テスト対象**: `_inline_css`のクロージャであり直接テストは無い。`load_katex_assets`経由で検証される。

### render_mono_html

- **種別**: 関数
- **グループ**: 単一言語版HTML
- **呼び出し元**: `render_all_pdfs()`（英語版・日本語版で計2回）
- **入力**: `blocks`（`Block`のリスト）・`lang`（`"en"`/`"ja"`）
- **出力**: 単一言語版の完全なHTML文書（文字列）
- **処理内容**: `lang`から`title`（"英語版"/"日本語版"）を決め、各`Block`を`_render_mono_block(block, lang)`でHTML断片へ変換して連結し、`_wrap_html(title, ..., bilingual=False, lang=...)`で完全なHTML文書に仕上げる。
- **テスト対象**: `TestRenderMonoHtml`が`<title>`と`lang`の対応（`test_render_mono_html_uses_matching_document_title_for_each_language`、mutation testing由来）を直接確認する。日本語版レンダリングの中身は統合テスト（`test_translate_and_export_*`）が`paper_ja.pdf`への日本語文字の反映・訳文数の一致まで踏み込んで検証する（英語版は空でないことのみ）。

### _render_mono_block

- **種別**: 関数
- **グループ**: 単一言語版HTML
- **呼び出し元**: `render_mono_html()`（`Block`ごとに1回）
- **入力**: `block`（`Block`）・`lang`（`"en"`/`"ja"`）
- **出力**: その`Block` 1つ分の単一言語版HTML断片（未知の`kind`は空文字列）
- **処理内容**: `block.kind`ごとに、`title`は`<h1>`、`heading`は`<h{block.level}>`、`paragraph`は各文を1つずつ`<p>`にして連結（対訳版の表と違い1文＝1`<p>`）、`figure`は`<figure><img>`、`equation`は`<div class="equation">`。本文は`_sentence_text(unit, lang)`で選択言語のテキストを取る。`meta`だけは`lang`によらず常に`unit.en_text`（著者・所属は訳さない）。テキストは全て`_esc`でエスケープする。
- **テスト対象**: `TestRenderBlockEscaping`が全6ブロック種別の`_esc`適用を`_render_mono_block`経由で確認している（`test_meta_block_html_escapes_author_affiliation_text`・`test_non_meta_blocks_also_html_escape_their_text`）。

### _sentence_text

- **種別**: 関数
- **グループ**: 単一言語版HTML
- **呼び出し元**: `_render_mono_block()`（title/heading/paragraph/equationの本文取得ごと）
- **入力**: `unit`（`DocUnit`）・`lang`（`"en"`/`"ja"`）
- **出力**: `lang == "en"`なら`unit.en_text`、そうでなければ`unit.ja_text`
- **処理内容**: 言語コードに応じて`DocUnit`の該当フィールドを返すだけの1行ヘルパー。
- **テスト対象**: 専用の直接テストは無い。`_render_mono_block`経由（`TestRenderBlockEscaping`・`TestRenderMonoHtml`）で間接的に検証される。

### Block

- **種別**: データ構造
- **グループ**: Block型
- **役割**: PDFレンダリング用に文をまとめた表示単位（段落・見出し・図表など）を持ち回るdataclass。工程(7)内だけで完結する表示単位のため`mainCode/shared/shared.py`ではなくこのファイルに置かれている。
- **フィールド**:

| フィールド | 型 | 役割 |
|---|---|---|
| `kind` | `str` | `"title"`\|`"meta"`\|`"heading"`\|`"paragraph"`\|`"figure"`\|`"equation"` |
| `level` | `int`（既定2） | `kind="heading"`の場合の見出しレベル（h2〜h4） |
| `role` | `str`（既定`""`） | `kind="meta"`の場合の種別（`"authors"`\|`"affil"`） |
| `sentences` | `list[DocUnit]` | この表示単位を構成する文（`DocUnit`の列） |
| `image_data_uri` | `str \| None` | 図表の場合の画像base64データURI |

- **使われ方**: `build_blocks`が`DocUnit.kind`（10種）を粗く再分類して生成し、`render_bilingual_html`/`render_mono_html`（および内部の`_render_bilingual_block`/`_render_mono_block`）が`block.kind`ごとに異なるHTML断片へレンダリングする。`DocUnit.kind`各値との対応関係の詳細は`doc/architecture.md`「5.5 Block」参照。

## 5. 関連ドキュメント

- `doc/architecture.md` §2「パイプラインの7工程」: 7工程モデルの正データ。工程(7)がパイプラインの最終ステップである位置づけを説明する。
- `doc/architecture.md`の「5.5 Block」節: `DocUnit.kind`（10種）→`Block.kind`（6種）への再分類の詳細と対応表。6節の`mainCode/stage7/stage7.py`の項も参照。
- `doc/architecture/stage6.md`: `render_units_to_pdfs`が受け取る`units`を用意する工程(6)`postprocess`（`write_translated_pages`による書き出し）の詳細。
