"""DeepL APIによる文脈付き翻訳モジュール。

各文の翻訳リクエストには、①ドキュメント全体の文脈（タイトル＋Abstract）と
②直前2〜3文の原文を ``context`` パラメータとして渡し、代名詞や専門用語の
表記揺れを抑える。DeepL側の障害（キー未設定・上限到達・通信エラー等）は
すべて :class:`TranslationBackendError` に正規化して呼び出し元へ伝える。
"""

from __future__ import annotations

from collections.abc import Callable

import deepl

from math_protection import protect, restore
from translation_models import DocUnit

_CONTEXT_HISTORY_SIZE = 3


class TranslationBackendError(Exception):
    """DeepLでの翻訳を継続できないことを示す（フォールバックのトリガー）。"""


def translate_with_deepl(
    units: list[DocUnit],
    api_key: str | None,
    document_context: str,
    log: Callable[[str], None] = print,
) -> None:
    """翻訳対象のDocUnitを順に翻訳し、``unit.ja_text`` を書き換える。

    Raises:
        TranslationBackendError: キー未設定、上限到達、通信エラーなど、
            DeepLでの翻訳継続が不可能な場合。
    """
    if not api_key:
        raise TranslationBackendError("DEEPL_API_KEY が未設定です")

    translator = deepl.Translator(api_key)
    history: list[str] = []

    for unit in units:
        if not unit.translatable:
            continue
        log(f"[DeepL] {unit.tag} を翻訳中...")

        protected_text, math_spans = protect(unit.en_text)

        context_parts = [document_context] if document_context else []
        if history:
            context_parts.append("\n".join(history[-_CONTEXT_HISTORY_SIZE:]))
        context_text = "\n\n".join(context_parts) or None

        try:
            result = translator.translate_text(
                protected_text,
                source_lang="EN",
                target_lang="JA",
                context=context_text,
                preserve_formatting=True,
                split_sentences="off",
            )
        except deepl.DeepLException as exc:
            raise TranslationBackendError(f"DeepL翻訳でエラーが発生しました: {exc}") from exc

        unit.ja_text = restore(result.text, math_spans)
        history.append(protected_text)
