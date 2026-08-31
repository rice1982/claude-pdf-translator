"""統合テスト（工程横断）。

工程(1)〜(7)のいずれか1つには分類できないテスト群。全て
prepare_translation_input・translate_units・apply_restore・
stage6.postprocess・render_units_to_pdfs（工程(3)〜(7)をまとめて実行する、
main()と同じ関数の並び）を呼ぶため、複数工程の連携そのものを検証対象に
している。
"""
from __future__ import annotations

import json

import fitz  # PyMuPDF
import pytest

import mainCode.whole_pipeline.whole_pipeline as translate_paper
from mainCode.stage4.stage4 import RawTranslationResult
from mainCode.stage6.stage6 import (
    find_untranslated_fragment_candidates,
    merge_comparison_math_spans,
    merge_function_call_math_spans,
    protect_confirmed_single_letter_leaks,
)
from mainCode.stage3.stage3 import exclude_references_section, parse_output_dir
from mainCode.shared.shared import DocUnit

from conftest import _find_latest_real_deepl_cache


class TestTranslateAndExportIntegration:
    @pytest.mark.parametrize(
        "processed_fixture_name,run_id",
        [
            ("processed", "sample0"),
            ("processed_sample1", "sample1"),
            ("processed_sample2", "sample2"),
            ("processed_sample3", "sample3_label55-60"),
        ],
        ids=["sample0", "sample1", "sample2", "sample3_label55-60"],
    )
    def test_translate_and_export_with_mocked_translation(self, request, processed_fixture_name, run_id, monkeypatch):
        """CLAUDE.mdの規定により、pytestでの翻訳テストは外部の翻訳エンジンへ
    実際に通信しないよう、使用する翻訳エンジンの呼び出し自体をモックする
    （``translate_paper.translate_units``を差し替える）。

    既にMinerU解析済みの``processed``系フィクスチャのoutput_dir（実際の
    図・数式画像を含む）に対し、工程(3)〜工程(7)（タグ解析・翻訳・
    PDF生成）を実行し、
    - 3種類のPDFが空でなく生成されること
    - paper_ja.pdfに実際に日本語訳が書き込まれていること（原文の
      英語のままではなく、恒等関数でもないこと）
    を確認する。MinerU自体は再実行しない（fixtureの結果を再利用する）ため、
    実行時間は翻訳・PDF生成の分のみで済む。翻訳エンジンを呼ばないため、
    run_idで`-k sample0`〜`sample3`のように対象サンプルを自由に選んで
    実行できる（sample3のみCLAUDE.mdの規定により印刷ページラベル55〜60の
    範囲に限定）。このrun_idはcache/配下のフォルダ名（工程(5)(6)の実データ
    テストが使うものと同じ命名規則）と揃えてあるが、本テスト自体はDeepL
    出力キャッシュ（real_deepl_output/）を一切参照しない。
    """
        processed = request.getfixturevalue(processed_fixture_name)
        output_dir = processed["output_dir"]

        # 参考文献セクションのみで構成されるページ（exclude_references_sectionに
        # より全unitが翻訳対象外になる）では、モック訳文字列を含まなくて正常。
        # sample0（2ページ抜粋）では発生しないが、sample1/sample2のような実論文
        # 全体では実際に起こりうるため、prepare_translation_input実行前に
        # 判定しておく。
        pre_units = parse_output_dir(output_dir)
        exclude_references_section(pre_units)
        translatable_pages = {u.page for u in pre_units if u.translatable}

        def _fake_translate_units(units, document_context, log=print):
            return {
                unit.tag: RawTranslationResult(raw_text="これはテスト用の日本語訳です。", math_spans=[])
                for unit in units
                if unit.translatable
            }

        monkeypatch.setattr(translate_paper, "translate_units", _fake_translate_units)

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

        # stage6.postprocess内でwrite_translated_pagesが呼ばれ、page_XX_en.mdと
        # 対になるpage_XX_ja.mdが生成されているか（ページ番号はサンプルごとに
        # 異なるため、processedが実際に生成したページ別Markdownのファイル名
        # から動的に期待値を組み立てる。sample0は2ページ、sample1は9ページ、
        # sample2は23ページ、sample3は67〜72ページ等、ハードコードしない）。
        expected_ja_names = sorted(md_path.name.replace("_en.md", "_ja.md") for md_path in processed["md_paths"])
        ja_md_paths = sorted(output_dir.glob("page_*_ja.md"))
        assert [p.name for p in ja_md_paths] == expected_ja_names
        for path in ja_md_paths:
            page_num = int(path.stem.split("_")[1])
            if page_num in translatable_pages:
                assert "これはテスト用の日本語訳です。" in path.read_text(encoding="utf-8")


    def test_translate_and_export_translates_all_translatable_units(self, tmp_path, monkeypatch):
        """test_translate_and_export_with_mocked_translationは「日本語が1文字でも
    あればPASS」という緩い検証のため、翻訳対象文の一部しか訳されていない
    （例: 5文中1文しか訳されていない）不具合を検知できない。この弱点を
    補強するため、翻訳対象の文数が既知（5文: title 1 + heading 1 +
    body_sentence 3）の自作Markdownを用意し、モック訳文字列が出力PDFに
    ちょうどその数だけ出現することを確認する。process_pdf/MinerUを
    一切経由しないため、数秒で完了する。
    """
        output_dir = tmp_path / "g_all_units_input"
        output_dir.mkdir()
        mock_ja = "これはテスト用の日本語訳です。"
        (output_dir / "page_01_en.md").write_text(
            "[P1-TITLE] A Study of Something Interesting\n"
            "[P1-AUTHORS] Jane Doe\n"
            "[P1-HEADING-1.introduction] 1. INTRODUCTION\n"
            "[P1-S1-1.introduction-S1] This is the first sentence.\n"
            "[P1-S2-1.introduction-S2] This is the second sentence.\n"
            "[P1-S3-1.introduction-S3] This is the third sentence.\n",
            encoding="utf-8",
        )

        def _fake_translate_units(units, document_context, log=print):
            return {
                unit.tag: RawTranslationResult(raw_text=mock_ja, math_spans=[]) for unit in units if unit.translatable
            }

        monkeypatch.setattr(translate_paper, "translate_units", _fake_translate_units)

        units, document_context = translate_paper.prepare_translation_input(output_dir)
        raw_results = translate_paper.translate_units(units, document_context)
        translate_paper.apply_restore(units, raw_results)
        translate_paper.stage6_postprocess(units, output_dir)
        pdf_paths = translate_paper.render_units_to_pdfs(units, output_dir)
        ja_pdf_path = next(p for p in pdf_paths if p.name == "paper_ja.pdf")
        with fitz.open(ja_pdf_path) as doc:
            ja_text = "".join(page.get_text() for page in doc)

        translated_count = ja_text.count(mock_ja)
        assert translated_count == 5, (
            f"翻訳対象の5文（title/heading/body_sentence x3）すべてに訳文が"
            f"反映されているはずが、{translated_count}回しか出現しなかった"
            f"（一部の文が未翻訳のまま残っている可能性がある）"
        )



# 未知候補チェックで使う許可リスト。sample0.pdfのpage_01/page_02を実際に
# DeepLで翻訳し、find_untranslated_fragment_candidatesが検出した全候補を
# 人間が目視確認した結果を記録したもの。
#
# ここに無い新しい候補が見つかった場合はテストが失敗する。その候補を
# 実際のpage_XX_ja.md・原文PDFで目視確認した上で、
#   (a) 固有名詞・略語・列挙記号など数式ではないもの
#       → KNOWN_FALSE_POSITIVE_FRAGMENTSへ追加
#   (b) $...$で保護されていない本物の数式
#       → KNOWN_LEAKED_MATH_FRAGMENTSへ追加（可能ならMinerU/analyze_
#         structure側での$...$保護も検討する）
# のどちらかに追加すること。単にリストに追加するだけでテストを通す
# ことが目的化しないよう注意（人間が確認した記録として運用する）。
KNOWN_FALSE_POSITIVE_FRAGMENTS = {
    "NMS",  # 略語（Non-Maximum Suppression）。P1-S16。
    "CFG",  # 略語（Classifier-Free Guidance）。P1-S32。
    "Classifier-Free Guidance (CFG)",  # 略語の展開形。P1-S32。DeepLが
    # 略語をそのまま残す代わりに正式名称＋略語をまとめて残すことがある
    # （言い回しの揺れ）。
    "DiT",  # 固有名詞（モデル名）。P2-FIG1-CAPTION-S3。
    "LoRA",  # 固有名詞（手法名）。P2-FIG1-CAPTION-S3。
    "FLUX",  # 固有名詞（モデル名）。P2-S13。
    "(ODE)",  # 略語（Ordinary Differential Equation）。P2-S7。
    "ODE",  # 同上の表記ゆれ。P2-S7。DeepLが全角括弧「（ODE）」で出力する
    # ことがあり、find_untranslated_fragment_candidatesは半角部分のみを
    # 抽出するため"(ODE)"とは別トークンになる。
    "(i)", "(ii)", "(iii)",  # 列挙記号。P2-S4。
}
KNOWN_LEAKED_MATH_FRAGMENTS: set[str] = set()
# ギリシャ文字（wrap_bare_greek_letters）・単体アルファベット
# （protect_confirmed_single_letter_leaks）・"1文字=値"形式
# （wrap_bare_letter_equals_expressions）はいずれも構造解析・後処理段階で
# 自動的に$...$保護されるようになったため、この許可リストに追加する必要は
# ない（詳細はstage2.py/stage6.pyの該当関数のdocstring
# 参照）。"DiT"/"NMS"のような複数文字のトークンは実在の固有名詞・略語と
# 区別できないため、引き続き検出のみ・許可リストでの容認とする。

# sample1.pdf（表を含む論文フルサイズ、9ページ）向けの許可リスト。
# sample0.pdfの許可リストとは別の論文・別の語彙のため独立して管理する。
# 中身はtest_page_ja_md_and_candidate_detection_against_real_deepl_sample1
# の初回実行結果を人間が目視確認して追加したもの。
KNOWN_FALSE_POSITIVE_FRAGMENTS_SAMPLE1: set[str] = {
    "NMS", "CFG",  # 略語（Non-Maximum Suppression / Classifier-Free Guidance）。
    "DiT", "LoRA", "FLUX",  # 固有名詞（モデル・手法名）。
    "(ODE)", "ODE",  # 略語（Ordinary Differential Equation）とその表記ゆれ。
    "(i)", "(ii)", "(iii)",  # 列挙記号。
    "GED",  # 固有名詞（比較対象手法名）。
    "ODS", "OIS",  # 評価指標の略語（Optimal Dataset/Image Scale）。
    "IoU",  # 評価指標の略語（Intersection over Union）。
    "A.", "A.1.", "A.1", "A.2.", "A.2",  # 付録の章番号・参照（Appendix A）。
    "B.", "B.1.", "B.1", "B.2.", "B.2",  # 同上（Appendix B）。
    "C.", "C.1.", "C.1", "C.2.", "C.2",  # 同上（Appendix C）。
}
KNOWN_LEAKED_MATH_FRAGMENTS_SAMPLE1: set[str] = set()

# sample3.pdf向けの許可リスト。sample0.pdf/sample1.pdfの許可リストとは
# 別の書籍・別の語彙のため独立して管理する。印刷ページラベル56
# （物理68、MinerUバックエンドvlm-engine）・印刷ページラベル55〜60
# （物理67〜72、MinerUバックエンドpipeline）の両方で共通のリストとして
# 扱う（同じ書籍の語彙のため重複が多く、範囲・バックエンドごとに分ける
# 必要が無いと判断した）。中身は
# test_run_pipeline_end_to_end_with_real_deepl[sample3_label56-56_vlm-engine_deepl]
# / [sample3_label55-60_pipeline_deepl]の実行結果を人間が目視確認して追加したもの。
KNOWN_FALSE_POSITIVE_FRAGMENTS_SAMPLE3: set[str] = {
    "PGM",  # 固有名詞（Probabilistic Graphical Modelの略語）。P68-S3等。
    "PGMS",  # 上記の複数形表記。見出しP70-HEADING-u5.graphical。
    "SIR",  # 略語（Sampling Importance Resampling）。P69-S15。
    "MCMC",  # 略語（Markov Chain Monte Carlo）。P69-S15/S16/S18。
    "KL",  # 略語（Kullback-Leibler）。P70-S3/S4/S5。
    "ELBO",  # 略語（Evidence Lower BOund）。P70-S5/S6。
    "DAG",  # 略語（Directed Acyclic Graph）。P71-S2。
    "VAE",  # 略語（Variational Autoencoder）。P72-S3。
    "1c",  # 図のサブパネル参照（"Figure 1c"）。P72-S3。列挙記号扱い。
    "AI",  # 略語（Artificial Intelligence）。P72-S5。
}
KNOWN_LEAKED_MATH_FRAGMENTS_SAMPLE3: set[str] = set()





class TestUntranslatedFragmentCandidatesAgainstDeepl:
    @pytest.mark.parametrize(
        "known_false_positives,known_leaked,run_id",
        [
            (KNOWN_FALSE_POSITIVE_FRAGMENTS, KNOWN_LEAKED_MATH_FRAGMENTS, "sample0_full"),
            (KNOWN_FALSE_POSITIVE_FRAGMENTS_SAMPLE1, KNOWN_LEAKED_MATH_FRAGMENTS_SAMPLE1, "sample1_full"),
            (KNOWN_FALSE_POSITIVE_FRAGMENTS_SAMPLE3, KNOWN_LEAKED_MATH_FRAGMENTS_SAMPLE3, "sample3_label56-56"),
            (KNOWN_FALSE_POSITIVE_FRAGMENTS_SAMPLE3, KNOWN_LEAKED_MATH_FRAGMENTS_SAMPLE3, "sample3_label55-60"),
        ],
        ids=["sample0", "sample1", "sample3_label56-56", "sample3_label55-60"],
    )
    def test_untranslated_fragment_candidates_against_cached_deepl_output(self, known_false_positives, known_leaked, run_id):
        """実際にDeepLで翻訳済みのunits_raw.jsonスナップショット（
    test_run_pipeline_end_to_end_with_real_deeplの実行、または人間による
    本実行が残したもの。`_find_latest_real_deepl_cache`で最新版を取得）を
    使い、未保護の数式らしき候補の許可リスト回帰チェックを行う。無課金・
    DeepLを一切呼ばない（意図的にテスト名へ"real_deepl"を含めていない。
    CLAUDE.mdの実行前確認ルールは名前に"real_deepl"を含むテストが対象
    のため、無課金の本テストが誤ってその対象に含まれないようにするため）。

    「実DeepL呼び出し（凍結データの生成）」と「許可リスト突合（コード側
    リグレッションの検知）」は目的が異なる。後者は前者の凍結データさえ
    あれば無課金・毎回実行できるため、本テストとして分離している。凍結
    データの生成元は、全体テストの実DeepL版
    （test_run_pipeline_end_to_end_with_real_deepl）または人間による通常の
    本実行（`translate_paper.py`。実行するたびに自動的にcache/配下へ
    スナップショットが残る）のいずれかに一本化している。

    units_raw.jsonはapply_restore直後・protect_confirmed_single_letter_
    leaks適用前のスナップショットのため、本番のstage6.postprocessと
    同じ順序でprotect_confirmed_single_letter_leaks・
    merge_function_call_math_spans・merge_comparison_math_spansをここに
    適用してから候補チェックする（そうしないと、本来自動保護・統合されて
    候補から除外されるはずの単体アルファベット数式変数、関数呼び出し
    記法の引数だけ保護された断片、比較演算子付きの数式断片が、未知候補
    として誤検出されてしまう）。

    DeepLの翻訳結果は完全に決定的ではなく、言い回しの変化により許可
    リストに無い新しい候補（真の数式漏れとは限らず、誤検知の場合もある）
    が出現し、凍結データが更新されるたびに失敗することがある。その場合は
    実際のpage_XX_ja.md・原文PDFで目視確認の上、誤検知ならKNOWN_FALSE_
    POSITIVE_FRAGMENTSへ、本物の数式漏れならKNOWN_LEAKED_MATH_FRAGMENTS
    へ追加すること（“取りあえず許可リストに足してテストを通す”という運用
    は本チェックの目的を損なうため避けること）。
    """
        cache_root = _find_latest_real_deepl_cache(run_id)
        units_raw_path = cache_root / "05_restored" / "units_raw.json" if cache_root else None
        if units_raw_path is None or not units_raw_path.exists():
            pytest.skip(
                f"{run_id} の実DeepLキャッシュが無いためスキップ"
                "（test_run_pipeline_end_to_end_with_real_deeplの実行、または"
                "translate_paper.pyでの本実行により生成される）"
            )

        units = [DocUnit(**d) for d in json.loads(units_raw_path.read_text(encoding="utf-8"))]
        protect_confirmed_single_letter_leaks(units, log=lambda _msg: None)
        merge_function_call_math_spans(units, log=lambda _msg: None)
        merge_comparison_math_spans(units, log=lambda _msg: None)

        known = known_false_positives | known_leaked
        unexpected: dict[str, list[str]] = {}
        for unit in units:
            if not unit.translatable:
                continue
            new_tokens = [t for t in find_untranslated_fragment_candidates(unit.ja_text) if t not in known]
            if new_tokens:
                unexpected[unit.tag] = new_tokens
        assert not unexpected, (
            "許可リストに無い未知の候補が見つかりました。実際のpage_XX_ja.md・"
            "原文PDFで目視確認の上、誤検知ならKNOWN_FALSE_POSITIVE_FRAGMENTSへ、"
            "本物の数式漏れならKNOWN_LEAKED_MATH_FRAGMENTSへ追加してください: "
            f"{unexpected}"
        )
