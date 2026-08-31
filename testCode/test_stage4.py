"""工程(4): 翻訳実行のテスト。

対応関数: stage4.call_deepl、およびそれを呼ぶ入口 stage4.translate_units。
7工程中(2)と並ぶ実行必須の工程だが、deepl.Translatorを差し替えることで
実エンジンを一切呼ばずに検証する。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import mainCode.stage4.stage4 as stage4
from mainCode.stage3.stage3 import protect_units
from mainCode.stage4.stage4 import (
    TranslationBackendError,
    call_deepl,
)
from mainCode.shared.shared import DocUnit


class TestBuildContextText:
    def test_build_context_text_only_keeps_last_three_history_entries(self):
        """_CONTEXT_HISTORY_SIZE（3）を超える履歴が渡された場合、直近3件
    だけに切り詰められることを確認する（既存のcall_deepl経由のテストは
    最大2件の履歴しか作らないため、切り詰めロジック自体は一度も
    踏まれていなかった）。"""
        history = ["s1", "s2", "s3", "s4", "s5"]

        result = stage4._build_context_text("Doc summary.", history)

        assert result == "Doc summary.\n\ns3\ns4\ns5"


    def test_build_context_text_returns_none_when_nothing_to_join(self):
        """document_context・historyのどちらも空の場合、Noneを返すことを
    確認する（DeepLのcontext引数へNoneをそのまま渡せることの前提）。"""
        assert stage4._build_context_text("", []) is None


def _make_fake_deepl_class(response_text: str = "[JA] translated"):
    calls: list[dict] = []

    class _FakeTranslator:
        def __init__(self, api_key):
            self.api_key = api_key

        def translate_text(self, text, source_lang, target_lang, context, preserve_formatting, split_sentences):
            calls.append({"api_key": self.api_key, "text": text, "context": context})
            return SimpleNamespace(text=response_text)

    return _FakeTranslator, calls




class TestCallDeepl:
    def test_call_deepl_raises_when_api_key_missing(self):
        units = [DocUnit(tag="P1-S1-body-S1", kind="body_sentence", page=1, en_text="Hello.", translatable=True)]
        with pytest.raises(TranslationBackendError):
            call_deepl(units, api_key=None, document_context="")


    def test_call_deepl_protects_math_and_builds_context_history(self, monkeypatch):
        fake_class, calls = _make_fake_deepl_class()
        monkeypatch.setattr(stage4.deepl, "Translator", fake_class)

        units = [
            DocUnit(
                tag="P1-S1-body-S1", kind="body_sentence", page=1, en_text="We use $x$ as input.", translatable=True
            ),
            DocUnit(tag="P1-S2-body-S2", kind="body_sentence", page=1, en_text="It works well.", translatable=True),
            DocUnit(tag="P1-FIG1", kind="figure_image", page=1, translatable=False),
        ]
        protect_units(units)
        raw_results = call_deepl(units, api_key="dummy-key", document_context="Doc summary.")

        # 非翻訳対象unit（figure_image）はDeepLに送られない
        assert set(raw_results) == {"P1-S1-body-S1", "P1-S2-body-S2"}
        assert len(calls) == 2

        # 数式スパンがプレースホルダへ退避された状態でDeepLへ送られる
        assert calls[0]["text"] == "We use __MATH0__ as input."
        assert raw_results["P1-S1-body-S1"].math_spans == ["$x$"]

        # 文脈: 1文目はドキュメント全体の要約のみ、2文目はそれに直近履歴が続く
        assert calls[0]["context"] == "Doc summary."
        assert calls[1]["context"] == "Doc summary.\n\nWe use __MATH0__ as input."


    def test_call_deepl_wraps_deepl_exception_as_backend_error(self, monkeypatch):
        class _RaisingTranslator:
            def __init__(self, api_key):
                pass

            def translate_text(self, *args, **kwargs):
                raise stage4.deepl.DeepLException("quota exceeded")

        monkeypatch.setattr(stage4.deepl, "Translator", _RaisingTranslator)
        units = [DocUnit(tag="P1-S1-body-S1", kind="body_sentence", page=1, en_text="Hello.", translatable=True)]

        with pytest.raises(TranslationBackendError):
            call_deepl(units, api_key="dummy-key", document_context="")


# ============================================================================
# 入口（translate_units）
#   環境変数からAPIキーを読んでcall_deeplへ委譲するだけの薄いラッパー。
#   call_deepl自体はTestCallDeeplで検証済みのため、ここではtranslate_units
#   自身の環境変数からのAPIキー取得・ログ出力のみをモックで検証する
#   （唯一この関数を実体のまま直接呼ぶテストクラス）。
# ============================================================================


class TestTranslateUnitsEntry:
    def test_translate_units_reads_api_key_from_environ_and_delegates_to_deepl(self, monkeypatch):
        captured: dict = {}

        def _fake_call_deepl(units, api_key, document_context, log):
            captured["api_key"] = api_key
            captured["document_context"] = document_context
            return {}

        monkeypatch.setattr(stage4, "call_deepl", _fake_call_deepl)
        monkeypatch.setenv("DEEPL_API_KEY", "env-key-123")
        units = [DocUnit(tag="P1-S1-body-S1", kind="body_sentence", page=1, en_text="Hello.", translatable=True)]
        logs: list[str] = []

        stage4.translate_units(units, document_context="ctx", log=logs.append)

        assert captured == {"api_key": "env-key-123", "document_context": "ctx"}
        assert any("DeepL" in m and "開始" in m for m in logs)
        assert any("DeepL" in m and "完了" in m for m in logs)


    def test_translate_units_passes_none_api_key_when_environ_unset(self, monkeypatch):
        """DEEPL_API_KEY未設定時、Noneのままcall_deepl（APIキー未設定エラーの
    判定はcall_deepl自身の責務）へ渡ることを確認する。"""
        captured: dict = {}
        monkeypatch.setattr(
            stage4, "call_deepl", lambda units, api_key, document_context, log: captured.update(api_key=api_key) or {}
        )
        monkeypatch.delenv("DEEPL_API_KEY", raising=False)
        units = [DocUnit(tag="P1-S1-body-S1", kind="body_sentence", page=1, en_text="Hello.", translatable=True)]

        stage4.translate_units(units, document_context="", log=lambda m: None)

        assert captured["api_key"] is None
