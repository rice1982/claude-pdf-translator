"""工程(1): ページ範囲の決定のテスト。

対応関数: parse_chapter_spec / resolve_chapter_page_range /
resolve_physical_page / resolve_physical_page_range / resolve_page_range /
translate_paper._require_pdf_exists。
fitzで組み立てる合成PDFのみで完結し、MinerU・DeepLを一切使わない
（sample3.pdfを使う2テストのみ、CLAUDE.mdの「実データ最低1テスト」規定に
沿って例外的に実データを使う。無ければskipする）。
"""
from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF
import pytest

from mainCode.stage1.stage1 import (
    ChapterResolutionError,
    PageLabelResolutionError,
    parse_chapter_spec,
    resolve_chapter_page_range,
    resolve_page_range,
    resolve_physical_page,
    resolve_physical_page_range,
)
import mainCode.whole_pipeline.whole_pipeline as translate_paper

from conftest import SAMPLE_PDF_PATH, SAMPLE3_PDF_PATH


def _build_synthetic_toc_pdf(
    path: Path, page_count: int, toc: list[list], page_labels: list[dict] | None = None
) -> None:
    doc = fitz.open()
    for _ in range(page_count):
        doc.new_page()
    doc.set_toc(toc)
    if page_labels is not None:
        doc.set_page_labels(page_labels)
    doc.save(str(path))
    doc.close()



def _build_synthetic_labeled_pdf(path: Path, page_count: int, page_labels: list[dict] | None = None) -> None:
    doc = fitz.open()
    for _ in range(page_count):
        doc.new_page()
    if page_labels is not None:
        doc.set_page_labels(page_labels)
    doc.save(str(path))
    doc.close()





class TestPageRangeResolution:
    @pytest.mark.parametrize(
        "spec,expected",
        [
            ("1,2", [1, 2]),
            ("1-2", [1, 2]),
            ("1,3-4", [1, 3, 4]),
            ("2", [2]),
            ("2,1", [1, 2]),  # 順序に依存せず昇順ソートされる
            ("1, 2", [1, 2]),  # カンマ区切りの前後の空白は無視される
            (",1,2,", [1, 2]),  # 先頭・末尾・連続するカンマによる空要素は無視される
            ("1-3,2-4", [1, 2, 3, 4]),  # 範囲指定同士が重なっても重複排除される
        ],
    )
    def test_parse_chapter_spec_valid(self, spec, expected):
        assert parse_chapter_spec(spec) == expected


    @pytest.mark.parametrize("spec", ["", "0", "abc", "2-1", "1-", "-1"])
    def test_parse_chapter_spec_invalid(self, spec):
        with pytest.raises(ChapterResolutionError):
            parse_chapter_spec(spec)


    def test_resolve_chapter_page_range_requires_toc(self, tmp_path):
        """目次（TOC/Outline）が無いPDFでは、--chapterではなく--start/--endを
    使うよう案内する例外を送出する。"""
        pdf_path = tmp_path / "no_toc.pdf"
        _build_synthetic_toc_pdf(pdf_path, page_count=3, toc=[])
        with pytest.raises(ChapterResolutionError):
            resolve_chapter_page_range(pdf_path, "1")


    def test_resolve_chapter_page_range_computes_ranges_from_toc(self, tmp_path):
        """章番号→物理ページ範囲への変換ロジックを、合成TOCで決定的に検証する。

    次の一般的な性質を検証する（特定の論文の目次構成には依存しない）。
    - 最上位階層（レベル最小値）のみを章として数え、下位階層（節等、
      ここではlevel=2）は無視する。
    - 章の終了ページは「その章の開始ページより大きい最小の開始ページの
      直前」（最終章は文書の最終ページ）。
    - 複数章指定はそれらを包含する最小の連続範囲になる。
    """
        toc = [
            [1, "Chapter 1", 1],
            [2, "Chapter 1.1", 2],
            [1, "Chapter 2", 4],
            [2, "Chapter 2.1", 5],
            [2, "Chapter 2.2", 7],
            [1, "Chapter 3", 9],
        ]
        pdf_path = tmp_path / "toc.pdf"
        _build_synthetic_toc_pdf(pdf_path, page_count=12, toc=toc)

        assert resolve_chapter_page_range(pdf_path, "1") == (1, 3)
        assert resolve_chapter_page_range(pdf_path, "2") == (4, 8)
        # 最終章（章3）は次の開始ページが無いため、文書の最終ページまでとなる
        assert resolve_chapter_page_range(pdf_path, "3") == (9, 12)
        # 複数章指定はそれらを包含する最小の連続範囲になる
        assert resolve_chapter_page_range(pdf_path, "1,2") == (1, 8)
        assert resolve_chapter_page_range(pdf_path, "1-3") == (1, 12)


    def test_resolve_chapter_page_range_numbers_chapters_by_toc_list_order(self, tmp_path):
        """章番号は、目次(TOC)の物理ページ順ではなく「TOCリストに登場する順」
    で振られる。終了ページの計算（_end_page_for）は開始ページの大小関係
    だけを使うため、ブックマークの並びが物理ページ順と一致しない文書
    でもクラッシュしたり不正な範囲にはならないが、「章1」が指す実体は
    物理的に最初の章ではなく、TOCリスト上で最初に現れる項目になる点に
    注意（`resolve_chapter_page_range`の実装コメント（ステップ5）が述べる
    「開始ページの大小関係だけで安定して終了ページを決められる」のは
    終了ページの計算についてであり、章番号の割り当てそのものが
    物理ページ順になることは保証しない）。"""
        toc = [
            [1, "Chapter 3", 9],
            [1, "Chapter 1", 1],
            [1, "Chapter 2", 4],
        ]
        pdf_path = tmp_path / "out_of_order_toc.pdf"
        _build_synthetic_toc_pdf(pdf_path, page_count=12, toc=toc)

        # 章1 = TOCリストの1番目の項目（"Chapter 3"、物理ページ9開始）
        assert resolve_chapter_page_range(pdf_path, "1") == (9, 12)
        # 章2 = TOCリストの2番目の項目（"Chapter 1"、物理ページ1開始）。
        # 終了ページは開始ページの大小関係から正しく計算される（次に大きい
        # 開始ページである4の直前）。
        assert resolve_chapter_page_range(pdf_path, "2") == (1, 3)
        assert resolve_chapter_page_range(pdf_path, "3") == (4, 8)
        assert resolve_chapter_page_range(pdf_path, "1,2") == (1, 12)


    def test_resolve_chapter_page_range_duplicate_top_level_pages_do_not_crash(self, tmp_path):
        """同じ物理ページを指す複数の最上位階層TOC項目（例: ネストされた
    ブックマークの重複登録）があっても例外にならず、それらの章番号は
    いずれも同じページ範囲へ解決される。"""
        toc = [
            [1, "Chapter 1", 1],
            [1, "Chapter 1 (重複したブックマーク)", 1],
            [1, "Chapter 2", 6],
        ]
        pdf_path = tmp_path / "dup_toc.pdf"
        _build_synthetic_toc_pdf(pdf_path, page_count=10, toc=toc)

        assert resolve_chapter_page_range(pdf_path, "1") == (1, 5)
        assert resolve_chapter_page_range(pdf_path, "2") == (1, 5)
        assert resolve_chapter_page_range(pdf_path, "3") == (6, 10)


    def test_resolve_chapter_page_range_out_of_range(self, tmp_path):
        """目次に存在しない章番号を指定した場合はエラーになる（合成TOCは
    章3件分のみなので、章番号10の指定はエラーになる）。"""
        toc = [[1, "Chapter 1", 1], [1, "Chapter 2", 2], [1, "Chapter 3", 3]]
        pdf_path = tmp_path / "toc.pdf"
        _build_synthetic_toc_pdf(pdf_path, page_count=3, toc=toc)
        with pytest.raises(ChapterResolutionError):
            resolve_chapter_page_range(pdf_path, "10")


    def test_resolve_chapter_page_range_without_page_labels_uses_all_top_level_entries(self, tmp_path):
        """印刷ページラベルの情報を持たないPDF（学術論文PDF等。fitzのget_label
    が全ページで空文字列を返す）では、前付け判定は行わず、目次の
    最上位階層すべてを章として数える。合成PDFは
    page_labelsを指定しないため、sample2.pdfのような「印刷ページラベル
    情報が無い」状態を再現する。"""
        toc = [[1, "Chapter 1", 1], [1, "Chapter 2", 3], [1, "Chapter 3", 5]]
        pdf_path = tmp_path / "no_page_labels.pdf"
        _build_synthetic_toc_pdf(pdf_path, page_count=6, toc=toc)

        assert resolve_chapter_page_range(pdf_path, "1") == (1, 2)
        assert resolve_chapter_page_range(pdf_path, "1,2") == (1, 4)


    def test_resolve_chapter_page_range_falls_back_when_toc_entries_are_all_front_matter(self, tmp_path):
        """印刷ページラベルの情報自体は存在するが、目次の項目が指す物理
    ページが全てローマ数字（前付け）の場合も、本文ページが1件も見つから
    ないため、上のtest_..._without_page_labels_...と同じフォールバック
    （最上位階層すべてを章として数える）が働くことを確認する。

    上のテストは「ラベル情報自体が無い」PDF（get_labelが常に空文字列）
    を検証しており、_is_body_pageはどちらのケースも最終的にFalseを
    返す点は同じだが、「ラベル情報はあるが実際の値が非数字」という
    別の入力経路からも同じフォールバックへ到達することは未検証だった。
    """
        toc = [[1, "Preface", 1], [1, "Foreword", 3], [1, "Acknowledgments", 5]]
        pdf_path = tmp_path / "all_front_matter.pdf"
        _build_synthetic_toc_pdf(
            pdf_path, page_count=6, toc=toc,
            page_labels=[{"startpage": 0, "style": "r", "firstpagenum": 1}],
        )

        assert resolve_chapter_page_range(pdf_path, "1") == (1, 2)
        assert resolve_chapter_page_range(pdf_path, "1,2") == (1, 4)


    def test_resolve_chapter_page_range_skips_roman_numeral_front_matter(self):
        """唯一の実データ確認（sample3.pdf）。前付け（ローマ数字ページ）が
    章として数えられず、本文の算用数字ページから第1章が始まることを
    確認する（CLAUDE.mdの「実データ最低1テスト」規定）。"""
        if not SAMPLE3_PDF_PATH.exists():
            pytest.skip("sample3.pdf がないためスキップ")
        assert resolve_chapter_page_range(SAMPLE3_PDF_PATH, "1") == (17, 49)
        # 第1・2章 = Part I 〜 Part II（Part III開始=物理ページ89の直前）
        assert resolve_chapter_page_range(SAMPLE3_PDF_PATH, "1,2") == (17, 88)


    def test_resolve_physical_page_range_handles_label_gap(self, tmp_path):
        """印刷ページ番号にギャップ（欠番）がある場合でも正しく変換できるか。"""
        pdf_path = tmp_path / "gap.pdf"
        page_labels = [
            {"startpage": 0, "style": "D", "firstpagenum": 1},
            {"startpage": 3, "style": "D", "firstpagenum": 5},
        ]
        _build_synthetic_labeled_pdf(pdf_path, page_count=6, page_labels=page_labels)
        assert resolve_physical_page_range(pdf_path, "3", "5") == (3, 4)


    def test_resolve_physical_page_unknown_label_raises(self, tmp_path):
        """存在しない印刷ページラベルを指定した場合にエラーになるか。"""
        pdf_path = tmp_path / "labeled.pdf"
        _build_synthetic_labeled_pdf(
            pdf_path, page_count=3, page_labels=[{"startpage": 0, "style": "D", "firstpagenum": 1}]
        )
        with pytest.raises(PageLabelResolutionError):
            resolve_physical_page(pdf_path, "zzz")


    def test_resolve_physical_page_without_page_labels_raises(self, tmp_path):
        """印刷ページラベルの情報を持たないPDF（学術論文PDF等）では、
    どのラベルを指定してもエラーになる（--start-labelではなく
    --start/--endを使うよう案内される）。"""
        pdf_path = tmp_path / "no_labels.pdf"
        _build_synthetic_labeled_pdf(pdf_path, page_count=3, page_labels=None)
        with pytest.raises(PageLabelResolutionError):
            resolve_physical_page(pdf_path, "1")


    def test_resolve_physical_page_for_sample3(self):
        """唯一の実データ確認その2（sample3.pdf）。"cov"の短縮形一致・大文字
    小文字同一視・ローマ数字前付けが実際の書籍PDFで正しく解決されるか。"""
        if not SAMPLE3_PDF_PATH.exists():
            pytest.skip("sample3.pdf がないためスキップ")
        assert resolve_physical_page(SAMPLE3_PDF_PATH, "cov") == 1  # 短縮形（実ラベルは"Cover"）
        assert resolve_physical_page(SAMPLE3_PDF_PATH, "COV") == 1  # 大文字小文字を区別しない
        assert resolve_physical_page(SAMPLE3_PDF_PATH, "Cover") == 1
        assert resolve_physical_page(SAMPLE3_PDF_PATH, "i") == 2
        assert resolve_physical_page(SAMPLE3_PDF_PATH, "xviii") == 16
        assert resolve_physical_page(SAMPLE3_PDF_PATH, "55") == 67  # 本文開始後の算用数字ページ
        assert resolve_physical_page(SAMPLE3_PDF_PATH, "60") == 72


    def test_resolve_physical_page_case_insensitive_and_cov_shorthand_on_synthetic_pdf(self, tmp_path):
        """"cov"の短縮形一致・大文字小文字同一視は、特定の実サンプルPDFの
    存在に依存しないアルゴリズムの性質であるため、合成PDFでも独立に
    決定的に検証する（sample3.pdfが無い環境では
    test_resolve_physical_page_for_sample3がskipされ、この挙動が一切
    検証されないままになるのを防ぐ）。"""
        pdf_path = tmp_path / "cover.pdf"
        page_labels = [
            {"startpage": 0, "prefix": "Cover", "style": "", "firstpagenum": 1},
            {"startpage": 1, "style": "D", "firstpagenum": 1},
        ]
        _build_synthetic_labeled_pdf(pdf_path, page_count=3, page_labels=page_labels)

        assert resolve_physical_page(pdf_path, "cov") == 1  # 短縮形
        assert resolve_physical_page(pdf_path, "COV") == 1  # 大文字小文字を区別しない
        assert resolve_physical_page(pdf_path, "Cover") == 1  # 完全一致
        assert resolve_physical_page(pdf_path, "1") == 2  # 本文側は通常の完全一致
        with pytest.raises(PageLabelResolutionError):
            # "cov"以外の短縮形（例:"cove"）には前方一致を適用しないため、
            # 完全一致しない限りエラーになる。
            resolve_physical_page(pdf_path, "cove")


    def test_resolve_physical_page_range_returns_none_when_both_labels_omitted(self):
        """start_label・end_labelの両方がNoneの場合、PDFを一切開かずに
    (None, None)を返す（片方だけ指定された場合の挙動は
    test_resolve_physical_page_range_handles_label_gap等で別途確認済み）。
    PDFを開かないことがこのテストの主旨そのものであるため、実在しない
    パスをそのまま渡して検証する（test_resolve_page_range_returns_none_
    when_nothing_specifiedと同じ考え方）。"""
        assert resolve_physical_page_range(Path("dummy.pdf"), None, None) == (None, None)


    def test_resolve_page_range_conflicting_start_and_start_label_raises(self):
        """--startと--start-labelを同時に指定するのは矛盾した指定であり、
    どちらを優先すべきか一意に決まらないためエラーとする。"""
        with pytest.raises(ValueError):
            resolve_page_range(SAMPLE3_PDF_PATH, None, 67, None, "55", None)


    def test_resolve_page_range_conflicting_end_and_end_label_raises(self):
        """--endと--end-labelの組み合わせも、--start/--start-labelと対称に
    矛盾した指定としてエラーになることを確認する（既存の
    test_resolve_page_range_conflicting_start_and_start_label_raisesは
    start側のみを検証しており、end側の同じ分岐は一度も踏まれていな
    かった）。"""
        with pytest.raises(ValueError):
            resolve_page_range(SAMPLE3_PDF_PATH, None, None, 72, None, "60")


    def test_resolve_page_range_combines_physical_start_with_end_label(self, tmp_path):
        """--start（物理ページ）と--end-label（印刷ページラベル）という
    異なる境界同士の組み合わせでも、--startの値が失われずそのまま
    使われることを確認する回帰テスト。

    実データ調査で発見した不具合の再現: 修正前は
    `start_label is not None or end_label is not None`が真になった時点で
    `start, end = resolved_start, resolved_end`が両方を無条件に上書き
    しており、start_labelが指定されていない（＝resolved_startが常に
    None）場合でも、呼び出し側が指定した物理ページ番号のstartがNoneに
    書き潰されていた（例: `--start 5 --end-label 60`が`(None, 72)`に
    なってしまい、5が消えていた）。
    """
        page_labels = [{"startpage": 0, "style": "D", "firstpagenum": 1}]  # ページ1,2,3,...
        pdf_path = tmp_path / "labeled.pdf"
        _build_synthetic_labeled_pdf(pdf_path, page_count=5, page_labels=page_labels)

        assert resolve_page_range(pdf_path, None, 2, None, None, "4") == (2, 4)


    def test_resolve_page_range_combines_start_label_with_physical_end(self, tmp_path):
        """上のテストと対称: --start-label（印刷ページラベル）と--end
    （物理ページ）の組み合わせでも、--endの値が失われないことを確認する。"""
        page_labels = [{"startpage": 0, "style": "D", "firstpagenum": 1}]
        pdf_path = tmp_path / "labeled.pdf"
        _build_synthetic_labeled_pdf(pdf_path, page_count=5, page_labels=page_labels)

        assert resolve_page_range(pdf_path, None, None, 5, "2", None) == (2, 5)


    def test_resolve_page_range_prefers_start_over_chapter(self, tmp_path):
        toc = [[1, "Chapter 1", 1], [1, "Chapter 2", 5]]
        pdf_path = tmp_path / "toc.pdf"
        _build_synthetic_toc_pdf(pdf_path, page_count=8, toc=toc)
        assert resolve_page_range(pdf_path, "1", 3, 4, None, None) == (3, 4)


    def test_resolve_page_range_prefers_label_over_chapter(self):
        """--start-label/--end-labelが指定された場合、--chapterより優先される
    （--start/--endが優先されるのと同じ優先順位）。"""
        start_page, end_page = resolve_page_range(SAMPLE3_PDF_PATH, "1", None, None, "55", "60")
        assert (start_page, end_page) == (67, 72)


    def test_resolve_page_range_falls_back_to_chapter_when_no_page_range_given(self, tmp_path):
        toc = [[1, "Chapter 1", 1], [1, "Chapter 2", 5]]
        pdf_path = tmp_path / "toc.pdf"
        _build_synthetic_toc_pdf(pdf_path, page_count=8, toc=toc)
        assert resolve_page_range(pdf_path, "1", None, None, None, None) == (1, 4)


    def test_resolve_page_range_returns_none_when_nothing_specified(self):
        assert resolve_page_range(Path("dummy.pdf"), None, None, None, None, None) == (None, None)


    def test_require_pdf_exists_raises_for_missing_file(self):
        """存在しない入力PDFパスを指定した場合、fitzの生の例外
    （FileNotFoundError）ではなく、分かりやすいSystemExitで終了すること
    を確認する。"""
        with pytest.raises(SystemExit, match="入力PDFファイルが見つかりません"):
            translate_paper._require_pdf_exists("input/does_not_exist.pdf")


    def test_require_pdf_exists_passes_for_existing_file(self):
        """実在するPDFパスを指定した場合は何も送出しないこと（正常系）を
    確認する。"""
        translate_paper._require_pdf_exists(SAMPLE_PDF_PATH)


    @pytest.mark.parametrize(
        "chapter,start,end,start_label,end_label,expected",
        [
            (None, None, None, None, None, "full"),
            ("1,2", None, None, None, None, "chapter1_2"),
            (None, 66, 71, None, None, "p66-71"),
            (None, None, None, "55", "60", "label55-60"),
            # --start/--end・--start-label/--end-labelはresolve_page_rangeと
            # 同じ優先順位（印刷ページラベル・物理ページ番号 > 章指定）。
            ("1", 66, 71, None, None, "p66-71"),
            ("1", None, None, "55", "60", "label55-60"),
        ],
    )
    def test_describe_page_range(self, chapter, start, end, start_label, end_label, expected):
        """cache/配下のフォルダ名・output/配下の自動生成名に使う人間可読な
    範囲記述子が、resolve_page_rangeと同じ優先順位で組み立てられるかの
    単体テスト。"""
        assert translate_paper.describe_page_range(chapter, start, end, start_label, end_label) == expected


    def test_default_output_dir_builds_manual_naming_convention(self):
        """output_dir省略時にmain()が使う自動生成パスが、testExplain.txtで
    定義した人間による手動実行（「本実行」）と同じ命名規則
    （output/manual_{PDF名}_{範囲記述子}_{実行日時}）になるかを確認する。
    main()自体はargparse・sys.stdout.reconfigure・process_pdf・
    prepare_translation_input等の一連の呼び出しを含み単体テストしにくい
    ため、命名ロジックを切り出したこの関数を直接検証する。"""
        result = translate_paper.default_output_dir(
            "input/sample0.pdf", "full", timestamp="20260101-120000"
        )
        assert result == Path("output") / "manual_sample0_full_20260101-120000"

