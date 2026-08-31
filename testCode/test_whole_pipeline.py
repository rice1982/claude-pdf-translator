"""全体テスト（工程(1)〜(7)を実際に1本のテストで通しで検証する）。

統合テスト（test_integration.py）は工程(3)〜(7)（prepare_translation_input
〜render_units_to_pdfs経由）までをカバーする。このテストは、工程(1)
（resolve_page_range、CLIオプション→ページ範囲の解決）を含めて最初から
最後まで通しで動くかを検証する唯一のテストで、resolve_page_range（工程1）
→process_pdf（工程2）→prepare_translation_input・translate_units・
apply_restore・stage6.postprocess・render_units_to_pdfs（工程3〜7）という、
main()が実際に呼ぶのと同じ関数の並びをそのまま実行する。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import fitz  # PyMuPDF
import pytest
from dotenv import load_dotenv

import mainCode.whole_pipeline.whole_pipeline as translate_paper
from mainCode.shared.shared import DocUnit
from mainCode.stage4.stage4 import RawTranslationResult
from mainCode.whole_pipeline.whole_pipeline import resolve_page_range

from conftest import (
    SAMPLE_PDF_PATH,
    SAMPLE1_PDF_PATH,
    SAMPLE2_PDF_PATH,
    SAMPLE3_PDF_PATH,
    _find_latest_real_deepl_cache,
)


class TestEndToEndPipeline:
    @pytest.mark.parametrize(
        "pdf_path,start_label,end_label,run_id,range_label,mineru_backend",
        [
            (SAMPLE_PDF_PATH, None, None, "sample0", "full", "pipeline"),
            (SAMPLE1_PDF_PATH, None, None, "sample1", "full", "pipeline"),
            (SAMPLE2_PDF_PATH, None, None, "sample2", "full", "pipeline"),
            (SAMPLE3_PDF_PATH, "55", "60", "sample3", "label55-60", "pipeline"),
            (SAMPLE3_PDF_PATH, "56", "56", "sample3", "label56-56", "vlm-engine"),
        ],
        ids=[
            "sample0_full_pipeline",
            "sample1_full_pipeline",
            "sample2_full_pipeline",
            "sample3_label55-60_pipeline",
            "sample3_label56-56_vlm-engine",
        ],
    )
    def test_run_pipeline_end_to_end_with_mocked_translation(self,
        tmp_path, pdf_path, start_label, end_label, run_id, range_label, mineru_backend, monkeypatch
    ):
        """工程(1)〜(7)を実際につなげて通しで検証する唯一の全体テスト。

    sample3のみ`--start-label 55 --end-label 60`相当（resolve_page_range
    経由）を指定し、ラベル解決の結果が実際にMinerU実行・翻訳・PDF生成まで
    正しくつながることを検証する（他のテストはラベル解決の結果を数値
    として検証するのみで、実際にパイプラインへ流し込むところまでは
    検証しない）。sample0/1/2は範囲指定無し（全文処理）。

    sample3_label56-56_vlm-engineケースは、MinerUバックエンドに
    `vlm-engine`（数式・OCR認識精度が高い代わりに低速なバックエンド）を
    指定した場合でも、翻訳エンジンをモックした軽量な形でパイプライン全体が
    問題なく通ることを無課金で確認するためのもの（実DeepL版の
    test_run_pipeline_end_to_end_with_real_deeplの同名ケースと対になる、
    無課金・気軽に何度でも回せる版）。

    range_labelは、cache/配下の実際のMinerUキャッシュフォルダ名
    （sample0_full等。mineru_backendが`pipeline`以外の場合は
    `mineru_cache_<backend>/`）と一致させるために必須。これを渡さないと
    process_pdf内部のキャッシュ機構がヒットせず、毎回実際にMinerUを
    再実行してしまう（無課金で気軽に回せるはずのテストが実質的に重い
    処理になってしまうため、他のテストと同じ命名規則に必ず揃えること）。

    無課金で気軽に何度も実行されるテストのため、出力はpytestの一時
    ディレクトリ（tmp_path）に書き、テスト終了後に自動的に消える
    （output/には残さない。永続化する必要がある場合は下の実DeepL版を
    参照）。翻訳エンジンをモックしているため、_resolve_snapshot_dirは
    呼ばない（snapshot_dirをNoneのまま扱うため、_write_restore_snapshotも
    呼ばれない）。スナップショットを保存してしまうと、モックの
    訳文が実行結果の凍結データ用フォルダ（real_deepl_output_*）に誤って
    書き込まれ、実データとして後続のオフライン回帰テストに混入してしまう。
    """
        if not pdf_path.exists():
            pytest.skip(f"{pdf_path} がないためスキップ")

        start_page, end_page = resolve_page_range(pdf_path, None, None, None, start_label, end_label)

        def _fake_translate_units(units, document_context, log=print):
            return {
                unit.tag: RawTranslationResult(raw_text="これは全体テスト用の日本語訳です。", math_spans=[])
                for unit in units
                if unit.translatable
            }

        monkeypatch.setattr(translate_paper, "translate_units", _fake_translate_units)

        output_dir = tmp_path / f"e2e_{run_id}"
        translate_paper.process_pdf(
            pdf_path, output_dir, start_page=start_page, end_page=end_page,
            range_label=range_label, mineru_backend=mineru_backend,
        )
        units, document_context = translate_paper.prepare_translation_input(output_dir)
        raw_results = translate_paper.translate_units(units, document_context)
        translate_paper.apply_restore(units, raw_results)
        translate_paper.stage6_postprocess(units, output_dir)
        pdf_paths = translate_paper.render_units_to_pdfs(units, output_dir)

        assert len(pdf_paths) == 3
        for path in pdf_paths:
            assert path.exists(), f"{path} が生成されていない"
            assert path.stat().st_size > 0, f"{path} が空ファイルになっている"

        ja_pdf_path = next(p for p in pdf_paths if p.name == "paper_ja.pdf")
        with fitz.open(ja_pdf_path) as doc:
            ja_text = "".join(page.get_text() for page in doc)
        has_japanese = any("぀" <= ch <= "ヿ" or "一" <= ch <= "鿿" for ch in ja_text)
        assert has_japanese, "paper_ja.pdfに日本語文字が見つからない（翻訳が行われていない可能性がある）"


    @pytest.mark.real_deepl
    @pytest.mark.parametrize(
        "pdf_path,start_label,end_label,run_id,range_label,mineru_backend",
        [
            (SAMPLE_PDF_PATH, None, None, "sample0", "full", "pipeline"),
            (SAMPLE1_PDF_PATH, None, None, "sample1", "full", "pipeline"),
            (SAMPLE3_PDF_PATH, "55", "60", "sample3", "label55-60", "pipeline"),
            (SAMPLE3_PDF_PATH, "56", "56", "sample3", "label56-56", "vlm-engine"),
        ],
        ids=[
            "sample0_full_pipeline_deepl",
            "sample1_full_pipeline_deepl",
            "sample3_label55-60_pipeline_deepl",
            "sample3_label56-56_vlm-engine_deepl",
        ],
    )
    def test_run_pipeline_end_to_end_with_real_deepl(self,
        pdf_path, start_label, end_label, run_id, range_label, mineru_backend
    ):
        """工程(1)〜(7)を、DeepLも含めて一切モックせず実際に通しで検証する
    全体テスト（上のtest_run_pipeline_end_to_end_with_mocked_translationの
    実DeepL版）。resolve_page_range（工程1）→process_pdf（工程2）→
    prepare_translation_input・translate_units・apply_restore・
    stage6.postprocess・render_units_to_pdfs（工程3〜7）をmain()と全く同じ
    呼び出し順・同じ関数で実行する。

    対象は以下の4ケースに限定（CLAUDE.mdのsample3.pdf運用規定・実DeepL
    例外規定を参照。この4ケース以外の組み合わせ、特にsample2.pdfや
    上記以外のsample3.pdf範囲・バックエンドの組み合わせでの実DeepL実行は
    対象外）。testIDには、実際に課金される実DeepLテストであることを
    `-k`指定時に明示するため`_deepl`サフィックスを付けている:
      - sample0_full_pipeline_deepl: 全体、MinerUバックエンドpipeline（既定）
      - sample1_full_pipeline_deepl: 全体、MinerUバックエンドpipeline（既定）
      - sample3_label55-60_pipeline_deepl: 印刷ページラベル55〜60
        （物理67〜72）、MinerUバックエンドpipeline（既定）
      - sample3_label56-56_vlm-engine_deepl: 印刷ページラベル56のみ
        （物理68）、MinerUバックエンドvlm-engine。数式・OCR認識精度の
        高いバックエンドでも、翻訳・数式保護・PDF生成まで一連の
        パイプラインが問題なく通ることを確認するためのケース。範囲を
        1ページのみに絞ることで、CLAUDE.mdの「sample3.pdfは最小限の
        範囲のみ処理する」規定と、実DeepL課金の最小化を両立させている。

    実行するたびに実際に課金が発生するため、人間の事前確認を得てから
    実行すること（CLAUDE.mdの実行前確認規定参照。日常の開発ループでは
    実行しない）。

    出力はpytestの一時ディレクトリではなく、output/配下の
    `pytest_{run_id}_{range_label}_{タイムスタンプ}_deepl`フォルダに残す。
    実際のDeepL翻訳結果は再現不可能なため、同じ組み合わせで再実行しても
    過去の結果を上書きしない。

    さらに、_resolve_snapshot_dir（main内でprepare_translation_input直後に
    呼ばれる）が備えるスナップショット機能（pdf_path/range_label引数。
    人間による本実行でも同様に動作する）により、DeepLとの実際の送受信
    内容が自動的にcache/<pdf_pathのstem>_<range_label>/
    real_deepl_output_<タイムスタンプ>/へ
    03_structured/04_deepl_output/05_restoredの形式で記録される。
    このテストはmain()と全く同じ関数の並びだけで完結する。"""
        if not pdf_path.exists():
            pytest.skip(f"{pdf_path} がないためスキップ")

        # マーカー（real_deepl）による既定除外に加えた二重ガード。marker指定
        # （-m real_deepl）だけでは「除外を明示的に外した」ことにしかならず、
        # 課金を伴う実行そのものを人間が許可したことにはならないため、
        # 別途この環境変数を人間が事前に設定していない限りスキップする
        # （CLAUDE.mdの実DeepL実行前確認規定）。
        if os.environ.get("ALLOW_REAL_DEEPL") != "1":
            pytest.skip(
                "ALLOW_REAL_DEEPL=1 が設定されていないためスキップ。"
                "実際にDeepL APIを呼び課金が発生するテストのため、"
                "人間の明示的な許可を得た上でこの環境変数を設定してから実行すること。"
            )

        load_dotenv()
        if os.environ.get("DEEPL_API_KEY") is None:
            pytest.skip("DEEPL_API_KEY が未設定のためスキップ")

        start_page, end_page = resolve_page_range(pdf_path, None, None, None, start_label, end_label)

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_dir = Path("output") / f"pytest_{run_id}_{range_label}_{timestamp}_deepl"

        translate_paper.process_pdf(
            pdf_path, output_dir, start_page=start_page, end_page=end_page,
            range_label=range_label, mineru_backend=mineru_backend,
        )
        units, document_context = translate_paper.prepare_translation_input(output_dir)
        snapshot_dir = translate_paper._resolve_snapshot_dir(pdf_path, range_label)
        if snapshot_dir is not None:
            translate_paper._write_structured_snapshot(output_dir, snapshot_dir, document_context)
        raw_results = translate_paper.translate_units(units, document_context)
        if snapshot_dir is not None:
            translate_paper._write_translation_snapshot(snapshot_dir, raw_results)
        translate_paper.apply_restore(units, raw_results)
        if snapshot_dir is not None:
            translate_paper._write_restore_snapshot(units, snapshot_dir)
        translate_paper.stage6_postprocess(units, output_dir)
        pdf_paths = translate_paper.render_units_to_pdfs(units, output_dir)

        assert len(pdf_paths) == 3
        for path in pdf_paths:
            assert path.exists(), f"{path} が生成されていない"
            assert path.stat().st_size > 0, f"{path} が空ファイルになっている"

        ja_pdf_path = next(p for p in pdf_paths if p.name == "paper_ja.pdf")
        with fitz.open(ja_pdf_path) as doc:
            ja_text = "".join(page.get_text() for page in doc)
        has_japanese = any("぀" <= ch <= "ヿ" or "一" <= ch <= "鿿" for ch in ja_text)
        assert has_japanese, "paper_ja.pdfに日本語文字が見つからない（翻訳が行われていない可能性がある）"

        cache_root = _find_latest_real_deepl_cache(f"{pdf_path.stem}_{range_label}")
        assert cache_root is not None, "_resolve_snapshot_dirによるreal_deepl_outputスナップショットが見つからない"
        assert (cache_root / "04_deepl_output" / "raw_deepl_results.json").exists()
        assert (cache_root / "05_restored" / "units_raw.json").exists()


class TestBuildArgParser:
    def test_build_arg_parser_defaults(self):
        """_build_arg_parserがこれまで一度も直接テストされていなかった
    （main()経由の間接テストも、sys.argvをモックしていないため一度も
    実行されていない）。既定値・必須引数（pdf_path）が想定通りである
    ことを直接確認する。"""
        args = translate_paper._build_arg_parser().parse_args(["input.pdf"])
        assert args.pdf_path == "input.pdf"
        assert args.output_dir is None
        assert args.chapter is None
        assert args.start is None
        assert args.end is None
        assert args.start_label is None
        assert args.end_label is None
        assert args.mineru_backend == "pipeline"


    def test_build_arg_parser_rejects_invalid_mineru_backend(self):
        """--mineru-backendのchoices制約が実際に機能していることを確認する。"""
        with pytest.raises(SystemExit):
            translate_paper._build_arg_parser().parse_args(["input.pdf", "--mineru-backend", "bogus"])


class TestResolveOutputDir:
    """_resolve_output_dirは、doc/architecture/whole_pipeline.mdの
    作成時点（過去のドキュメント移行セッション）で既に「テストが一度も
    無い」と記録されていた既知のギャップであり、今回の監査まで放置
    されていた。"""

    def test_resolve_output_dir_returns_explicit_value_as_is(self):
        args = argparse.Namespace(output_dir="explicit/output", pdf_path="input/sample0.pdf")
        assert translate_paper._resolve_output_dir(args, "full") == "explicit/output"


    def test_resolve_output_dir_auto_generates_when_omitted(self):
        """output_dir省略時、default_output_dirによる自動命名規則
    （manual_{PDF名}_{範囲記述子}_{タイムスタンプ}）に従ったパスが返ることを
    確認する（タイムスタンプは呼び出しのたびに変わるため、厳密な文字列
    一致ではなく接頭辞で確認する）。"""
        args = argparse.Namespace(output_dir=None, pdf_path="input/sample0.pdf")
        result = str(translate_paper._resolve_output_dir(args, "full"))
        assert result.startswith(str(Path("output") / "manual_sample0_full_"))


class TestResolveSnapshotDir:
    """_resolve_snapshot_dir・_write_structured_snapshot・
    _write_translation_snapshotは、test_run_pipeline_end_to_end_with_real_
    deepl（実DeepLを伴う高コストなテスト、日常の開発ループでは実行しない）
    経由でしか一度も実行されていなかった。ここではパス計算・ファイル
    書き出しというロジック自体を無課金・高速な合成データで直接検証する
    （_write_restore_snapshotがtest_stage5.pyで既にこの方針を採っているのと
    同じ考え方）。"""

    def test_resolve_snapshot_dir_returns_none_when_pdf_path_or_range_label_missing(self):
        assert translate_paper._resolve_snapshot_dir(None, "full") is None
        assert translate_paper._resolve_snapshot_dir("input/sample0.pdf", None) is None


    def test_resolve_snapshot_dir_builds_cache_path_with_deepl_folder_name(self):
        """スナップショットの保存先が cache/<PDF名>_<範囲記述子>/
    real_deepl_output_<タイムスタンプ>/ という構成になることを確認する。"""
        snapshot_dir = translate_paper._resolve_snapshot_dir("input/sample0.pdf", "full")

        assert "real_deepl_output_" in snapshot_dir.name
        # cache/<PDF名>_<範囲記述子>/ という親ディレクトリ構成
        assert snapshot_dir.parent.name == "sample0_full"
        assert snapshot_dir.parent.parent.name == "cache"


class TestWriteSnapshots:
    def test_write_structured_snapshot_writes_context_and_copies_pages(self, tmp_path):
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (output_dir / "page_01_en.md").write_text("[P1-TITLE] Title\n", encoding="utf-8")
        snapshot_dir = tmp_path / "snapshot"

        translate_paper._write_structured_snapshot(output_dir, snapshot_dir, "A Title\nAn abstract.")

        structured_dir = snapshot_dir / "03_structured"
        assert (structured_dir / "document_context.txt").read_text(encoding="utf-8") == "A Title\nAn abstract."
        assert (structured_dir / "page_01_en.md").read_text(encoding="utf-8") == "[P1-TITLE] Title\n"


    def test_write_translation_snapshot_writes_raw_results_json(self, tmp_path):
        """翻訳エンジンの生応答が04_deepl_output/raw_deepl_results.jsonへ
    記録されることを確認する。書き出す内容自体はRawTranslationResultを
    dataclasses.asdictでJSONにしたもの。"""
        snapshot_dir = tmp_path / "snapshot"
        raw_results = {"P1-S1-body-S1": RawTranslationResult(raw_text="訳文です。", math_spans=["$x$"])}

        translate_paper._write_translation_snapshot(snapshot_dir, raw_results)
        deepl_path = snapshot_dir / "04_deepl_output" / "raw_deepl_results.json"
        assert deepl_path.exists()
        assert json.loads(deepl_path.read_text(encoding="utf-8")) == {
            "P1-S1-body-S1": {"raw_text": "訳文です。", "math_spans": ["$x$"]}
        }


class TestMainStdoutEncoding:
    def test_main_reconfigures_stdout_encoding_when_not_utf8(self, monkeypatch):
        """main()の最初のステップ（標準出力がUTF-8でない環境でも日本語ログ
    がUnicodeEncodeErrorにならないよう、sys.stdout.reconfigure(encoding=
    "utf-8")で切り替える処理）を直接検証する。

    わざと存在しないPDFパスを渡すことで、_require_pdf_existsが即座に
    SystemExitを送出し、それより後の重い工程（MinerU・翻訳等）のモックを
    一切不要にしている（stdout切り替えロジック自体はこの手前、main()の
    一番最初で実行されるため）。sys.stdoutはモジュール経由で動的に参照
    されており、本コード側の変更は一切不要（monkeypatch.setattrで
    sys.stdoutを差し替えるだけで済む）。"""
        fake_stdout = MagicMock()
        fake_stdout.encoding = "cp932"  # UTF-8でない環境を再現
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        monkeypatch.setattr(sys, "argv", ["translate_paper.py", "nonexistent.pdf"])

        with pytest.raises(SystemExit):
            translate_paper.main()

        fake_stdout.reconfigure.assert_called_once_with(encoding="utf-8")


    def test_main_does_not_reconfigure_stdout_when_already_utf8(self, monkeypatch):
        """既にUTF-8の標準出力に対しては、無用なreconfigure呼び出しを
    行わないことを確認する（境界条件。上のテストと対をなす）。"""
        fake_stdout = MagicMock()
        fake_stdout.encoding = "utf-8"
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        monkeypatch.setattr(sys, "argv", ["translate_paper.py", "nonexistent.pdf"])

        with pytest.raises(SystemExit):
            translate_paper.main()

        fake_stdout.reconfigure.assert_not_called()


class TestMainErrorHandling:
    """main()自体は、sys.argvの実解析・全工程の実行を伴うため、これまで
    一度もテストされていなかった（test_run_pipeline_end_to_end_with_
    mocked_translation等の全体テストは、main()の内部処理を個別関数の
    直接呼び出しとして再現しているだけで、main()自体は一切呼んでいない）。
    ここではmain()自身が担う「例外→分かりやすいSystemExitへの変換」
    ロジックのみを対象とする（各工程の処理内容自体は他のtest_stageN.pyで
    検証済み）。"""

    def test_main_converts_chapter_resolution_error_to_system_exit(self, monkeypatch):
        """--chapter指定時、目次(TOC)を持たないPDF（sample0.pdf）に対する
    resolve_page_rangeのChapterResolutionErrorが、main()により
    分かりやすいSystemExitへ変換されることを確認する（MinerUを一切
    起動する前にresolve_page_rangeの時点で失敗するため無課金・高速）。"""
        monkeypatch.setattr(sys, "argv", ["translate_paper.py", str(SAMPLE_PDF_PATH), "--chapter", "1"])
        with pytest.raises(SystemExit, match="章指定の解決に失敗しました"):
            translate_paper.main()


    def test_main_converts_page_label_resolution_error_to_system_exit(self, monkeypatch):
        """--start-label指定時、印刷ページラベル情報を持たないPDF
    （sample0.pdf）に対するresolve_page_rangeのPageLabelResolutionErrorが、
    main()により分かりやすいSystemExitへ変換されることを確認する。"""
        monkeypatch.setattr(
            sys, "argv", ["translate_paper.py", str(SAMPLE_PDF_PATH), "--start-label", "nonexistent-label"]
        )
        with pytest.raises(SystemExit, match="印刷ページラベルの解決に失敗しました"):
            translate_paper.main()


    def test_main_converts_translation_backend_error_to_system_exit(self, monkeypatch, tmp_path):
        """翻訳エンジン側の障害（TranslationBackendError）が、main()により
    分かりやすいSystemExitへ変換されることを確認する。PDF解析（工程2）・
    構造化（工程3）は実行コストを避けるためモックし、_resolve_snapshot_dir
    も実際のcache/配下を汚さないようNoneに固定する（このテストの対象は
    あくまでmain()の例外変換ロジックであり、スナップショット機構は
    test_run_pipeline_end_to_end_with_real_deepl側で別途検証済み）。"""
        monkeypatch.setattr(
            sys, "argv", ["translate_paper.py", str(SAMPLE_PDF_PATH), str(tmp_path / "out")]
        )
        monkeypatch.setattr(translate_paper, "process_pdf", lambda *a, **k: [])
        monkeypatch.setattr(translate_paper, "prepare_translation_input", lambda output_dir: ([], ""))
        monkeypatch.setattr(translate_paper, "_resolve_snapshot_dir", lambda *a, **k: None)

        def _raise_backend_error(*_args, **_kwargs):
            raise translate_paper.TranslationBackendError("boom")

        monkeypatch.setattr(translate_paper, "translate_units", _raise_backend_error)

        with pytest.raises(SystemExit, match="翻訳に失敗しました"):
            translate_paper.main()


class TestMainHappyPath:
    def test_main_runs_all_stages_in_order_and_writes_snapshots(self, monkeypatch, tmp_path):
        """main()自体を実際に呼び出し、正常系（例外を送出せず完了する
    パス）を検証する唯一のテスト。他の全体テスト（test_run_pipeline_end_
    to_end_with_mocked_translation等）はmain()の内部処理を個別関数の
    直接呼び出しとして再現しているだけで、main()自体は一度も呼んで
    いない。

    工程(2)（process_pdf。MinerU実行を伴う）・工程(4)
    （translate_units。実際の翻訳エンジンへの通信を伴う）・工程(7)
    （render_units_to_pdfs。Playwright起動を伴う）は実行コストを避ける
    ためモックするが、工程(3)（prepare_translation_input）・工程(5)
    （apply_restore）・工程(6)（stage6_postprocess）はモックせず実際に
    実行し、main()が各工程の戻り値を次の工程へ正しい引数で渡している
    （工程間の配線自体）を検証する。_resolve_snapshot_dirはtmp_path配下を
    指すようモックし、実際のcache/配下を汚さない。"""
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        (output_dir / "page_01_en.md").write_text(
            "[P1-TITLE] A Title\n[P1-S1-1.introduction-S1] Hello world.\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            sys, "argv", ["translate_paper.py", str(SAMPLE_PDF_PATH), str(output_dir)]
        )
        # 工程(2): 実際にはoutput_dir配下へpage_XX_en.mdを書き出す関数だが、
        # 上で既に用意済みのファイルをそのまま使うため何もしない（工程(3)の
        # prepare_translation_inputは実際に呼び、そのファイルを読ませる）。
        monkeypatch.setattr(translate_paper, "process_pdf", lambda *a, **k: [])

        def _fake_translate_units(units, document_context, log):
            return {
                unit.tag: RawTranslationResult(raw_text="こんにちは世界。", math_spans=[])
                for unit in units
                if unit.translatable
            }

        monkeypatch.setattr(translate_paper, "translate_units", _fake_translate_units)

        snapshot_dir = tmp_path / "snapshot"
        monkeypatch.setattr(translate_paper, "_resolve_snapshot_dir", lambda *a, **k: snapshot_dir)

        fake_pdf_path = tmp_path / "paper_bilingual.pdf"
        fake_pdf_path.write_bytes(b"%PDF-fake")
        monkeypatch.setattr(
            translate_paper, "render_units_to_pdfs", lambda units, output_dir, log: [fake_pdf_path]
        )

        translate_paper.main()  # 例外を送出せず完了することの確認そのものがこのテストの主旨

        # 工程(3)〜(5)の配線: prepare_translation_inputが読んだ本文が、
        # モック済みtranslate_unitsの結果を経て、実際のapply_restoreにより
        # ja_textへ正しく書き込まれている。
        structured_context = (snapshot_dir / "03_structured" / "document_context.txt").read_text(encoding="utf-8")
        assert "A Title" in structured_context
        assert (snapshot_dir / "04_deepl_output" / "raw_deepl_results.json").exists()
        restored_units = json.loads((snapshot_dir / "05_restored" / "units_raw.json").read_text(encoding="utf-8"))
        assert any(u["ja_text"] == "こんにちは世界。" for u in restored_units)

        # 工程(6): stage6_postprocessは実際に実行され、保護後の内容で
        # output_dir配下のpage_01_ja.mdを書き出している。
        ja_page = (output_dir / "page_01_ja.md").read_text(encoding="utf-8")
        assert "こんにちは世界。" in ja_page




