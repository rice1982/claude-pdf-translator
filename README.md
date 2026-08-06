# PDF学術論文・書籍 構造化・翻訳ツール マニュアル

このドキュメントは、PDF学術論文および書籍を解析してタグ付き構造化Markdownを
生成し、さらに日本語訳と3種類のPDF（対訳版／英語版／日本語版）を
出力するまでの一連のツールについて説明します。


## 第1章 概要

このソフトウェアは、英語の学術論文や書籍のPDFを入力として、以下の処理を
1つのコマンドで実行します。

1. PDF解析（MinerUによるレイアウト解析）
   本文・図表・数式・メタ情報（タイトル／著者／所属）を分離抽出し、
   1文ごとにユニークID（例: `[P1-S2-abstract-S2]`）を付与した
   ページ別Markdownを生成します。
   図・表・数式領域はPNG画像として切り出します。

2. 日本語訳
   翻訳エンジンとして DeepL API を使用します。
   DeepL API 使用時、APIキー未設定・文字数上限到達・通信エラーなどが
   発生した場合はエラーとして処理を終了します。

3. ページ指定・章指定処理
   長大なPDFや書籍向けに、処理対象の「物理ページ範囲（--start, --end）」、
   「印刷ページラベル範囲（--start-label, --end-label）」、または
   「章（--chapter）」を指定して、必要な箇所のみを高速かつ安全に処理できます。

4. PDF生成
   以下3種類のPDFを自動生成します。
   - 対訳版PDF（左：英語 / 右：日本語、文単位で行を揃えて対応）
   - 英語版のみPDF
   - 日本語版のみPDF

   文中のインライン数式（例: p(y|x)）はKaTeXにより実際の数式として
   描画されます（生のLaTeXテキストのままにはなりません）。
   参考文献（References）セクションは、著者名等の誤翻訳を防ぐため
   翻訳対象から除外され、原文のまま出力されます。


## 第2章 事前準備（初回のみ）

### 2-1. Python仮想環境と必要ライブラリ

プロジェクトフォルダ直下に venv（仮想環境）がすでに用意されています。
未構築の場合は以下を実行してください。

```
python -m venv venv
venv\Scripts\pip install -r requirements.txt
```

requirements.txt には、PDF解析用（pymupdf, mineru 等）と翻訳・PDF
生成用（python-dotenv, deepl, playwright）の両方のライブラリが
含まれています。

### 2-2. Playwright（PDF生成エンジン）のブラウザ本体の導入

PDF生成にはPlaywright経由のChromiumを使用します。ライブラリ本体を
pipで入れただけではブラウザの実体が無いため、初回のみ以下を実行して
Chromiumをダウンロードしてください（数百MB程度）。

```
venv\Scripts\python -m playwright install chromium
```

※ 数式描画用のKaTeXは `vendor\katex` フォルダにフォント込みで
  同梱済みのため、追加のダウンロードは不要です。

### 2-3. DeepL APIキーの設定（必須）

1. DeepLアカウント（無料プランで可）を作成し、APIキーを発行します。
   https://www.deepl.com/ja/pro-api

2. プロジェクトフォルダ直下に `.env` という名前のファイルを作成し、
   以下の1行を記述します（実際のキーに置き換えてください）。

   ```
   DEEPL_API_KEY="発行されたAPIキー:fx"
   ```

   ※ 無料プランのキーは末尾が `:fx` になります。
   ※ `.env` は .gitignore で除外済みのため、Gitには含まれません。

   APIキーが未設定の場合、翻訳処理はエラーとして終了します。


## 第3章 使い方（1コマンド実行）

### 3-1. 基本の実行方法

まずプロジェクトフォルダ直下で仮想環境を有効化します（コマンドプロンプトの場合）。

```
venv\Scripts\activate
```

PowerShellの場合は以下を使用してください。

```
venv\Scripts\Activate.ps1
```

有効化すると、プロンプトの先頭に `(venv)` が表示されます。以降は以下の形式で実行します。

```
python translate_paper.py <PDFファイルのパス> <出力先ディレクトリ> [オプション]
```

`<出力先ディレクトリ>` が既存の場合は中身が上書き再生成されます。
存在しない場合は自動的に作成されます。

※ 仮想環境を有効化せずに実行したい場合は、`venv\Scripts\python.exe` を直接指定しても同様に動作します（本マニュアルの他のコマンド例も同じ考え方で読み替え可能です）。

### 3-2. 利用可能なオプション（CLI引数）

- `--chapter <章番号>`
  処理対象の章を指定します（例: `--chapter 1`）。
  PDFの目次（TOC/Outline）から該当する章の物理ページ範囲を自動解析して処理します。

- `--start <開始物理ページ番号>`
  処理を開始するPDFの物理ページ番号（1始まりの絶対インデックス）を指定します。

- `--end <終了物理ページ番号>`
  処理を終了するPDFの物理ページ番号（1始まりの絶対インデックス）を指定します。

- `--start-label <開始印刷ページラベル>`
  本に印刷されている見た目上のページ表記（例: "cov", "i", "xviii", "36" 等）で
  開始ページを指定します。

- `--end-label <終了印刷ページラベル>`
  本に印刷されている見た目上のページ表記（例: "41" 等）で終了ページを指定します。

※ `--start-label` / `--end-label` または `--start` / `--end` が明示的に指定された場合は、
  `--chapter` よりも優先されます。

### 3-3. 実行具体例

- 論文サンプル（2ページ）を実行:
  ```
  venv\Scripts\python translate_paper.py input\sample0.pdf output\sample0
  ```

- 書籍（sample3.pdf）の第1章のみを処理:
  ```
  venv\Scripts\python translate_paper.py input\sample3.pdf output\sample3_ch1 --chapter 1
  ```

- 書籍（sample3.pdf）の物理ページ 67〜72 を指定して処理:
  ```
  venv\Scripts\python translate_paper.py input\sample3.pdf output\sample3_p67_72 --start 67 --end 72
  ```

- 書籍（sample3.pdf）の印刷ページラベル "55"〜"60" を指定して処理（物理67〜72に相当）:
  ```
  venv\Scripts\python translate_paper.py input\sample3.pdf output\sample3_label55_60 --start-label 55 --end-label 60
  ```

### 3-4. 実行結果

指定した出力先ディレクトリの直下に、以下のファイルが生成されます。

```
page_01_en.md, page_02_en.md, ...   … ページ別タグ付きMarkdown（原文）
images/                              … 抽出された図・表・数式のPNG画像
paper_bilingual.pdf                  … 対訳版PDF
paper_en.pdf                         … 英語版のみPDF
paper_ja.pdf                         … 日本語版のみPDF
```

### 3-5. 処理時間の目安

PDF解析（MinerU）はCPU実行のため、1PDFあたり数分〜十数分程度
かかることがあります（範囲指定・章指定を行うことで大幅に短縮可能です）。
翻訳・PDF生成はページ数・文数に応じて数十秒〜数分程度です。
処理中はターミナルに進捗状況（「[DeepL] P1-S5 を翻訳中...」等）が表示されます。


## 第4章 ファイル構成（参考）

```
pdf_processor.py         … PDF解析パイプラインの起点（MinerU実行〜Markdown出力）
pdf_mineru_runner.py     … MinerU実行ラッパー
pdf_structure_analyzer.py… 構造解析（本文/図表/数式/見出し等への分類・目次解析）
pdf_text_utils.py        … 文分割・見出し判定・ページラベル変換等のユーティリティ
pdf_document_builder.py  … ページ別Markdownの組み立て・画像保存
pdf_models.py            … PDF解析パイプライン共通のデータ構造

translate_paper.py       … 本マニュアルの主役。CLI引数パース・パイプライン統合・実行エントリポイント
md_tag_parser.py         … タグ付きMarkdownの解析、参考文献セクションの除外
math_protection.py       … インライン数式を翻訳エンジンから保護するモジュール
deepl_translator.py      … DeepL APIによる文脈付き翻訳
pdf_renderer.py          … HTML/CSS + Playwright + KaTeXによるPDF生成
katex_assets.py          … 数式描画用KaTeXアセットの読み込み
translation_models.py    … 翻訳・PDF生成パイプライン共通のデータ構造

vendor\katex\             … PDF内で数式を描画するためのKaTeX本体・フォント一式
input\sample0.pdf         … 動作確認・軽量テスト用サンプルPDF（論文抜粋）
input\sample1.pdf         … 動作確認用サンプルPDF（論文フルサイズ）
input\sample3.pdf         … 大型書籍テスト用サンプルPDF
```


## 第5章 トラブルシューティング

**Q. 書籍全体を処理しようとすると時間がかかりすぎる／トークンが切れる。**
A. `--chapter 1` や `--start-label` / `--end-label` オプションを使用して、必要な章や印刷ページ範囲に絞って処理を実行してください。DeepL APIの文字数上限にも注意してください。

**Q. 「DEEPL_API_KEY が未設定です」というエラーで処理が終了する。**
A. プロジェクトフォルダ直下に `.env` ファイルが無いか、`DEEPL_API_KEY` が正しく記述されていません。第2章2-3の手順に従ってAPIキーを設定してください。

**Q. PDFの日本語が文字化けする／表示されない（豆腐文字）。**
A. 通常はWindows標準の游ゴシック/メイリオ等にフォールバックするため発生しませんが、発生する場合はPlaywrightのChromiumが正しくインストールされているか（第2章2-2）を確認してください。

**Q. 前書き（ローマ数字ページ）がある書籍で、見た目のページ番号と出力ファイル番号がずれる。**
A. `--start` / `--end` はPDF全体の絶対物理ページ数を指定する仕様です。本に印刷されている見た目通りの数字（例: 表紙 "cov"、前書き "i", "ii"、本文 "36" 等）で範囲指定したい場合は、`--start-label 55 --end-label 60` を使用してください。
