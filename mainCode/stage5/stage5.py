"""工程(5)「翻訳後処理（数式復元）」モジュール。

インライン数式（`$...$` / `$$...$$`）を翻訳エンジンから保護するための
プレースホルダ退避・復元の仕組み（``protect``/``restore``）自体は
:mod:`mainCode.shared.shared` に置かれている（工程(3)の仕上げである
``mainCode.stage3.stage3.protect_units``/``normalize``と、工程(6)側の
複数関数の双方が、互いをimportし合うことなく使うため）。DeepLは生のLaTeX
断片を誤訳・破損させる可能性があるため、翻訳リクエスト前に数式スパンを
`__MATH0__` のような単純なプレースホルダへ置き換え、翻訳結果に対して
プレースホルダを元の数式へ復元する。復元後のLaTeXはPDFレンダリング側
（:mod:`stage7`）でKaTeXにより実際の数式として描画される。

工程(5)全体の入口は ``apply_restore``。``call_deepl``の戻り値
（``RawTranslationResult``）を受け取り、数式プレースホルダを復元して
``unit.ja_text`` へ書き込む、翻訳エンジンへの再通信を伴わない決定的な処理。
翻訳後に残った未保護の数式らしき断片の検出・自動保護・翻訳済みMarkdownの
書き出しは工程(6)「数式保護」（:mod:`mainCode.stage6.stage6`）の責務。
構成の詳細は ``doc/architecture/stage5.md`` を参照。
"""

from __future__ import annotations

from mainCode.shared.shared import DocUnit, filter_translatable_units, restore
from mainCode.stage4.stage4 import RawTranslationResult


# call_deeplの戻り値を受け取り、数式を復元して
# ``unit.ja_text``へ書き込む（工程(5)「翻訳後処理」の唯一のステップ）。
#
# 翻訳エンジンへの再通信を伴わない決定的な処理。
def apply_restore(units: list[DocUnit], raw_results: dict[str, RawTranslationResult]) -> None:
    for unit in filter_translatable_units(units):
        # raw_resultsはunit.tagをキーにした辞書（call_deeplが返した形式
        # そのまま）。翻訳対象外unitはtranslate_units自体に送られておらず
        # raw_resultsにキーが存在しないため、filter_translatable_unitsで先に絞り込む。
        raw = raw_results[unit.tag]
        unit.ja_text = restore(raw.raw_text, raw.math_spans)
