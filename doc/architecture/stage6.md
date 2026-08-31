# stage6.md — `mainCode/stage6/stage6.py`

## 1. 概要

`mainCode/stage6/stage6.py`は工程(6)「数式保護」を担う。

やっていることを一言でいうと、工程(5)（`apply_restore`）で`unit.ja_text`まで書き込み終えたDocUnit列を受け取り、数式として保護されないまま翻訳を通過し日本語訳に半角で残った数式らしき断片を、再翻訳を挟まず決定的な文字列処理だけで補足的に`$...$`保護（地の文に裸で残った1文字変数を新たに囲い、既存スパンに隣接する断片を1つのスパンへ統合）したうえで、その結果をページ別の翻訳済みタグ付きMarkdown（`page_XX_ja.md`／`page_XX_en.md`）として書き出す処理である。

入口`postprocess`は、下の3種類の後処理を決まった順で呼ぶだけの薄いオーケストレーターである（実行順とその根拠は§3.2参照。断片統合が関数2本に分かれるため、実行上は4ステップになる）。翻訳エンジンとの送受信内容の記録保存（`cache/`配下へのスナップショット保存）はこの関数の責務ではなく、呼び出し元の`whole_pipeline.main`が担う。

インライン数式（`$...$`/`$$...$$`）のプレースホルダ退避・復元の仕組み（`protect`/`restore`）自体は`mainCode/shared/shared.py`にある（工程(3)の仕上げである`stage3.protect_units`/`normalize`と、この工程(6)側の複数関数の双方が、互いをimportし合うことなく使うため）。このファイルに独自`class`定義は無い。

- **単体文字変数の事後保護**（`find_untranslated_fragment_candidates`・`protect_confirmed_single_letter_leaks`）: 翻訳後の`ja_text`に半角のまま残った、`$...$`で保護されていない数式らしき断片をヒューリスティックに洗い出し（`find_untranslated_fragment_candidates`。パターンを列挙するのではなく「全角の訳文中に半角の連続が生き残っていたら怪しい」という統計的な網で拾うため、未検出の数式・固有名詞の誤検知の両方を含みうる）、そのうち半角アルファベット1文字だけの候補を自動的に`$...$`で保護する（`protect_confirmed_single_letter_leaks`）。「英字1文字 = 値」形式（例:"t = 1"）や裸のギリシャ文字の自動保護は、翻訳前の工程(3)（`stage2.wrap_bare_letter_equals_expressions`/`wrap_bare_greek_letters`）で済ませてある。`find_untranslated_fragment_candidates`は、無課金の許可リスト突合テスト（`test_untranslated_fragment_candidates_against_cached_deepl_output`、`testCode/test_integration.py`）が実翻訳結果に対する未保護数式漏れの回帰検知に用いる測定器も兼ねる。
- **断片統合**（`merge_function_call_math_spans`・`merge_comparison_math_spans`）: 既存の`$...$`スパンの周囲に、識別子・括弧（`P($A$, $B$)`）や比較演算子付きの断片（`$K$ > 1`）が地の文として絡んでいる箇所を、1つの`$...$`（`$P(A, B)$`・`$K > 1$`）へまとめ直す。同じ「隣接断片の吸収」という仕事を、関数呼び出し版（`merge_function_call_math_spans`）と比較演算子版（`merge_comparison_math_spans`）の2つの関数で行い、`postprocess`はこれらを別々のステップとして順に実行する。
- **書き出し**（`write_translated_pages`）: 上記の保護・統合を反映した最終状態を、`page_*_en.md`と同じタグ形式で`page_*_ja.md`（および`page_*_en.md`）へ書き出す。`stage3.parse_page_file`の逆変換にあたるが、タグ書式の実装を共有していないため、実行順（工程6の末尾）に合わせてこのファイルに置かれている。

## 2. 構成要素（6グループ）

このファイルに独自`class`定義は無い。

- **書き出し（関数）**: `write_translated_pages`。
- **単体文字変数の事後保護（関数）**: `find_untranslated_fragment_candidates`・`_strip_unpaired_paren`・`_isolated_token_re`（内部ヘルパー）・`protect_confirmed_single_letter_leaks`。
- **断片統合の共通ヘルパー（関数）**: `_apply_ja_detected_replacements`・`_apply_replacement_finder`（`merge_function_call_math_spans`/`merge_comparison_math_spans`の両方が使う「検出→適用→ログ出力」の共通骨格）。
- **関数呼び出し断片の統合（関数）**: `_find_function_call_math_replacements`・`merge_function_call_math_spans`。
- **比較演算子断片の統合（関数）**: `_find_comparison_math_replacements`・`merge_comparison_math_spans`。
- **入口（関数）**: `postprocess`。

## 3. postprocess()の処理フロー

### 3.1 関数依存関係図

```
postprocess()
├── protect_confirmed_single_letter_leaks()
│     ├── find_untranslated_fragment_candidates()      … unitごとに1回
│     ├── _strip_unpaired_paren()
│     └── _isolated_token_re()
├── merge_function_call_math_spans()
│     └── _apply_replacement_finder()
│           ├── _find_function_call_math_replacements()
│           └── _apply_ja_detected_replacements()
├── merge_comparison_math_spans()
│     └── _apply_replacement_finder()
│           ├── _find_comparison_math_replacements()
│           └── _apply_ja_detected_replacements()
└── write_translated_pages()
```

（`shared.filter_translatable_units`（`mainCode/shared/shared.py`）は`protect_confirmed_single_letter_leaks`・`_apply_replacement_finder`のいずれからも呼ばれる横断的な絞り込みヘルパーだが、`protect`/`restore`同様このファイル外の依存のため、上記の木からは省略している。詳細は`doc/architecture/shared.md`参照。）

### 3.2 実行フローの設計

`postprocess`は4ステップを固定順序で直接呼ぶだけの薄いオーケストレーターで、結合用の中間関数は無い。順序には意味があり、(1)単体文字変数の自動保護（テキストを実際に書き換える最初のステップ）→(2)(3)断片統合（ステップ(1)で新たに保護されたスパンも統合対象になりうるため、その後に実行）→(4)最終状態の書き出し、という積み上げになっている。「英字1文字 = 値」形式（例:"t = 1"）や裸のギリシャ文字の自動保護は翻訳前の工程(3)（`stage2.wrap_bare_letter_equals_expressions`/`wrap_bare_greek_letters`）で済ませてあるため、この工程では扱わない。(2)(3)の断片統合2関数は、検出対象の正規表現とログメッセージ文言だけが異なり、「ja_textで検出→ja_textに必ず適用→en_textには同じ文字列がそのまま見つかった場合のみ適用→ログ出力」という骨格を`_apply_replacement_finder`に共通化している。

### 3.3 エラー処理方針

`postprocess`自身は例外を捕捉・変換しない。各保護関数はいずれも例外を送出せず、行った自動保護を`log`経由の情報（`[情報]`）メッセージとして出力するのみで処理を止めない（ヒューリスティックであるため、誤検知・見逃しの可能性を前提に、最終判断は人間が出力PDF・Markdownを目視確認する運用としている。CLAUDE.md「テスト・実行運用規定」項目4参照）。

## 4. 構成要素リファレンス

各項目は先頭に`種別`（関数／データ構造／ヘルパークラス）を持つ。このファイルの構成要素はすべて関数で、3.1節の依存関係図と同じ順（`postprocess`を先頭に、各ステップの子関数を深さ優先でたどる順）に並べている。各項目の「グループ」は2節の6グループ（書き出し／単体文字変数の事後保護／断片統合の共通ヘルパー／関数呼び出し断片の統合／比較演算子断片の統合／入口）のどれに属するかを示す。データ構造・ヘルパークラス・独自の`class`定義は無い。

### postprocess

- **種別**: 関数
- **グループ**: 入口
- **呼び出し元**: `mainCode/whole_pipeline/whole_pipeline.py`の`main()`（工程(5)`apply_restore`の直後。他のどの工程とも同じパターンで結合用のラッパーを介さず直接呼ぶ）
- **入力**: `units`（`apply_restore`適用済みの`DocUnit`の列）・`output_dir`（`write_translated_pages`の書き出し先）・`log`（ログメッセージを受け取るコールバック、既定`print`）
- **出力**: `write_translated_pages`が書き出したファイルパスの一覧（`page_XX_en.md`/`page_XX_ja.md`）
- **処理内容**: 3節参照。
- **テスト対象**: `test_postprocess_protects_leaked_letter_and_writes_pages`で、`apply_restore`適用済みのunitsを受け取り、未保護のまま残った単体アルファベット変数（"z"）を`$...$`保護した上で`page_XX_en.md`/`page_XX_ja.md`を実際に書き出しその戻り値を返すことを、`test_postprocess_runs_single_letter_protection_before_comparison_merge`で、ステップ1（単体アルファベット保護）→ステップ2-3（断片統合）の順序が実際に意味を持つケース（右辺"K"が単体変数として保護されて初めて`merge_comparison_math_spans`が発火する）を、それぞれ直接検証している（後者はpostprocess全体としての順序依存で、個々のstep関数の単体テストでは検知できないためmutation testingを経て追加された）。各構成要素の単体テストはそれぞれのグループの項を参照。全体テスト（`testCode/test_whole_pipeline.py`）・統合テスト（`testCode/test_integration.py`）からもエンドツーエンドで検証されている。無課金の許可リスト突合テスト（`test_untranslated_fragment_candidates_against_cached_deepl_output`、`testCode/test_integration.py`）は、実際のDeepL翻訳結果に対して`find_untranslated_fragment_candidates`を適用し、未知の候補が出ないかを回帰確認する（CLAUDE.md「テスト・実行運用規定」項目4参照）。

### protect_confirmed_single_letter_leaks

- **種別**: 関数
- **グループ**: 単体文字変数の事後保護
- **呼び出し元**: `postprocess()`（ステップ1。既存スパンに隣接しない裸の断片を新たに`$...$`保護できる唯一のステップで、この関数群で最初に`en_text`/`ja_text`を書き換える。ステップ2-3の断片統合も`en_text`/`ja_text`を書き換えるが、対象は既存スパンに隣接する断片に限られる）
- **入力**: `units`（翻訳済み、`en_text`/`ja_text`設定済みの`DocUnit`のリスト。書き換え対象）・`log`
- **出力**: 情報メッセージ（`[情報]`始まり）のリスト（1件も無ければ空リスト）
- **処理内容**: 翻訳対象unitごとに`find_untranslated_fragment_candidates(unit.ja_text)`が返す候補を`_strip_unpaired_paren`で孤立した丸括弧を剥がし、半角アルファベット1文字だけ（`_SAFE_SINGLE_LETTER_RE`）に絞る。`_isolated_token_re`が作る「半角英数字に前後を挟まれない孤立出現」パターンで、`protect`で既存の`$...$`スパンを退避した`en_text`・`ja_text`の両方を照合し、両方にマッチする場合のみ該当箇所を`$文字$`へ置換して`restore`で戻す。翻訳後も半角のまま生き残った1文字は数式変数である可能性が高い（実在の英単語なら通常の翻訳で全角の日本語に置き換わる）という判断に基づく。"DiT"・"NMS"のような複数文字トークンは実在の固有名詞・略語と区別できないため対象外とし、翻訳結果の目視確認（CLAUDE.md「テスト・実行運用規定」項目4）に委ねる。DeepLへの再翻訳はせず、確定済みテキストの同じ箇所に`$...$`を挿入するだけの後処理。
- **テスト対象**: `TestProtectConfirmedSingleLetterLeaks`が、基本的な置換（`_wraps_matching_occurrences`）、既存`$...$`スパン内部の同名文字を二重に囲まずスパン外のみ保護すること（`_does_not_touch_existing_math_spans`）、複数文字トークン"DiT"は対象外（`_skips_multi_char_tokens`）、孤立した前括弧・後括弧の除去（`_strips_unpaired_leading_paren`/`_strips_unpaired_trailing_paren`、実データ回帰）、"(i)"のような両括弧が揃った列挙記号は対象外（`_does_not_touch_paired_paren_enumerators`、実データ回帰）、別単語内に埋め込まれた同名文字（"context"の"x"）を壊さないこと（`_does_not_corrupt_letter_embedded_in_other_word`、mutation testing由来）、翻訳対象外unitは触らないこと（`_skips_non_translatable_units`）、を確認している。

### find_untranslated_fragment_candidates

- **種別**: 関数
- **グループ**: 単体文字変数の事後保護
- **呼び出し元**: `protect_confirmed_single_letter_leaks()`（unitごとに1回）。ほかに無課金の許可リスト突合テスト（`test_untranslated_fragment_candidates_against_cached_deepl_output`、`testCode/test_integration.py`）が、記録済みの実翻訳結果に対する未保護数式漏れの回帰検知のため直接呼ぶ。
- **入力**: `ja_text`（翻訳後の文字列）
- **出力**: 半角のまま残った数式らしき候補文字列のリスト
- **処理内容**: `MATH_RE.sub("", ja_text)`で保護済み`$...$`スパンを空文字列へ除去した上で（プレースホルダ化しないのは、隣接スパン間が半角スペース1つだけの場合にプレースホルダ同士が連結され誤検知の元になるため）、非空白の半角文字（またはギリシャ文字）で始まり同種で終わる区間を抽出する。各候補は、英字またはギリシャ文字を1つ以上含み（数字・記号だけの引用番号等を除外）、かつ4文字以下または`()=_^{}`等の数式記号を含む（長い固有名詞を除外）場合のみ採用する。パターンを列挙せず「全角の訳文中の半角の連続」という統計的な網で拾うため`t=1`・`O(n)`・モデル名なども一括で拾えるが、英字を含まない裸の数字1文字の数式漏れは検出できない（見出し番号・節番号・引用番号の誤検知を避けるための意図的なトレードオフ）。`protect_confirmed_single_letter_leaks`はこの出力のうち半角アルファベット1文字だけを自動保護に採用し、それ以外の候補（固有名詞・型番等の誤検知を含みうる）は上記の許可リスト突合テストと人間の目視確認に委ねる。
- **テスト対象**: `test_find_untranslated_fragment_candidates`（`ja_text`/`expected`をparametrize）で、半角"z"・"t=1"・スペース入り"t = 1"・"(ODE)"・"K"の検出、保護済み数式は非候補、日本語のみは空、ギリシャ文字"γ"の検出、記号を含まない場合の4文字（"ABCD"採用）と5文字（"ABCDE"不採用）の境界、を確認している。

### _strip_unpaired_paren

- **種別**: 関数
- **グループ**: 単体文字変数の事後保護
- **呼び出し元**: `protect_confirmed_single_letter_leaks()`（候補ごとに1回）
- **入力**: `token`（候補断片文字列）
- **出力**: 孤立した丸括弧を除いた文字列
- **処理内容**: `token`が`(`で始まり`)`を含まない場合は先頭の`(`を、`)`で終わり`(`を含まない場合は末尾の`)`を剥がす。地の文の丸括弧書き（例:"...patchifying x)."）で候補範囲に片方の括弧だけが巻き込まれた"(x"・"Z)"を1文字へ戻すためのもの。開き・閉じ両方が既に`token`内に揃っている場合（"(i)"等の列挙記号の可能性）は何もしない。
- **テスト対象**: 専用の直接テストは無い。`protect_confirmed_single_letter_leaks`経由（`test_protect_confirmed_single_letter_leaks_strips_unpaired_leading_paren`/`_strips_unpaired_trailing_paren`/`_does_not_touch_paired_paren_enumerators`）で間接的に検証される。

### _isolated_token_re

- **種別**: 関数
- **グループ**: 単体文字変数の事後保護
- **呼び出し元**: `protect_confirmed_single_letter_leaks()`（対象トークンごとに1回）
- **入力**: `token`（保護対象の確定済みトークン）
- **出力**: `re.Pattern`（`token`の孤立した出現にのみマッチ）
- **処理内容**: `(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])`を組み立てる。`\b`はUnicodeの結合文字クラスの都合上、直前直後が日本語のとき期待通り機能しないことがあるため使わず、前後が半角英数字でないことを明示的に確認する。
- **テスト対象**: 専用の直接テストは無い。`protect_confirmed_single_letter_leaks`経由（特に`test_protect_confirmed_single_letter_leaks_does_not_corrupt_letter_embedded_in_other_word`・`_wraps_matching_occurrences`）で間接的に検証される。

### merge_function_call_math_spans

- **種別**: 関数
- **グループ**: 関数呼び出し断片の統合
- **呼び出し元**: `postprocess()`（ステップ2）
- **入力**: `units`（翻訳済み`DocUnit`のリスト。書き換え対象）・`log`
- **出力**: 情報メッセージのリスト（1件も無ければ空リスト）
- **処理内容**: `_apply_replacement_finder`に`_find_function_call_math_replacements`とメッセージ文言を渡すだけの薄いラッパー。"P($A$, $B$)"のように関数呼び出し記法の引数だけが個別に`$...$`保護された箇所を、識別子・括弧ごと1つの`$...$`（"$P(A, B)$"）へまとめ直す。発火条件は、識別子が英字1〜4文字で`(`に空白なしで直接続き（"call ("のような通常の英語散文の丸括弧書きと区別する主な手がかり）、引数部に既存の`$...$`スパンを最低1つ含むこと（`$...$`を含まない通常の地の文には一切マッチしない）。引数部はカンマ区切り・条件付き確率の"|"区切り・プライム記号（`'`と`’`）にも対応する。検出は`ja_text`のみで行い、同一文字列が`en_text`にもあれば反映する（`en_text`上で同じ正規表現を独立実行すると、引数用文字クラスが緩いため無関係な後続英文まで巻き込みうる）。
- **テスト対象**: `TestMergeFunctionCallMathSpans`が、基本マージ（`_merges_matching_occurrences`）、条件付き確率の"|"（`_merges_conditional_probability_pattern`）、プライム記号2種（`_merges_prime_notation_pattern`）、引数部に`$...$`を含まない関数呼び出しは対象外（`_skips_when_no_protected_span`）、識別子と"("の間に空白がある場合は対象外（`_skips_when_space_before_paren`）、アンダースコア区切り複合識別子"D_KL"を壊さない（`_skips_underscore_joined_identifiers`）、5文字以上の英単語は対象外（`_skips_identifier_longer_than_four_chars`、mutation testing由来）、翻訳対象外unitは触らない（`_skips_non_translatable_units`）、`en_text`単独で正規表現を再実行しない（`_does_not_redetect_independently_on_en_text`、sample2.pdf実データ回帰）、`en_text`に一字一句一致しなければ`ja_text`だけ書き換える（`_leaves_en_text_untouched_when_not_found_verbatim`）、を確認している。

### _apply_replacement_finder

- **種別**: 関数
- **グループ**: 断片統合の共通ヘルパー
- **呼び出し元**: `merge_function_call_math_spans()`・`merge_comparison_math_spans()`
- **入力**: `units`・`find_replacements`（`ja_text`→`(元文字列, 置換後文字列)`のペアのリストを返す関数）・`message_for`（unit→ログメッセージ）・`log`
- **出力**: 情報メッセージのリスト
- **処理内容**: 翻訳対象unitごとに`find_replacements(unit.ja_text)`で置換ペアを求め、`_apply_ja_detected_replacements`で`ja_text`（必ず）と`en_text`（同一文字列がある場合のみ）へ適用する。変化があったunitに対し`message_for(unit)`を記録・ログ出力する。`merge_function_call_math_spans`/`merge_comparison_math_spans`の「検出→適用→ログ出力」の共通骨格で、両者の差分は検出関数とメッセージ文言のみ。
- **テスト対象**: 専用の直接テストは無い。`merge_function_call_math_spans`・`merge_comparison_math_spans`経由（`TestMergeFunctionCallMathSpans`・`TestMergeComparisonMathSpans`の全テスト）で間接的に検証される。

### _find_function_call_math_replacements

- **種別**: 関数
- **グループ**: 関数呼び出し断片の統合
- **呼び出し元**: `merge_function_call_math_spans()`が`_apply_replacement_finder`へ渡す（`ja_text`に対して1回）
- **入力**: `text`（検出対象の文字列。実際には`ja_text`）
- **出力**: `(元の文字列, まとめ直した文字列)`のペアのリスト
- **処理内容**: `_FUNC_CALL_WITH_MATH_RE`にマッチした各断片について、引数部に`$`が含まれる場合のみ、引数内の`$...$`を外して`$識別子(引数)$`という1つのスパンへ組み立てたペアを返す。テキストの書き換えはここでは行わない（呼び出し側で`ja_text`/`en_text`への反映方法を制御するため）。
- **テスト対象**: 専用の直接テストは無い。`merge_function_call_math_spans`経由（`test_merge_function_call_math_spans_merges_matching_occurrences`等）で間接的に検証される。

### _apply_ja_detected_replacements

- **種別**: 関数
- **グループ**: 断片統合の共通ヘルパー
- **呼び出し元**: `_apply_replacement_finder()`（unitごとに1回）
- **入力**: `en_text`・`ja_text`・`replacements`（`(元文字列, 置換後文字列)`のペアのリスト）
- **出力**: `(新しいen_text, 新しいja_text, 何らかの置換が行われたか)`のタプル
- **処理内容**: 各ペアについて、`ja_text`に元文字列があれば1回だけ置換し`changed`を立てる。`en_text`にも同じ元文字列がそのまま存在すれば同様に1回だけ置換する（無ければ触らない）。`ja_text`側での検出結果を、パターンとしてではなく具体的な文字列一致として`en_text`へ機械的に反映する設計（翻訳後の`ja_text`は大半が全角文字であることを前提にヒューリスティックの誤爆を抑えているのに対し、半角英字が続く`en_text`ではその前提が成り立たないため）。
- **テスト対象**: 専用の直接テストは無い。`merge_function_call_math_spans`・`merge_comparison_math_spans`経由（特に`test_merge_function_call_math_spans_leaves_en_text_untouched_when_not_found_verbatim`・`_does_not_redetect_independently_on_en_text`）で間接的に検証される。

### merge_comparison_math_spans

- **種別**: 関数
- **グループ**: 比較演算子断片の統合
- **呼び出し元**: `postprocess()`（ステップ3）
- **入力**: `units`（翻訳済み`DocUnit`のリスト。書き換え対象）・`log`
- **出力**: 情報メッセージのリスト（1件も無ければ空リスト）
- **処理内容**: `_apply_replacement_finder`に`_find_comparison_math_replacements`とメッセージ文言を渡す薄いラッパー。"$K$ > 1"のように既存の`$...$`スパン直後に比較演算子（`>` `<` `>=` `<=` `==` `!=` `≥` `≤` `≠`）と数値または別の`$...$`スパンが地の文として続く箇所を、1つの`$...$`（"$K > 1$"）へまとめ直す。右辺を「数値」または「既に`$...$`で保護済みのスパン」に限定しているため、"$X$ > many models"のような自然文（右辺が単語）や"$X$</sup>"のようなHTML上付きタグには一致しない。逆方向（値が先、スパンが後）は対象外。検出は`ja_text`のみで行う。
- **テスト対象**: `TestMergeComparisonMathSpans`が、基本マージ（`_merges_matching_occurrences`）、各種演算子`<`/`>=`/`<=`/`==`（`_merges_various_operators`）、右辺が別の`$...$`スパン（`_merges_when_right_side_is_math_span`）、右辺が単語なら対象外（`_skips_when_right_side_is_word`）、HTML上付きタグ"</sup>"を"<"と誤認しない（`_skips_html_sup_tag`）、翻訳対象外unitは触らない（`_skips_non_translatable_units`）、を確認している。ステップ1→3の順序依存は`test_postprocess_runs_single_letter_protection_before_comparison_merge`で検証されている。

### _find_comparison_math_replacements

- **種別**: 関数
- **グループ**: 比較演算子断片の統合
- **呼び出し元**: `merge_comparison_math_spans()`が`_apply_replacement_finder`へ渡す（`ja_text`に対して1回）
- **入力**: `text`（検出対象の文字列。実際には`ja_text`）
- **出力**: `(元の文字列, まとめ直した文字列)`のペアのリスト
- **処理内容**: `_COMPARISON_AFTER_MATH_RE`にマッチした各断片について、左辺スパンの中身と、比較演算子＋右辺（右辺に`$...$`があれば中身を展開）を連結し、`$左辺演算子右辺$`という1つのスパンへ組み立てたペアを返す。テキストの書き換えはここでは行わない。
- **テスト対象**: 専用の直接テストは無い。`merge_comparison_math_spans`経由（`test_merge_comparison_math_spans_merges_matching_occurrences`等）で間接的に検証される。

### write_translated_pages

- **種別**: 関数
- **グループ**: 書き出し
- **呼び出し元**: `postprocess()`（ステップ4。最後に実行）
- **入力**: `units`（翻訳済み`DocUnit`のリスト）・`output_dir`（書き出し先ディレクトリ）
- **出力**: 書き出したファイルパスのリスト（ページごとに`page_XX_en.md`・`page_XX_ja.md`）
- **処理内容**: unitsをページ番号でまとめ、各ページについて`page_XX_en.md`（`en_text`）と`page_XX_ja.md`（`ja_text`）を`page_*_en.md`と同じタグ形式で書き出す。画像unit（`figure_image`/`equation_image`）は`![tag](path) [tag]`行、`equation_latex`とその他のkindは`[tag] text`行として出力する。`parse_page_file`（`mainCode/stage3/stage3.py`）の逆変換にあたるが、タグ書式の実装は共有していない。`en.md`も現在の`en_text`で上書きするのは、後処理（`protect_confirmed_single_letter_leaks`等）で更新された`en_text`を工程(2)時点の`en.md`へ反映するため。
- **テスト対象**: `test_write_translated_pages_preserves_tag_format`で、自作unitsから`page_01_en.md`/`page_01_ja.md`/`page_02_en.md`/`page_02_ja.md`が期待どおりのタグ形式（タイトル・画像行・キャプション・本文）で書き出されることを直接検証している。`postprocess`経由の書き出しは`test_postprocess_protects_leaked_letter_and_writes_pages`でも検証されている。

## 5. 関連ドキュメント

- `doc/architecture.md` §2「パイプラインの7工程」: 7工程モデルの正データ。工程(6)が工程(5)の出力を受けて未保護数式の自動保護・書き出しまでを担う位置づけを説明する。
- `doc/architecture/stage5.md`: `postprocess`が受け取る`units`を用意する工程(5)`apply_restore`の詳細。
- `doc/architecture/shared.md`: `filter_translatable_units`/`protect`/`restore`の実装詳細（本ファイルの複数関数が委譲する先）。
- `CLAUDE.md`「テスト・実行運用規定」項目4: 未保護インライン数式のフルチェックにおける許可リスト（`KNOWN_FALSE_POSITIVE_FRAGMENTS`/`KNOWN_LEAKED_MATH_FRAGMENTS`）の運用方針。
