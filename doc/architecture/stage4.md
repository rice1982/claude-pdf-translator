# stage4.md — `mainCode/stage4/stage4.py`

## 1. 概要

`mainCode/stage4/stage4.py`は工程(4)「翻訳実行」を担う。

やっていることを一言でいうと、工程(3)が用意した翻訳直前のDocUnit列と文書文脈を受け取り、DeepL APIを実際に呼び出して、1unitごとの生の翻訳応答（数式プレースホルダが残ったままの、復元前のテキスト）を`unit.tag`をキーにした辞書で返す処理である。

DeepLを呼び出す翻訳バックエンドの実装と、環境変数からAPIキーを読んでそれを呼ぶ薄い入口を1ファイルにまとめている。数式スパンの保護は工程(3)の終わりに`stage3.protect_units`が既に済ませているため、バックエンドは`unit.protected_en_text`/`unit.math_spans`をそのまま使う（自身ではprotectを呼ばない）。数式プレースホルダの復元（`apply_restore`）は工程(5)の責務のため`mainCode/stage5/stage5.py`にある。

- **DeepL**（`call_deepl`）: 各文の翻訳リクエストに、文書全体の文脈（タイトル＋Abstract）と直前2〜3文の原文を`context`パラメータとして渡し、代名詞や専門用語の表記揺れを抑える。要APIキー・実課金。文単位で逐次送信する。
- **入口**（`translate_units`）: 環境変数`DEEPL_API_KEY`からAPIキーを読み、`call_deepl`へ委譲するだけの薄い関数。翻訳エンジンとの実際の送受信内容を記録として保存する機能（`cache/`配下へのスナップショット保存）はこの関数の責務ではなく、呼び出し元（`whole_pipeline.main`）が担う。

## 2. 構成要素（3グループ）

- **共通型（データ構造）**: `TranslationBackendError`（例外）・`RawTranslationResult`（dataclass。バックエンドの戻り値をこの型に揃える）。
- **DeepL（関数）**: `call_deepl`・`_build_context_text`（DeepLの`context`パラメータ文字列の組み立て）。`call_deepl`の翻訳対象unit絞り込みは`shared.filter_translatable_units`（`mainCode/shared/shared.py`。工程(4)〜(6)の共通フィルタ）に委譲する。
- **入口（関数）**: `translate_units`。

## 3. translate_units()の処理フロー

### 3.1 関数依存関係図

```
translate_units()
└── call_deepl()
      └── _build_context_text()                          … unitごとに1回
```

### 3.2 実行フローの設計

`translate_units`は環境変数`DEEPL_API_KEY`を`os.environ`から取得して`call_deepl`へ渡し、その戻り値をそのまま返すだけの薄いラッパーである。APIキーが未設定（`None`）の場合の失敗判定は`call_deepl`自身の責務であり、`translate_units`はキーの有無を見ない。

`call_deepl`は`shared.filter_translatable_units`で翻訳対象unitへ絞り込んだ上で、1文ずつDeepLへ逐次送信する。各リクエストには`_build_context_text`が組み立てた「文書全体の文脈＋直前数文の原文履歴」を`context`パラメータとして添える。送信済みの原文（protect済み）は`history`へ追記され、以降の文の文脈になる。

### 3.3 エラー処理方針

翻訳継続が不可能な失敗（APIキー未設定・通信エラー・DeepL側の例外等）は`TranslationBackendError`に正規化して呼び出し元へ伝える。`deepl.DeepLException`は再試行せず即座に`TranslationBackendError`へ正規化する。

## 4. 構成要素リファレンス

各項目は先頭に`種別`（関数／データ構造）を持つ。まず関数を3.1節の依存関係図と同じ順（入口`translate_units`を先頭に、そこから呼ばれる順）に並べ、その後にデータ構造（`RawTranslationResult`）を記載する。各項目の「グループ」は2節の3グループ（共通型／DeepL／入口）のどれに属するかを示す。`TranslationBackendError`は本体が`pass`のみの単純な例外クラスのため、独立した項目は設けず、送出する関数の項目内で言及するに留める。

### translate_units

- **種別**: 関数
- **グループ**: 入口
- **呼び出し元**: `mainCode/whole_pipeline/whole_pipeline.py`の`main()`（工程(3)`prepare_translation_input`の直後）
- **入力**: `units`（`DocUnit`のリスト）・`document_context`（文書全体の文脈文字列）・`log`（進捗ログコールバック）
- **出力**: `unit.tag`をキーにした`RawTranslationResult`辞書
- **処理内容**: 3節参照。環境変数`DEEPL_API_KEY`からAPIキーを読み、`call_deepl`へ委譲する。
- **テスト対象**: `TestTranslateUnitsEntry`が担当する（`translate_units`を実体のまま直接呼ぶ唯一のテストクラス。`call_deepl`自体はモックし、この関数の環境変数からのAPIキー取得・ログ出力のみを検証する）。`test_translate_units_reads_api_key_from_environ_and_delegates_to_deepl`で環境変数`DEEPL_API_KEY`から読んだキーが`call_deepl`へ渡ること、`test_translate_units_passes_none_api_key_when_environ_unset`で環境変数未設定時に`None`のまま`call_deepl`へ渡る（未設定エラーの判定は`call_deepl`の責務）ことを確認している。`call_deepl`自体の単体テストは`TestCallDeepl`を参照。全体テスト（`testCode/test_whole_pipeline.py`）・統合テスト（`testCode/test_integration.py`）からもエンドツーエンドで検証されている。

### call_deepl

- **種別**: 関数
- **グループ**: DeepL
- **呼び出し元**: `translate_units()`
- **入力**: `units`（`DocUnit`のリスト）・`api_key`（`DEEPL_API_KEY`、未設定なら`None`）・`document_context`（文書全体の文脈文字列）・`log`
- **出力**: `unit.tag`をキーにした`RawTranslationResult`辞書（restore適用前）
- **処理内容**: `api_key`が空なら即座に`TranslationBackendError`。`shared.filter_translatable_units`で翻訳対象unitへ絞り込み、1文ずつ`deepl.Translator.translate_text`へ逐次送信する（`split_sentences="off"`・`preserve_formatting=True`）。各リクエストの`context`パラメータには`_build_context_text`が組み立てた「文書全体の文脈＋直前数文の原文履歴」を渡す。送信済みの原文（protect済み）は`history`へ追記され、以降の文の文脈になる。`deepl.DeepLException`は再試行せず即座に`TranslationBackendError`へ正規化する。`unit.ja_text`はこの時点では書き換えない（復元は工程(5)）。
- **テスト対象**: `TestCallDeepl`が担当する。`test_call_deepl_raises_when_api_key_missing`でAPIキー未設定時のエラーを、`test_call_deepl_protects_math_and_builds_context_history`で非翻訳対象unitの除外・protect済みテキストの送信・文脈履歴の積み上がりを、`test_call_deepl_wraps_deepl_exception_as_backend_error`で`deepl.DeepLException`が`TranslationBackendError`へ正規化されることを確認している。実DeepLを実際に呼ぶ検証は全体テスト（`test_run_pipeline_end_to_end_with_real_deepl`、CLAUDE.md「テスト・実行運用規定」項目3の許可4組み合わせに限定）が担う。

### _build_context_text

- **種別**: 関数
- **グループ**: DeepL
- **呼び出し元**: `call_deepl()`（unitごとに1回）
- **入力**: `document_context`（文書全体の文脈文字列）・`history`（それまでに送信した原文のリスト）
- **出力**: DeepLの`context`パラメータへ渡す文字列。連結結果が空なら`None`
- **処理内容**: `document_context`（あれば）と、`history`末尾`_CONTEXT_HISTORY_SIZE`（3）件を改行連結したものを、空行区切りで連結する。どちらも空なら`None`を返す（DeepLの`context`引数へそのまま渡せる）。
- **テスト対象**: `TestBuildContextText`の`test_build_context_text_only_keeps_last_three_history_entries`で3件超の履歴が直近3件へ切り詰められること（`call_deepl`経由の既存テストは履歴を最大2件しか作らず未検証だった）を、`test_build_context_text_returns_none_when_nothing_to_join`で両方空の場合に`None`を返すことを確認している。

### RawTranslationResult

- **種別**: データ構造
- **グループ**: 共通型
- **役割**: 1unit分の翻訳エンジン応答（restore適用前）を持ち回るdataclass。
- **フィールド**:

| フィールド | 型 | 役割 |
|---|---|---|
| `raw_text` | `str` | 翻訳エンジンの応答テキスト（数式プレースホルダ`__MATHn__`が残った状態） |
| `math_spans` | `list[str]` | `restore`でプレースホルダを復元するための、元の数式スパンのリスト |

- **使われ方**: `unit.tag`をキーにした辞書として`call_deepl`から返され、工程(5)の`apply_restore`（`mainCode/stage5/stage5.py`）が復元処理に使う。`whole_pipeline.main()`によって`cache/.../04_deepl_output/raw_deepl_results.json`としてそのままディスクにも書き出される。

## 5. 関連ドキュメント

- `doc/architecture.md` §2「パイプラインの7工程」: 7工程モデルの正データ。工程(4)が工程(2)と並ぶ実行必須の工程（外部の重い処理を伴う）である位置づけを説明する。
- `doc/architecture.md`の「5.4 翻訳実行の中間データ」節: `RawTranslationResult`が復元処理（`apply_restore`）に必要な情報だけを`DocUnit`を参照せずに完結して持つ設計の詳細。6節の`mainCode/stage4/stage4.py`の項も参照。
- `doc/architecture/stage5.md`: `RawTranslationResult`を受け取って`DocUnit.ja_text`へ復元する工程(5)`apply_restore`の詳細。
- `doc/architecture/shared.md`: `call_deepl`が使う`filter_translatable_units`の実装詳細。
- `CLAUDE.md`「テスト・実行運用規定」項目3・4: 実DeepLの許可されたテスト実行組み合わせと、未保護インライン数式の許可リスト運用。
