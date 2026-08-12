"""DeepL APIによる文脈付き翻訳モジュール。

各文の翻訳リクエストには、①ドキュメント全体の文脈（タイトル＋Abstract）と
②直前2〜3文の原文を ``context`` パラメータとして渡し、代名詞や専門用語の
表記揺れを抑える。DeepL側の障害（キー未設定・上限到達・通信エラー等）は
すべて :class:`TranslationBackendError` に正規化して呼び出し元へ伝える。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import deepl

from math_protection import protect, restore
from translation_models import DocUnit

_CONTEXT_HISTORY_SIZE = 3


class TranslationBackendError(Exception):
    """DeepLでの翻訳を継続できないことを示す（フォールバックのトリガー）。"""


@dataclass
class RawDeeplResult:
    """1unit分のDeepL応答（restore適用前）。"""

    raw_text: str
    """DeepLの応答テキスト（数式プレースホルダ__MATHn__が残った状態）。"""
    math_spans: list[str]
    """restoreでプレースホルダを復元するための、元の数式スパンのリスト。"""


@dataclass
class DeeplRequest:
    """1unit分の、DeepLへ実際に送信される内容。"""

    protected_text: str
    """DeepLの``text``引数に渡される本文（数式プレースホルダ退避済み）。"""
    context_text: str | None
    """DeepLの``context``引数に渡される文脈（ドキュメント要約＋直近履歴）。"""


def build_deepl_requests(units: list[DocUnit], document_context: str) -> dict[str, DeeplRequest]:
    """DeepLへ実際に送信される内容を、unit.tagをキーにした辞書で返す。

    ``call_deepl``が内部で組み立てる内容と同じ決定的な計算（``protect``と
    直近3文の履歴の連結）であり、DeepLへの通信は行わない。
    """
    history: list[str] = []
    requests: dict[str, DeeplRequest] = {}

    for unit in units:
        if not unit.translatable:
            continue

        protected_text, _ = protect(unit.en_text)

        context_parts = [document_context] if document_context else []
        if history:
            context_parts.append("\n".join(history[-_CONTEXT_HISTORY_SIZE:]))
        context_text = "\n\n".join(context_parts) or None

        requests[unit.tag] = DeeplRequest(protected_text=protected_text, context_text=context_text)
        history.append(protected_text)

    return requests


def call_deepl(
    units: list[DocUnit],
    api_key: str | None,
    document_context: str,
    log: Callable[[str], None] = print,
) -> dict[str, RawDeeplResult]:
    """翻訳対象のDocUnitを順にDeepLへ送り、restore適用前の生の応答を返す。

    ``unit.ja_text`` はこの時点では書き換えない。数式の復元は
    :func:`apply_restore` が担う。

    Raises:
        TranslationBackendError: キー未設定、上限到達、通信エラーなど、
            DeepLでの翻訳継続が不可能な場合。
    """
    if not api_key:
        raise TranslationBackendError("DEEPL_API_KEY が未設定です")

    translator = deepl.Translator(api_key)
    history: list[str] = []
    raw_results: dict[str, RawDeeplResult] = {}

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

        raw_results[unit.tag] = RawDeeplResult(raw_text=result.text, math_spans=math_spans)
        history.append(protected_text)

    return raw_results


def apply_restore(units: list[DocUnit], raw_results: dict[str, RawDeeplResult]) -> None:
    """:func:`call_deepl` の戻り値を受け取り、数式を復元して``unit.ja_text``へ書き込む。

    DeepLへの再通信を伴わない決定的な処理。
    """
    for unit in units:
        if not unit.translatable:
            continue
        raw = raw_results[unit.tag]
        unit.ja_text = restore(raw.raw_text, raw.math_spans)
