"""Step 3: 翻訳モジュール。

責務はStep 2が抽出した「翻訳対象の本文テキスト」（TranslationUnitのリスト）
だけを受け取り、翻訳結果を返すことに限定する。ページ構造・章立て・図表と
いった非翻訳要素については一切関知しない（それらはStructuredDocument.pages
側に残ったまま、Step 4で組み合わされる）。

現時点ではこのプロジェクトに実際の翻訳バックエンド（LLM API等）は接続されて
いないため、デフォルトの ``identity_translator`` は入力テキストをそのまま
返す恒等関数になっている。実際の翻訳を行う場合は、``translate`` 関数を
差し替えるだけでよい（Step 1/2/4のコードは変更不要）。
"""

from __future__ import annotations

import logging
from typing import Callable

from pdf_models import TranslationUnit

logger = logging.getLogger(__name__)

TranslateFunc = Callable[[str], str]
"""1文を受け取り、翻訳後の1文を返す関数の型。"""


def identity_translator(text: str) -> str:
    """デフォルトの翻訳関数（恒等関数）。実際の翻訳バックエンドが未接続の
    間は、原文をそのまま返す。"""
    return text


def translate_units(
    units: list[TranslationUnit], translate: TranslateFunc = identity_translator
) -> dict[str, str]:
    """TranslationUnitのリストを翻訳し、unit_id -> 翻訳後テキストの辞書を返す。

    1文の翻訳に失敗しても他の文の処理・呼び出し元のパイプライン全体には
    影響させない。失敗した文は原文をそのまま結果に含める（Step 4側で
    「翻訳結果があればそれを、無ければ原文を使う」という前提と整合する
    フォールバックになる）。

    Args:
        units: 翻訳対象の文一覧（Step 2の出力）。
        translate: 1文を翻訳する関数。省略時は原文をそのまま返す
            identity_translator が使われる。

    Returns:
        unit_id をキーとする翻訳結果の辞書。
    """
    results: dict[str, str] = {}
    for unit in units:
        try:
            results[unit.unit_id] = translate(unit.text)
        except Exception as exc:  # noqa: BLE001 - 1文の翻訳失敗で全体を止めないためのフォールバック
            logger.warning("文 %s の翻訳に失敗したため原文を使用します: %s", unit.unit_id, exc)
            results[unit.unit_id] = unit.text
    return results
