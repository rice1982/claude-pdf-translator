# stage5.md — `mainCode/stage5/stage5.py`

## 1. 概要

`mainCode/stage5/stage5.py`は工程(5)「翻訳後処理（数式復元）」を担う、パイプライン中もっとも小さいファイルである。

やっていることを一言でいうと、工程(4)が返す`raw_results`（`unit.tag`→`raw_text`＋`math_spans`の辞書。プレースホルダ付きの翻訳結果であり、この時点では`units`自体はまだ書き換えられていない）を受け取り、`raw_text`中のプレースホルダを`math_spans`で元の数式へ戻して、その結果を初めて`unit.ja_text`へ書き込む処理である。

インライン数式（`$...$`/`$$...$$`）をプレースホルダ（`__MATHn__`）へ退避・復元する仕組みそのもの（`protect`/`restore`）は`mainCode/shared/shared.py`側にある。このファイルが担うのはそのうち「復元」だけであり、それが工程(5)唯一のステップになる。このファイルに独自`class`定義は無い。

- **入口**（`apply_restore`）: `call_deepl`の戻り値（`RawTranslationResult`。工程(4)、`mainCode/stage4/stage4.py`）を受け取り、数式プレースホルダを復元して`unit.ja_text`へ書き込む、工程(5)全体を代表する単一の入出力を持つ関数。翻訳エンジンへの再通信を伴わない決定的な処理。

翻訳後に残った未保護の数式らしき断片の自動保護・翻訳済みMarkdownの書き出しは、このファイルの責務ではなく工程(6)「数式保護」（`mainCode/stage6/stage6.py`）が担う。

## 2. 構成要素

このファイルに独自`class`定義は無い。関数は`apply_restore`（入口）1つのみで、グループ分けするほどの規模もない。翻訳対象unitの絞り込み（`filter_translatable_units`）・数式復元（`restore`）はいずれも`mainCode/shared/shared.py`側の共通関数に委譲しており、このファイル自身には内部ヘルパーが無い。

## 3. apply_restore()の処理フロー

### 3.1 関数依存関係図

このファイル内で完結する中間ステップは無いため、依存関係図は省略する。`apply_restore`は、`shared.filter_translatable_units`で翻訳対象unitへ絞り込んだ後、unitごとに`shared.restore`を直接呼ぶだけの1ループで完結する。呼び出す2関数（`filter_translatable_units`・`restore`）はどちらも`mainCode/shared/shared.py`側にあり、このファイル自身が定義する関数ではない（詳細は`doc/architecture/shared.md`参照）。

### 3.2 実行フローの設計

`apply_restore`の処理は、単純な1ループで完結する。まず`shared.filter_translatable_units`で翻訳対象unitだけに絞り込む。次に、`unit.tag`をキーに`raw_results`（`call_deepl`が返した`RawTranslationResult`辞書）から`raw.raw_text`・`raw.math_spans`を取り出し、`shared.restore`へ渡す。最後に、復元後のテキストを`unit.ja_text`へ書き込む。

翻訳対象外のunitは工程(4)の`translate_units`自体に送信されず、`raw_results`にキーを持たない。そのため、絞り込みを先に行わないと`KeyError`になる。

### 3.3 エラー処理方針

`apply_restore`自身は例外を捕捉・変換しない。`raw_results`に翻訳対象unitのタグが欠けている場合は`KeyError`がそのまま呼び出し元（`whole_pipeline.main()`）へ伝播する（工程(4)`translate_units`が全翻訳対象unit分の結果を返すことを前提としており、この前提が崩れた場合は早期に失敗させる設計）。

## 4. 構成要素リファレンス

（種別はすべて「関数」。データ構造・ヘルパークラス・独自の`class`定義は無い。このファイルの関数は`apply_restore`のみのため全件記載済み。）

### apply_restore

- **種別**: 関数
- **グループ**: 入口
- **呼び出し元**: `mainCode/whole_pipeline/whole_pipeline.py`の`main()`（工程(4)`translate_units`の直後。cache/への記録保存を直後に挟む必要があるため、結合用のラッパーは介さず直接呼ぶ）
- **入力**: `units`（`DocUnit`のリスト、書き換え対象）・`raw_results`（`translate_units`が返した、restore適用前の`RawTranslationResult`辞書）
- **出力**: なし（副作用として`unit.ja_text`を書き換える）
- **処理内容**: 3節参照。
- **テスト対象**: `testCode/test_stage5.py`が担当する。

  `test_apply_restore_matches_cached_deepl_output`は、`cache/sample0_full/real_deepl_output_<タイムスタンプ>/`配下の`04_deepl_output/raw_deepl_results.json`と`05_restored/units_raw.json`（`_find_latest_real_deepl_cache`が最新のタイムスタンプを自動選択する）を読み込み、DeepLを一切呼ばずに`apply_restore`の出力が、独立に凍結された`ja_text`と一致することを検証する。テスト名にあえて"real_deepl"を含めていないのは、無課金のテストでありCLAUDE.mdが定める実DeepL実行前確認ルールの対象外であることを示すためである。このキャッシュは、実DeepL版の全体テスト（`test_run_pipeline_end_to_end_with_real_deepl`）の実行、または人間による本実行時に生成される。未生成の場合、このテストはスキップされる。

  加えて、`whole_pipeline._write_restore_snapshot`が「`apply_restore`の直後・工程(6)の保護処理より前」というタイミングでスナップショットを書き出しているかを確認する回帰テストも、検証している役割が工程(5)のスコープに収まるためここに置かれている（CLAUDE.md「テスト・実行運用規定」項目8参照）。

  全体テスト（`testCode/test_whole_pipeline.py`）・統合テスト（`testCode/test_integration.py`）からもエンドツーエンドで検証されている。

## 5. 関連ドキュメント

- `doc/architecture.md` §2「パイプラインの7工程」: 7工程モデルの正データ。工程(5)が工程(4)の出力を受けてDeepLへの再通信なしに完結する決定的な処理である位置づけを説明する。
- `doc/architecture/stage4.md`: `apply_restore`が受け取る`RawTranslationResult`の定義元（工程(4)の型）。
- `doc/architecture/shared.md`: `filter_translatable_units`/`restore`の実装詳細（`apply_restore`が委譲する先）。
- `doc/architecture/stage6.md`: `apply_restore`の後段を担う工程(6)「数式保護」（未保護数式の自動保護・書き出し）。
