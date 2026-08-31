"""testCode/ 配下の9ファイルが共有するフィクスチャ・定数・ヘルパー。

SAMPLE*_PDF_PATH・_REAL_SAMPLE_SCENARIOS・real_deepl_outputキャッシュ
探索・sample0〜3をMinerUで処理済みの`processed`系フィクスチャなど、
複数のテストファイルにまたがって使われるものだけをここに置く。1ファイル
からしか使われないヘルパーは、そのファイル側にローカル定義する。
"""
from __future__ import annotations

from pathlib import Path

import pytest

SAMPLE_PDF_PATH = Path("input/sample0.pdf")
SAMPLE1_PDF_PATH = Path("input/sample1.pdf")
SAMPLE2_PDF_PATH = Path("input/sample2.pdf")
SAMPLE3_PDF_PATH = Path("input/sample3.pdf")


_REAL_SAMPLE_SCENARIOS = [
    pytest.param(SAMPLE_PDF_PATH, None, None, "sample0_full", "full", id="sample0_full"),
    pytest.param(SAMPLE1_PDF_PATH, None, None, "sample1_full", "full", id="sample1_full"),
    pytest.param(SAMPLE2_PDF_PATH, None, None, "sample2_full", "full", id="sample2_full"),
    pytest.param(SAMPLE3_PDF_PATH, 67, 72, "sample3_label55-60", "label55-60", id="sample3_label55-60"),
]



def _find_latest_real_deepl_cache(run_id: str) -> Path | None:
    """cache/<run_id>/real_deepl_output_<タイムスタンプ>/ に一致するフォルダ
    のうち、タイムスタンプが最も新しいものを1つ返す（1件も無ければNone）。

    cache/<run_id>/ はmainCode/stage2/stage2.pyが実際に使うキャッシュ
    フォルダ（cache/<run_id>/mineru_cache/）と同じ親ディレクトリで、real_deepl_
    output_<タイムスタンプ>/はそのきょうだいとして置かれる。実DeepLを
    呼ぶ処理（test_run_pipeline_end_to_end_with_real_deepl、または
    translate_paper.pyでの本実行）は、同じrun_idで再実行しても過去の
    実行結果を上書きしないよう、実行のたびにタイムスタンプ付きの新しい
    フォルダへ書き込む。タイムスタンプは"%Y%m%d-%H%M%S"形式
    （例:"20260813-153045"）で、辞書順ソートがそのまま時系列順になるため、
    文字列としてソートするだけで最新版を特定できる。
    """
    run_dir = Path(__file__).resolve().parent.parent / "cache" / run_id
    if not run_dir.is_dir():
        return None
    candidates = sorted(run_dir.glob("real_deepl_output_*"))
    return candidates[-1] if candidates else None


# ============================================================================
# 工程(2): PDF解析（test_stage2.pyが使うMinerU実行済みfixture群）
#   process_pdfのimportは、これらのfixtureが実際に呼ばれるまで発生させない
#   よう各fixture内でローカルimportする。モジュールの先頭でimportすると、
#   process_pdfの依存先（mainCode/stage2）に問題がある場合に
#   conftest.py自体の読み込みが失敗し、それらのfixtureを一切使わない
#   他工程（例: test_stage1.py）のテストまで道連れでcollectionに失敗して
#   しまうため。
# ============================================================================



@pytest.fixture(scope="module")
def processed(tmp_path_factory):
    """sample0.pdf を一度だけMinerUで処理し、結果を全テストで共有する
    （MinerUの推論はCPU環境で数分かかるため、モジュール内で使い回す）。"""
    from mainCode.stage2.stage2 import process_pdf

    if not SAMPLE_PDF_PATH.exists():
        pytest.skip("sample0.pdf がないためスキップ")
    output_dir = tmp_path_factory.mktemp("output")
    md_paths = process_pdf(SAMPLE_PDF_PATH, output_dir, range_label="full")
    texts = {p.name: p.read_text(encoding="utf-8") for p in md_paths}
    return {"output_dir": output_dir, "md_paths": md_paths, "texts": texts}





@pytest.fixture(scope="module")
def processed_sample1(tmp_path_factory):
    """sample1.pdf（表を含む）を一度だけMinerUで処理し、結果を全テストで共有する。"""
    from mainCode.stage2.stage2 import process_pdf

    if not SAMPLE1_PDF_PATH.exists():
        pytest.skip("sample1.pdf がないためスキップ")
    output_dir = tmp_path_factory.mktemp("output_sample1")
    md_paths = process_pdf(SAMPLE1_PDF_PATH, output_dir, range_label="full")
    texts = {p.name: p.read_text(encoding="utf-8") for p in md_paths}
    return {"output_dir": output_dir, "md_paths": md_paths, "texts": texts}



@pytest.fixture(scope="module")
def processed_sample2(tmp_path_factory):
    """sample2.pdf（表を含む）を一度だけMinerUで処理し、結果を全テストで共有する。"""
    from mainCode.stage2.stage2 import process_pdf

    if not SAMPLE2_PDF_PATH.exists():
        pytest.skip("sample2.pdf がないためスキップ")
    output_dir = tmp_path_factory.mktemp("output_sample2")
    md_paths = process_pdf(SAMPLE2_PDF_PATH, output_dir, range_label="full")
    texts = {p.name: p.read_text(encoding="utf-8") for p in md_paths}
    return {"output_dir": output_dir, "md_paths": md_paths, "texts": texts}



@pytest.fixture(scope="module")
def processed_sample3(tmp_path_factory):
    """sample3.pdf（書籍PDF）を一度だけMinerUで処理し、結果を全テストで共有する。

    CLAUDE.mdの運用規定によりsample3.pdfは全体を一括処理せず、印刷ページ
    ラベル55〜60（物理ページ67〜72）の最小範囲のみを処理対象とする
    （test_unnumbered_headings_in_real_book_pdf・_REAL_SAMPLE_SCENARIOSと
    同じ範囲）。
    """
    from mainCode.stage2.stage2 import process_pdf

    if not SAMPLE3_PDF_PATH.exists():
        pytest.skip("sample3.pdf がないためスキップ")
    output_dir = tmp_path_factory.mktemp("output_sample3")
    md_paths = process_pdf(SAMPLE3_PDF_PATH, output_dir, start_page=67, end_page=72, range_label="label55-60")
    texts = {p.name: p.read_text(encoding="utf-8") for p in md_paths}
    return {"output_dir": output_dir, "md_paths": md_paths, "texts": texts}


