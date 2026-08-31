"""工程(5): 翻訳後処理（数式復元）のテスト。

対応関数: stage5.apply_restore（工程5全体を代表する、唯一のステップを
持つ関数）。あわせて、whole_pipeline._write_restore_snapshotがapply_restore
の直後（工程(6)「数式保護」の保護処理より前）のタイミングでスナップショット
を書き出しているかの回帰テストも置く（この関数はwhole_pipeline.py側に
あるが、検証している役割が工程5のスコープに収まるため、CLAUDE.md
「テスト・実行運用規定」項目8の方針（ソースの置き場所ではなく役割で
分類する）に従いここに置く）。未保護数式の検出・自動保護・翻訳済み
Markdownの書き出しは工程(6)「数式保護」（test_stage6.py）が担う。
"""
from __future__ import annotations

import json

import pytest

from mainCode.shared.shared import DocUnit
from mainCode.stage4.stage4 import RawTranslationResult
from mainCode.stage5.stage5 import apply_restore
from mainCode.whole_pipeline.whole_pipeline import _write_restore_snapshot

from conftest import _find_latest_real_deepl_cache


class TestApplyRestore:
    def test_apply_restore_writes_ja_text_from_raw_results_with_math_restored(self):
        units = [
            DocUnit(tag="P1-S1-body-S1", kind="body_sentence", page=1, en_text="We use $x$.", ja_text="", translatable=True),
            DocUnit(tag="P1-FIG1", kind="figure_image", page=1, ja_text="", translatable=False),
        ]
        raw_results = {"P1-S1-body-S1": RawTranslationResult(raw_text="__MATH0__を使用します。", math_spans=["$x$"])}

        apply_restore(units, raw_results)

        assert units[0].ja_text == "$x$を使用します。"
        assert units[1].ja_text == ""  # 非翻訳対象unitは書き換えない


    def test_apply_restore_leaves_placeholder_when_index_is_out_of_range(self):
        """翻訳エンジンがプレースホルダ番号を破損・捏造し、raw_textの
    __MATHn__がmath_spansの範囲外を指してしまった場合、shared.restoreの
    フォールバックによりプレースホルダ文字列がそのまま残るか（shared.restore
    のこの境界分岐は、apply_restore経由も含めどこからも一度もテストされて
    いなかった）。"""
        units = [
            DocUnit(tag="P1-S1-body-S1", kind="body_sentence", page=1, en_text="We use $x$.", ja_text="", translatable=True),
        ]
        raw_results = {"P1-S1-body-S1": RawTranslationResult(raw_text="__MATH5__を使用します。", math_spans=["$x$"])}

        apply_restore(units, raw_results)

        assert units[0].ja_text == "__MATH5__を使用します。"


    @pytest.mark.parametrize("run_id", ["sample0_full", "sample1_full", "sample2_full", "sample3_label55-60"])
    def test_apply_restore_matches_cached_deepl_output(self, run_id):
        """apply_restoreを、cache/<run_id>/real_deepl_output_<タイムスタンプ>/に
    凍結された実際のDeepL実行結果と突き合わせてオフラインで検証する
    （DeepLを一切呼ばない。意図的にテスト名へ"real_deepl"を含めていない。
    CLAUDE.mdの実行前確認ルールは名前に"real_deepl"を含むテストが対象
    のため、無課金の本テストが誤ってその対象に含まれないようにするため）。

    自作データによる上記のround-tripテストとは異なり、本テストは
    `04_deepl_output/raw_deepl_results.json`（DeepLの生応答、restore適用前）
    と`05_restored/units_raw.json`（restore直後に凍結されたDocUnit
    スナップショット、独立した正解データ）という、実DeepL実行時にのみ
    生成される永続キャッシュを使う。raw_deepl_results.jsonにapply_restore
    を通した結果を自分自身の正解データにするのではなく、独立に凍結された
    units_raw.jsonのja_textと比較することで、apply_restore自体に
    リグレッションが入っても検知できる（同じ関数の出力を自分自身の正解
    データにしてしまうと、そのリグレッションを検知できなくなるため）。

    CLAUDE.mdの規定により実DeepLテストはsample0/sample1限定のため、現時点
    ではsample2/sample3は対応するキャッシュが無くskipされる。将来
    sample2/sample3向けにキャッシュが用意された場合、コードの変更無しに
    自動的に実行対象になる（サンプルを選べるようにするための
    parametrize）。同じrun_idで複数回実行されたキャッシュがある場合は
    最も新しいものを使う（`_find_latest_real_deepl_cache`参照）。
    """
        cache_root = _find_latest_real_deepl_cache(run_id)
        raw_path = cache_root / "04_deepl_output" / "raw_deepl_results.json" if cache_root else None
        units_raw_path = cache_root / "05_restored" / "units_raw.json" if cache_root else None
        if cache_root is None or not raw_path.exists() or not units_raw_path.exists():
            pytest.skip(
                f"{run_id} の実DeepLキャッシュが無いためスキップ"
                "（test_run_pipeline_end_to_end_with_real_deeplの実行、または"
                "translate_paper.pyでの本実行により生成される）"
            )

        raw_results = {
            tag: RawTranslationResult(raw_text=data["raw_text"], math_spans=data["math_spans"])
            for tag, data in json.loads(raw_path.read_text(encoding="utf-8")).items()
        }
        units_raw_data = json.loads(units_raw_path.read_text(encoding="utf-8"))
        expected_ja_by_tag = {d["tag"]: d["ja_text"] for d in units_raw_data}

        # translatable=Falseのunit（著者名・LaTeX数式等）はparse_output_dir時点で
        # ja_text=en_textが設定済みで、apply_restoreはtranslatable=Trueのunitしか
        # 書き換えない。translatableなunitだけja_textを空にリセットする。
        units = [DocUnit(**{**d, "ja_text": "" if d["translatable"] else d["ja_text"]}) for d in units_raw_data]
        apply_restore(units, raw_results)

        actual_ja_by_tag = {u.tag: u.ja_text for u in units}
        assert actual_ja_by_tag == expected_ja_by_tag


class TestWriteRestoreSnapshot:
    def test_write_restore_snapshot_records_pre_protection_state(self, tmp_path):
        """apply_restore→_write_restore_snapshotという、main()が工程(5)の
    末尾で実行する順序をそのまま呼び、snapshot_dir/05_restored/の内容が
    apply_restore直後（工程(6)の保護処理適用前）の状態になっていることを
    確認する。

    test_apply_restore_matches_cached_deepl_outputが、この
    05_restored/units_raw.jsonを「apply_restore直後の独立した正解データ」
    として前提にしているため、将来_write_restore_snapshotの呼び出し
    タイミングを誤って崩してしまった場合に検知できるようにする安全網
    として置いている。
    """
        units = [
            DocUnit(
                tag="P1-S1-body-S1",
                kind="body_sentence",
                page=1,
                en_text="We decode a latent z back to an edge map.",
                ja_text="",
                translatable=True,
            )
        ]
        raw_results = {
            "P1-S1-body-S1": RawTranslationResult(raw_text="潜在変数zをエッジマップにデコードする。", math_spans=[])
        }
        snapshot_dir = tmp_path / "snapshot"

        apply_restore(units, raw_results)
        _write_restore_snapshot(units, snapshot_dir)

        restored_dir = snapshot_dir / "05_restored"
        page_en_text = (restored_dir / "page_01_en.md").read_text(encoding="utf-8")
        page_ja_text = (restored_dir / "page_01_ja.md").read_text(encoding="utf-8")

        assert "[P1-S1-body-S1] We decode a latent z back to an edge map." in page_en_text
        assert "[P1-S1-body-S1] 潜在変数zをエッジマップにデコードする。" in page_ja_text

        units_raw = json.loads((restored_dir / "units_raw.json").read_text(encoding="utf-8"))
        assert units_raw[0]["ja_text"] == "潜在変数zをエッジマップにデコードする。"
