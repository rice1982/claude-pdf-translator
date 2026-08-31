"""工程(4)「翻訳実行」モジュール。

DeepL APIを呼び出す翻訳バックエンドの実装と、それをCLIから呼び出す入口
関数を1ファイルにまとめている。数式スパンの保護は工程3の終わりに
``stage3.protect_units``が既に済ませているため、バックエンドは
``unit.protected_en_text``/``unit.math_spans``をそのまま使う（自身では
protectを呼ばない）。障害は :class:`TranslationBackendError` に正規化して
呼び出し元へ伝える。数式プレースホルダの復元（apply_restore）は工程(5)の
責務のため :mod:`mainCode.stage5.stage5` にある。

``call_deepl`` は文書全体の文脈＋直前2〜3文を ``context`` パラメータで
渡しながら、翻訳対象のDocUnitを1文ずつ逐次送信する（要APIキー・実課金）。
入口 ``translate_units`` は環境変数 ``DEEPL_API_KEY`` を読んで ``call_deepl``
を呼ぶだけで、送受信内容の記録保存（``cache/``配下へのスナップショット）は
呼び出し元（``whole_pipeline.main``）が担う。構成の詳細は
``doc/architecture/stage4.md`` を参照。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

import deepl

from mainCode.shared.shared import DocUnit, filter_translatable_units


# ============================================================================
# 共通型（バックエンドと入口が共有する例外と戻り値型）
# ============================================================================


# 翻訳を継続できないことを示す。
class TranslationBackendError(Exception):
    pass


# 1unit分の翻訳エンジン応答（restore適用前）。
@dataclass
class RawTranslationResult:
    raw_text: str
    """翻訳エンジンの応答テキスト（数式プレースホルダ__MATHn__が残った状態）。"""
    math_spans: list[str]
    """restoreでプレースホルダを復元するための、元の数式スパンのリスト。"""


# ============================================================================
# DeepL
# ============================================================================

_CONTEXT_HISTORY_SIZE = 3


# document_context（文書全体の要約）と直近の履歴（最大
# ``_CONTEXT_HISTORY_SIZE``件）を連結し、DeepLの``context``パラメータへ
# 渡す文字列を組み立てる。連結後の文字列が空になる場合はNoneを返す。
def _build_context_text(document_context: str, history: list[str]) -> str | None:
    context_parts = [document_context] if document_context else []
    if history:
        context_parts.append("\n".join(history[-_CONTEXT_HISTORY_SIZE:]))
    return "\n\n".join(context_parts) or None


# 翻訳対象のDocUnitを順にDeepLへ送り、restore適用前の生の応答を返す。
#
# ``unit.ja_text`` はこの時点では書き換えない。数式の復元は
# :func:`apply_restore` が担う。キー未設定・上限到達・通信エラーなど、
# DeepLでの翻訳継続が不可能な場合は TranslationBackendError を送出する。
def call_deepl(
    units: list[DocUnit],
    api_key: str | None,
    document_context: str,
    log: Callable[[str], None] = print,
) -> dict[str, RawTranslationResult]:
    # 1. APIキーが無ければ即座に失敗させる。
    if not api_key:
        raise TranslationBackendError("DEEPL_API_KEY が未設定です")

    # 2. DeepLクライアント、文ごとに積み上げる文脈履歴、翻訳結果を詰めていく辞書を用意する。
    translator = deepl.Translator(api_key)
    history: list[str] = []
    raw_results: dict[str, RawTranslationResult] = {}

    # 3. 翻訳対象unitだけを、文書全体の文脈＋直近履歴を添えて1文ずつ逐次送信する。
    for unit in filter_translatable_units(units):
        log(f"[DeepL] {unit.tag} を翻訳中...")

        protected_text = unit.protected_en_text
        context_text = _build_context_text(document_context, history)

        # 4. DeepLでの翻訳継続が不可能なエラーはTranslationBackendErrorへ正規化する。
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

        # 5. restore適用前の生の応答を記録し、送った原文を次以降の文脈履歴へ加える。
        raw_results[unit.tag] = RawTranslationResult(raw_text=result.text, math_spans=unit.math_spans)
        history.append(protected_text)

    return raw_results


# ============================================================================
# 入口（環境変数からAPIキーを読んでDeepLを呼ぶ）
# ============================================================================


# 環境変数``DEEPL_API_KEY``からAPIキーを読み、call_deeplへ委譲して
# raw_results（restore適用前の生の応答）を返す。
#
# 翻訳エンジンとの実際の送受信内容をcache/配下へ記録として保存する機能は
# 持たない（それが必要な場合は呼び出し元がこの関数の戻り値を使って別途
# 行う。:func:`mainCode.whole_pipeline.whole_pipeline.main`
# 参照）。キー未設定・上限到達・通信エラーなど、DeepLでの翻訳継続が
# 不可能な場合は TranslationBackendError を送出する。
def translate_units(
    units: list[DocUnit],
    document_context: str,
    log: Callable[[str], None] = print,
) -> dict[str, RawTranslationResult]:
    # 1. APIキーは環境変数から読む。未設定ならcall_deepl側でTranslationBackendErrorになる。
    api_key = os.environ.get("DEEPL_API_KEY")
    log("[DeepL] 文脈付き翻訳を開始します...")
    raw_results = call_deepl(units, api_key, document_context, log=log)
    log("[DeepL] 翻訳が完了しました。")
    return raw_results
