# shared.md — `mainCode/shared/shared.py`

## 1. 概要

`mainCode/shared/shared.py`は、複数工程から共通して呼ばれるデータ構造、および全工程から参照される末端ユーティリティ（数式プレースホルダ変換・ログ出力）を置くファイルである。工程(2)専用の中間表現（`TextBlockElement`等）や工程(7)専用の表示単位（`Block`）のように、単一の工程内だけで完結し他工程から一切参照されない型は、複数工程共有の境界にならないためこのファイルには置かず、その工程のモジュール（`mainCode/stage2/stage2.py`・`mainCode/stage7/stage7.py`）側に定義する。

このファイルに置かれる基準は「複数工程から本当に共有され、かつ他のどの`mainCode`モジュールにも依存しない末端であること」の2点。後者により、循環importを起こさずに複数の工程から共通して呼べる。

## 2. 構成要素（3グループ）

- **`DocUnit`（データ構造・関数）**: `DocUnit`（データ構造）と`filter_translatable_units`（関数）の2つ。`DocUnit`はロジックを持たず、工程(3)〜(7)を貫通して受け渡される「データの形」だけを定義する。`filter_translatable_units`は`DocUnit`のリストから`translatable=True`のものだけを列挙する共通フィルタで、工程(4)（`call_deepl`）・工程(5)（`apply_restore`）・工程(6)（複数関数）から呼ばれる。元は各工程ファイルにほぼ同一のロジックが個別に（`stage4`は`u.translatable`直書き、`stage5`/`stage6`は`getattr(u, "translatable", False)`という微妙な書き方の違いを持って）重複定義されていたが、3工程以上から使われる横断的なフィルタのため、shared.pyへ統合した。
- **数式保護〔退避・復元〕（関数）**（`MATH_RE`・`MATH_TOKEN_RE`・`_normalize_math_escape`・`protect`・`restore`）: インライン数式（`$...$`/`$$...$$`）をプレースホルダ（`__MATHn__`）へ退避・復元する純粋な文字列変換。`re`標準ライブラリ以外の依存を持たない。`protect`・`restore`はそれぞれ内部に`_replace`というネスト関数を持つ（`re.sub()`のコールバックとして使う、互いに独立した別々の関数）。`MATH_RE`・`MATH_TOKEN_RE`はモジュール定数のため独立した項目を設けず、`protect`/`restore`の項で言及する。
- **ログ出力（関数）**（`log`）: 標準出力への進捗メッセージ出力のみを行う、1行だけの極小関数。

## 3. 呼び出し関係

このファイル内での依存は、`protect`→`_replace`→`_normalize_math_escape`と、`restore`→`_replace`（`protect`内の同名関数とは別物）の2系統。

```
protect()
└── _replace()                   … マッチした数式スパンごとに1回（ネスト関数）
      └── _normalize_math_escape()

restore()
└── _replace()                   … マッチしたプレースホルダごとに1回（ネスト関数、protect内の同名関数とは別物）
```

他ファイルからの呼び出し元は以下の通り（型のみ依存も含む）。

| シンボル | 呼び出し元 |
| --- | --- |
| `DocUnit` | `mainCode/stage3/stage3.py`・`mainCode/stage4/stage4.py`・`mainCode/stage5/stage5.py`・`mainCode/stage6/stage6.py`・`mainCode/stage7/stage7.py`・`mainCode/whole_pipeline/whole_pipeline.py` |
| `filter_translatable_units` | `mainCode/stage4/stage4.py`（`call_deepl`）・`mainCode/stage5/stage5.py`（`apply_restore`）・`mainCode/stage6/stage6.py`（数式保護の複数関数） |
| `protect` | `mainCode/stage3/stage3.py`（`protect_units`・`normalize`）・`mainCode/stage6/stage6.py`（数式保護の複数関数） |
| `restore` | `mainCode/stage3/stage3.py`（`normalize`内部のラウンドトリップ）・`mainCode/stage5/stage5.py`（`apply_restore`）・`mainCode/stage6/stage6.py`（数式保護の複数関数） |
| `log` | `mainCode/stage1/stage1.py`・`mainCode/stage7/stage7.py`（`_log`としてimport）・`mainCode/whole_pipeline/whole_pipeline.py`（`_log`としてimport） |

`mainCode/stage4/stage4.py`・`mainCode/stage5/stage5.py`・`mainCode/stage6/stage6.py`の各関数は、`log`を直接importせず、`whole_pipeline.main()`から`log`コールバック引数として注入される（`translate_units(..., log=_log)`等）。

## 4. 構成要素リファレンス

各項目は先頭に`種別`（関数／データ構造／ヘルパークラス）を持つ。各項目の「グループ」は2節の3グループ（`DocUnit`／数式保護〔退避・復元〕／ログ出力）のどれに属するかを示す。`shared.md`は入口が無いため図順ではなく§2グループ順（`DocUnit`→数式保護→ログ出力）で記載する。データ構造は`DocUnit`のみ、ヘルパークラスは無い。`MATH_RE`・`MATH_TOKEN_RE`はモジュール定数のため独立した項目を設けない。

### DocUnit

- **種別**: データ構造
- **グループ**: `DocUnit`
- **役割**: タグ付きMarkdownの1行から解析した最小単位を表す、ロジックを持たない純粋なデータ構造。翻訳対象の文だけでなく画像参照・LaTeX・メタ情報など非翻訳要素も同じ型で表現する。工程(3)〜(7)と`mainCode/whole_pipeline/whole_pipeline.py`を貫通して受け渡される、このファイルで唯一の複数工程共有型。
- **フィールド**: 識別情報・本文/成果物・翻訳パイプライン用の内部状態の3グループに分かれる。

| フィールド | 型 | 役割 |
|---|---|---|
| `tag` | `str` | タグ文字列（例:`"P1-S1-1.introduction-S1"`）。行の一意識別・翻訳結果の突き合わせ・スナップショット保存キー |
| `kind` | `str` | `"title"` \| `"authors"` \| `"affil"` \| `"heading"` \| `"body_sentence"` \| `"caption_sentence"` \| `"equation_latex"` \| `"figure_image"` \| `"equation_image"` \| `"unknown"` |
| `page` | `int` | 物理ページ番号（1始まり） |
| `en_text` | `str`（既定`""`） | 原文（英語）。翻訳対象外の`kind`では空文字列のままか、LaTeXそのものが入る |
| `ja_text` | `str`（既定`""`） | 訳文（日本語）。`stage5.apply_restore`が書き込むまでは空文字列 |
| `image_rel_path` | `str \| None`（既定`None`） | `figure_image` / `equation_image`の場合の画像相対パス（例:`"images/fig_p2_1.png"`） |
| `translatable` | `bool`（既定`False`） | 翻訳エンジンへ送信する対象かどうか（`kind`に応じて`stage3`が設定） |
| `protected_en_text` | `str`（既定`""`） | `en_text`の数式スパンをプレースホルダへ退避した状態（`stage3.protect_units`が設定）。翻訳エンジンへ実際に送信するのはこちら |
| `math_spans` | `list[str]`（既定空リスト） | `protected_en_text`の`__MATHn__`を元の数式スパンへ復元するための対応表（`stage3.protect_units`が設定、`shared.restore`経由で`stage5.apply_restore`が使用） |

- **使われ方**: 工程(3)がタグ付きMarkdownから生成し、工程(4)が`ja_text`、`stage3.protect_units`が`protected_en_text`・`math_spans`を書き込む。工程(4)〜(6)は`filter_translatable_units`で`translatable`なものだけを処理し、工程(7)が`Block`へ再編成してPDF化する。専用テストは無く、工程(3)〜(7)の各テストを通じて間接的に妥当性が検証される。

### filter_translatable_units

- **種別**: 関数
- **グループ**: `DocUnit`
- **呼び出し元**: `mainCode/stage4/stage4.py`の`call_deepl`、`mainCode/stage5/stage5.py`の`apply_restore`、`mainCode/stage6/stage6.py`の数式保護の複数関数（`protect_confirmed_single_letter_leaks`・`_apply_replacement_finder`）
- **入力**: `units`（`DocUnit`のリスト）
- **出力**: `translatable=True`のunitだけを、元の順序を保ったまま列挙するジェネレータ
- **処理内容**: `(u for u in units if u.translatable)`。翻訳対象外のunit（`figure_image`等）を、工程(4)〜(6)の各処理から一律に除外するための共通フィルタ。
- **テスト対象**: `testCode/test_shared.py`（`filter_translatable_units`自身の入出力契約: 空リスト・`translatable`混在時のフィルタ結果と順序保持）。工程(4)〜(6)それぞれの入口関数（`translate_units`・`apply_restore`・`postprocess`）を通じた間接テストは、各工程別テストファイルを参照。

### protect

- **種別**: 関数
- **グループ**: 数式保護〔退避・復元〕
- **呼び出し元**: `mainCode/stage3/stage3.py`の`protect_units`・`normalize`、`mainCode/stage6/stage6.py`の数式保護の複数関数
- **入力**: `text`（本文中の数式スパンを含みうるテキスト）
- **出力**: `(置換後テキスト, 元の数式スパンのリスト)`。リストのインデックスがそのままプレースホルダ番号（`__MATHn__`のn）に対応する。
- **処理内容**: `MATH_RE`（`$$...$$`を優先しつつ`$...$`にもマッチする正規表現、`re.DOTALL`で複数行のディスプレイ数式にも対応）で数式スパンを検出し、`_normalize_math_escape`を通した上でプレースホルダへ置き換える。数式を含まないテキストはそのまま返す（spansは空リスト）。閉じられていない単独の`$`は数式スパンとしてマッチせず、そのまま残る。
- **テスト対象**: `testCode/test_shared.py`（`protect`自身の入出力契約: 数式無し・閉じていない`$`・ディスプレイ数式の単一スパン捕捉・複数スパンの連番割当）、`testCode/test_stage3.py`（`protect_units`経由の基本ケース、`normalize`経由の実データラウンドトリップ・`\textless`/`\textgreater`正規化範囲の確認）。

### _replace（protect内）

- **種別**: 関数
- **グループ**: 数式保護〔退避・復元〕
- **呼び出し元**: `protect`内部のみ（`MATH_RE.sub()`のコールバックとして、マッチした数式スパンごとに呼ばれる）
- **入力**: `match`（`re.Match`オブジェクト。マッチした数式スパン1つ分）
- **出力**: そのマッチを置き換えるプレースホルダ文字列（`__MATHn__`）
- **処理内容**: マッチした数式スパン全体（`match.group(0)`）に`_normalize_math_escape`を適用した上で、`protect`のローカル変数`spans`へ追記する（クロージャによる副作用）。追記直後のインデックス（＝末尾の要素番号）がそのままプレースホルダ番号になる。
- **テスト対象**: 専用の直接テストは無い。`protect`経由（`testCode/test_shared.py`）で間接的に検証される。

### _normalize_math_escape

- **種別**: 関数
- **グループ**: 数式保護〔退避・復元〕
- **呼び出し元**: `protect`内部のみ（他のどのファイルからもimportされない、`protect`専用のヘルパー）
- **入力**: `span`（マッチした数式スパン1つ分の文字列）
- **出力**: 正規化後の文字列
- **処理内容**: MinerUが数式中に出力する`\textless`/`\textgreater`（テキストモード用の比較記号エスケープで、KaTeXは解釈できない）を`<`/`>`へ正規化する。
- **テスト対象**: 専用の直接テストは無い。`protect`経由（`test_shared.py`）、および`mainCode/stage3/stage3.py`の`normalize`（同じ正規化を`\textless`/`\textgreater`が数式スパンの外側では変更されないことまで確認する`testCode/test_stage3.py`のテスト）を通じて検証される。

### restore

- **種別**: 関数
- **グループ**: 数式保護〔退避・復元〕
- **呼び出し元**: `mainCode/stage3/stage3.py`の`normalize`（内部で`protect`と対にしたラウンドトリップとして使用）、`mainCode/stage5/stage5.py`の`apply_restore`（工程(5)「翻訳後処理」の唯一のステップ）、`mainCode/stage6/stage6.py`の数式保護の複数関数
- **入力**: `text`（`__MATHn__`プレースホルダを含みうるテキスト、通常は翻訳エンジンの応答）・`spans`（`protect`が返した元の数式スパンのリスト）
- **出力**: プレースホルダを元の数式スパンへ復元したテキスト
- **処理内容**: `MATH_TOKEN_RE`で`__MATHn__`を検出し、対応する`spans[n]`へ置き換える。プレースホルダが存在しないテキストはそのまま返す。番号`n`が`spans`の範囲外の場合はプレースホルダ文字列をそのまま残す（翻訳エンジンがプレースホルダ番号を破損させた場合の安全策）。同一番号のプレースホルダが複数回出現した場合は、出現箇所全てが対応する数式スパンへ置換される（翻訳エンジンが言い回し上プレースホルダを正当に複数回参照するケースを想定した仕様であり、不具合ではない）。
- **テスト対象**: `testCode/test_shared.py`（`restore`自身の入出力契約: プレースホルダ無し・同一番号の複数回出現）、`testCode/test_stage5.py`（`apply_restore`経由の基本ケース、プレースホルダ番号が範囲外の場合のフォールバック）、`testCode/test_stage3.py`（`normalize`経由の実データラウンドトリップ）。

### _replace（restore内）

- **種別**: 関数
- **グループ**: 数式保護〔退避・復元〕
- **呼び出し元**: `restore`内部のみ（`MATH_TOKEN_RE.sub()`のコールバックとして、マッチしたプレースホルダごとに呼ばれる。`protect`内の同名関数`_replace`とは別物）
- **入力**: `match`（`re.Match`オブジェクト。`__MATHn__`にマッチしたプレースホルダ1つ分）
- **出力**: 対応する元の数式スパン文字列（`spans[n]`）。番号`n`が`spans`の範囲外の場合はプレースホルダ文字列をそのまま返す（`match.group(0)`）
- **処理内容**: マッチしたプレースホルダ番号（`match.group(1)`）を`int`に変換し、`restore`の引数`spans`の対応する要素を返す。範囲外の場合のフォールバックは、翻訳エンジンがプレースホルダ番号を破損させた場合の安全策。
- **テスト対象**: 専用の直接テストは無い。`restore`経由（`testCode/test_shared.py`の範囲外インデックスのフォールバックケースを含む）で間接的に検証される。

### log

- **種別**: 関数
- **グループ**: ログ出力
- **呼び出し元**: `mainCode/stage1/stage1.py`、`mainCode/stage7/stage7.py`・`mainCode/whole_pipeline/whole_pipeline.py`（いずれも`_log`としてimport）。`mainCode/stage4/stage4.py`・`mainCode/stage5/stage5.py`・`mainCode/stage6/stage6.py`へは`whole_pipeline.main()`からコールバック引数として注入される。
- **入力**: `message`（出力する文字列）
- **出力**: なし（副作用のみ）
- **処理内容**: `print(message, flush=True)`の薄いラッパー。
- **テスト対象**: 専用テストは無い。print文の副作用は通常テスト対象としない。

## 5. 関連ドキュメント

- `doc/architecture.md` §2「パイプラインの7工程」: 7工程モデルの正データ。`protect`/`restore`が工程(3)の仕上げ・工程(5)・工程(6)の三者から共有される位置づけを説明する。
- `doc/architecture.md`の「5. データ構造」節: `DocUnit`と、単一工程専用のため別ファイルに定義されている型（PDF解析側・`Block`）との境界の説明。6節の`mainCode/shared/shared.py`の項も参照。
- `CLAUDE.md`「テスト・実行運用規定」項目8: `testCode/test_shared.py`が専用テストファイルとして存在する理由（特定の工程の役割に紐づかない`shared.py`自身の入出力契約を直接対象とする）。
