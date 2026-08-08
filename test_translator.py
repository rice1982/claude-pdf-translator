import re
from pathlib import Path

import fitz  # PyMuPDF
import pytest

import pdf_processor
from pdf_chapter_resolver import ChapterResolutionError, parse_chapter_spec, resolve_chapter_page_range
from pdf_models import HeadingElement, TextBlockElement
from pdf_page_label_resolver import (
    PageLabelResolutionError,
    resolve_physical_page,
    resolve_physical_page_range,
)
from pdf_processor import process_pdf, split_sentences
from pdf_structure_analyzer import analyze_structure
import translate_paper
from translate_paper import resolve_page_range, translate_and_export

# テスト用サンプルのパス
SAMPLE_PDF_PATH = Path("input/sample0.pdf")
SAMPLE1_PDF_PATH = Path("input/sample1.pdf")
SAMPLE2_PDF_PATH = Path("input/sample2.pdf")
SAMPLE3_PDF_PATH = Path("input/sample3.pdf")

SENTENCE_ID_RE = re.compile(r"^\[P(\d+)-S(\d+)-([A-Za-z0-9.]+)-S(\d+)\] (.+)$")
FIGURE_ID_RE = re.compile(r"^!\[P(\d+)-FIG(\d+)\]\(images/(fig_p\d+_\d+\.png)\) \[P(\d+)-FIG(\d+)\]$")
HEADING_ID_RE = re.compile(r"^\[P(\d+)-HEADING-([A-Za-z0-9.]+)\] (.+)$")
CAPTION_ID_RE = re.compile(r"^\[P(\d+)-FIG(\d+)-CAPTION-S(\d+)\] (.+)$")
EQUATION_ID_RE = re.compile(r"^!\[P(\d+)-EQ(\d+)\]\(images/(eq_p\d+_\d+\.png)\) \[P(\d+)-EQ(\d+)\]$")
TABLE_FIG_ID_RE = re.compile(r"^!\[P(\d+)-TABLE(\d+)\]\(images/(table_p\d+_\d+\.png)\) \[P(\d+)-TABLE(\d+)\]$")
TABLE_CAPTION_ID_RE = re.compile(r"^\[P(\d+)-TABLE(\d+)-CAPTION-S(\d+)\] (.+)$")


@pytest.fixture(scope="module")
def processed(tmp_path_factory):
    """sample.pdf を一度だけMinerUで処理し、結果を全テストで共有する
    （MinerUの推論はCPU環境で数分かかるため、モジュール内で使い回す）。"""
    if not SAMPLE_PDF_PATH.exists():
        pytest.skip("sample.pdf がないためスキップ")
    output_dir = tmp_path_factory.mktemp("output")
    md_paths = process_pdf(SAMPLE_PDF_PATH, output_dir)
    texts = {p.name: p.read_text(encoding="utf-8") for p in md_paths}
    return {"output_dir": output_dir, "md_paths": md_paths, "texts": texts}


def test_extract_and_number_sentences(processed):
    """
    input/sample0.pdf を解析し、
    - 画像が抽出されているか
    - [P1-S{n}-{section}-S{n}] 形式のナンバリングが付与されたテキストが得られるか
    をテストする
    """
    md_paths = processed["md_paths"]
    assert len(md_paths) == 2  # 2ページ分出力されているか
    assert [p.name for p in md_paths] == ["page_01_en.md", "page_02_en.md"]

    page1_text = processed["texts"]["page_01_en.md"]
    assert "[P1-TITLE]" in page1_text
    assert "[P1-S1-abstract-S1]" in page1_text


def test_front_matter_labels_are_not_counted_as_sentences(processed):
    """タイトル・著者・所属は本文の文としてナンバリングされず、専用ラベルになっているか。"""
    page1_text = processed["texts"]["page_01_en.md"]
    lines = page1_text.splitlines()
    assert lines[0] == "[P1-TITLE] EASYCONTROLEDGE: A FOUNDATION-MODEL FINE-TUNING FOR EDGE DETECTION"
    assert lines[1].startswith("[P1-AUTHORS] Hiroki Nakamura")
    assert lines[2].startswith("[P1-AFFIL]")
    # ABSTRACT見出し自体は本文の文として扱われる（章ラベルは"abstract"、文としてナンバリングされる）
    assert lines[3] == "[P1-S1-abstract-S1] ABSTRACT"


def test_chapter_headings_get_dedicated_labels(processed):
    """章・節見出しが本文の文カウントに含まれず、章番号ベースの専用ラベルになっているか。"""
    page1_text = processed["texts"]["page_01_en.md"]
    page2_text = processed["texts"]["page_02_en.md"]

    m = HEADING_ID_RE.search(next(line for line in page1_text.splitlines() if "INTRODUCTION" in line))
    assert m is not None
    assert m.group(2) == "1.introduction"
    assert m.group(3) == "1. INTRODUCTION"

    for line, expected_section in [
        ("2. METHOD: EASYCONTROLEDGE", "2.method"),
        ("2.1. Preliminaries", "2.1.preliminaries"),
        ("2.2. Lightweight Adaptation via Condition Injection", "2.2.lightweight"),
    ]:
        full_line = next(l for l in page2_text.splitlines() if l.endswith(line))
        m = HEADING_ID_RE.match(full_line)
        assert m is not None, f"unexpected heading line format: {full_line!r}"
        assert m.group(2) == expected_section


def test_cross_page_paragraph_is_kept_on_starting_page(processed):
    """段落が物理ページをまたいでいても、MinerUが1つの段落としてまとめた場合は
    開始ページ側のMarkdownに収まる（本ツールが許容している既知の挙動）。"""
    page1_text = processed["texts"]["page_01_en.md"]
    page2_text = processed["texts"]["page_02_en.md"]

    # 元は物理2ページ目にある(2)(3)の列挙が、1ページ目の段落として結合されている
    assert "(2) We add an edge-specific pixel loss" in page1_text
    assert "(3) At inference time, we adopt a Classifier-Free Guidance" in page1_text
    assert "(2) We add an edge-specific pixel loss" not in page2_text


def test_figure_caption_is_separated_from_body_text(processed):
    """図表のキャプションが本文の文カウントに含まれず、対応する図番号に紐づいた
    専用ラベルで、かつ文単位に分割・ナンバリングされているか。"""
    page2_text = processed["texts"]["page_02_en.md"]
    lines = page2_text.splitlines()

    caption_lines = [line for line in lines if line.startswith("[P2-FIG1-CAPTION-S")]
    assert len(caption_lines) == 3  # "Fig. 1: ..." / "The left side..." / "We train only..." の3文

    for expected_seq, line in enumerate(caption_lines, start=1):
        m = CAPTION_ID_RE.match(line)
        assert m is not None, f"unexpected caption line format: {line!r}"
        assert m.group(1) == "2"
        assert m.group(2) == "1"
        assert int(m.group(3)) == expected_seq  # 図ごとに1から連番

    assert caption_lines[0].endswith("Fig. 1: Overview of EasyControlEdge.")
    assert "We train only a condition-injection LoRA" in caption_lines[2]

    # キャプションの文は本文（P2-S...）の連番には含まれない
    assert not any(
        line.startswith("[P2-S") and "We train only a condition-injection LoRA" in line for line in lines
    )


def test_images_are_extracted_as_png(processed):
    """図・数式領域がPNGとして output/images/ に切り出されているかを確認する
    （sample.pdfには図1つと数式3つ(Eq.1〜Eq.3)が含まれる）。"""
    images_dir = processed["output_dir"] / "images"
    png_files = sorted(images_dir.glob("*.png"))
    assert [p.name for p in png_files] == [
        "eq_p2_1.png", "eq_p2_2.png", "eq_p2_3.png", "fig_p2_1.png",
    ]

    for png_file in png_files:
        data = png_file.read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        pix = fitz.Pixmap(str(png_file))
        assert pix.width > 0 and pix.height > 0


def test_figure_reference_is_placed_in_page2_markdown(processed):
    """画像領域に参照用ID [P2-FIG1] が割り当てられ、Markdownに埋め込まれているか。"""
    page2_text = processed["texts"]["page_02_en.md"]
    fig_lines = [line for line in page2_text.splitlines() if line.startswith("![P2-FIG")]
    assert len(fig_lines) == 1
    m = FIGURE_ID_RE.match(fig_lines[0])
    assert m is not None, f"unexpected figure line format: {fig_lines[0]!r}"
    assert m.group(3) == "fig_p2_1.png"

    # page1には図が存在しない
    assert "![P1-FIG" not in processed["texts"]["page_01_en.md"]


def test_all_three_equations_are_detected_with_latex(processed):
    """本文中に埋め込まれた数式番号(1)(2)(3)を持つ3つの数式すべてが、画像として
    分離され、かつMinerUが生成した構文的に正しいLaTeXテキストを伴っているか。

    Eq.3は数式番号が文の末尾ではなく式の直後（文中の途中）にあるパターンで、
    かつては（PyMuPDFテキスト抽出ベースの自前ヒューリスティックでは）検出できて
    いなかったが、MinerUのレイアウト・数式検出により正しく捉えられる。
    """
    page2_text = processed["texts"]["page_02_en.md"]
    lines = page2_text.splitlines()

    for n in (1, 2, 3):
        assert any(line.startswith(f"![P2-EQ{n}]") for line in lines), f"Eq.{n} の画像行が見つからない"
        m = EQUATION_ID_RE.match(next(line for line in lines if line.startswith(f"![P2-EQ{n}]")))
        assert m is not None
        assert m.group(3) == f"eq_p2_{n}.png"
        latex_line = next(line for line in lines if line.startswith(f"[P2-EQ{n}-LATEX]"))
        assert "\\tag{" + str(n) + "}" in latex_line


def test_inline_math_is_wrapped_in_dollar_signs(processed):
    """文中に埋め込まれたインライン数式が、MinerUの数式認識により$...$で
    構造化されているか。"""
    page2_text = processed["texts"]["page_02_en.md"]
    assert "$p ( y \\mid x )$" in page2_text

    # 単位行列を表す"I"（英語の代名詞"I"と同形）も、正しく数式側に含まれていること
    # （フォント名や辞書だけでは判定できず、以前は自前ヒューリスティックでは解決できなかった）
    assert "\\mathbf { I }" in page2_text


def test_header_and_footer_noise_excluded(processed):
    """arXiv IDなどのノイズが本文に含まれていないか。"""
    full_text = "\n".join(processed["texts"].values())
    assert "arXiv:" not in full_text


def test_sentence_ids_are_sequential_per_page(processed):
    """各ページの文IDが1から連番で付与されているか。"""
    for name, text in processed["texts"].items():
        page_num = int(re.search(r"page_(\d+)_en", name).group(1))
        sentence_lines = [line for line in text.splitlines() if line.startswith(f"[P{page_num}-S")]
        assert sentence_lines, f"{name} に文が1つも抽出されていない"

        ids = []
        for line in sentence_lines:
            m = SENTENCE_ID_RE.match(line)
            assert m is not None, f"unexpected sentence line format: {line!r}"
            assert int(m.group(1)) == page_num
            ids.append(int(m.group(2)))
        assert ids == list(range(1, len(ids) + 1))


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "We use a linear model. It performs well.",
            ["We use a linear model.", "It performs well."],
        ),
        (
            "See Fig. 1 for details. The results are shown in Sec. 2.",
            ["See Fig. 1 for details.", "The results are shown in Sec. 2."],
        ),
        (
            "This includes e.g. cats and dogs. Both are pets.",
            ["This includes e.g. cats and dogs.", "Both are pets."],
        ),
        (
            "",
            [],
        ),
    ],
)
def test_split_sentences_handles_abbreviations(text, expected):
    """略語（Fig., Sec., e.g. など）の直後で誤分割しないことを確認する単体テスト。"""
    assert split_sentences(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1. INTRODUCTION", ("1", "introduction")),
        ("2.1. Preliminaries", ("2.1", "preliminaries")),
        ("ABSTRACT", None),  # 章番号が無いので見出しとして扱わない
    ],
)
def test_heading_regex_and_slugify(text, expected):
    """章・節見出しの判定と章名スラッグ化の単体テスト。"""
    m = pdf_processor.HEADING_RE.match(text)
    if expected is None:
        assert m is None
    else:
        assert m is not None
        assert m.group(1) == expected[0]
        assert pdf_processor._slugify_section_name(m.group(2)) == expected[1]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Fig. 1: Overview of EasyControlEdge.", ("figure", 1)),
        ("Figure 3. Something", ("figure", 3)),
        ("Table 2: Comparison results.", ("table", 2)),
        ("This is not a caption.", None),
    ],
)
def test_parse_caption_label(text, expected):
    """キャプション文からの種別・番号抽出の単体テスト。"""
    assert pdf_processor._parse_caption_label(text) == expected


@pytest.fixture(scope="module")
def processed_sample1(tmp_path_factory):
    """sample1.pdf（表を含む）を一度だけMinerUで処理し、結果を全テストで共有する。"""
    if not SAMPLE1_PDF_PATH.exists():
        pytest.skip("sample1.pdf がないためスキップ")
    output_dir = tmp_path_factory.mktemp("output_sample1")
    md_paths = process_pdf(SAMPLE1_PDF_PATH, output_dir)
    texts = {p.name: p.read_text(encoding="utf-8") for p in md_paths}
    return {"output_dir": output_dir, "md_paths": md_paths, "texts": texts}


@pytest.fixture(scope="module")
def processed_sample2(tmp_path_factory):
    """sample2.pdf（表を含む）を一度だけMinerUで処理し、結果を全テストで共有する。"""
    if not SAMPLE2_PDF_PATH.exists():
        pytest.skip("sample2.pdf がないためスキップ")
    output_dir = tmp_path_factory.mktemp("output_sample2")
    md_paths = process_pdf(SAMPLE2_PDF_PATH, output_dir)
    texts = {p.name: p.read_text(encoding="utf-8") for p in md_paths}
    return {"output_dir": output_dir, "md_paths": md_paths, "texts": texts}


def _assert_table_caption(texts, page_name, page_num, table_num, expected_sentences):
    """指定ページのMarkdownに、表番号に紐づいたキャプションIDが文単位で
    正しく振られているかを検証する共通ヘルパー。"""
    text = texts[page_name]
    lines = text.splitlines()

    fig_line = next((l for l in lines if l.startswith(f"![P{page_num}-TABLE{table_num}]")), None)
    assert fig_line is not None, f"{page_name} に![P{page_num}-TABLE{table_num}]の画像行が見つからない"
    m = TABLE_FIG_ID_RE.match(fig_line)
    assert m is not None, f"unexpected table image line format: {fig_line!r}"
    assert m.group(3) == f"table_p{page_num}_{table_num}.png"

    caption_lines = [l for l in lines if l.startswith(f"[P{page_num}-TABLE{table_num}-CAPTION-S")]
    assert len(caption_lines) == len(expected_sentences)

    for expected_seq, (line, expected_text) in enumerate(zip(caption_lines, expected_sentences), start=1):
        m = TABLE_CAPTION_ID_RE.match(line)
        assert m is not None, f"unexpected table caption line format: {line!r}"
        assert int(m.group(1)) == page_num
        assert int(m.group(2)) == table_num
        assert int(m.group(3)) == expected_seq
        assert expected_text in line


def test_table_captions_sample1(processed_sample1):
    """sample1.pdf（4つの表を含む）で、表キャプションがFIGとは別のTABLEラベルで
    ページ・表番号ごとに文単位で正しく振られているか。"""
    texts = processed_sample1["texts"]
    _assert_table_caption(texts, "page_04_en.md", 4, 1, ["Quantitative comparisons on BSDS500"])
    _assert_table_caption(
        texts, "page_04_en.md", 4, 2,
        ["Quantitative comparison on the BIPED data-set", "column headers denote the percentage"],
    )
    _assert_table_caption(
        texts, "page_05_en.md", 5, 3,
        ["Quantitative comparison of wall detection", "column headers denote the percentage"],
    )
    _assert_table_caption(texts, "page_05_en.md", 5, 4, ["The effectiveness of"])


def test_table_captions_sample2(processed_sample2):
    """sample2.pdf（2つの表を含む）でも、同じTABLEキャプション形式が
    正しく振られているか。"""
    texts = processed_sample2["texts"]
    _assert_table_caption(
        texts, "page_06_en.md", 6, 1,
        ["Table 1.", "Correspondences between mathematical notations"],
    )
    _assert_table_caption(
        texts, "page_20_en.md", 20, 2,
        ["Table 2.", "Nomenclature of CPC-MS"],
    )


def _assert_table_pngs_extracted(processed):
    """表領域が図と同様にPNGとして images/ に切り出されているかを検証する共通処理。"""
    images_dir = processed["output_dir"] / "images"
    table_pngs = sorted(images_dir.glob("table_*.png"))
    assert table_pngs, f"{images_dir} に表のPNGが見つからない"
    for png_file in table_pngs:
        data = png_file.read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        pix = fitz.Pixmap(str(png_file))
        assert pix.width > 0 and pix.height > 0


def test_table_images_are_extracted_as_png_sample1(processed_sample1):
    _assert_table_pngs_extracted(processed_sample1)


def test_table_images_are_extracted_as_png_sample2(processed_sample2):
    _assert_table_pngs_extracted(processed_sample2)


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("1,2", [1, 2]),
        ("1-2", [1, 2]),
        ("1,3-4", [1, 3, 4]),
        ("2", [2]),
        ("2,1", [1, 2]),  # 順序に依存せず昇順ソートされる
    ],
)
def test_parse_chapter_spec_valid(spec, expected):
    assert parse_chapter_spec(spec) == expected


@pytest.mark.parametrize("spec", ["", "0", "abc", "2-1"])
def test_parse_chapter_spec_invalid(spec):
    with pytest.raises(ChapterResolutionError):
        parse_chapter_spec(spec)


def test_resolve_chapter_page_range_requires_toc():
    """目次（TOC/Outline）が無いPDF（sample0.pdf, sample1.pdfはarXiv論文で
    しおりを持たない）では、--chapterではなく--start/--endを使うよう
    案内する例外を送出する。"""
    with pytest.raises(ChapterResolutionError):
        resolve_chapter_page_range(SAMPLE_PDF_PATH, "1")


def test_resolve_chapter_page_range_with_real_toc():
    """sample2.pdf（実際に目次を持つPDF）の章1・章2が、目次から正しく
    ページ範囲に解決されるか。

    sample2.pdfの目次（最上位階層）は開始ページ [2, 4, 10, 14, 17, 19, 2, 4, 21]
    の9項目（Introduction, ..., References）。章の終了ページは「その章の
    開始ページより大きい最小の開始ページの直前」で決まるため、
    章1(Introduction, p2)は次の開始ページ4の直前(p3)まで、
    章2(...p4)は次の開始ページ10の直前(p9)までとなる。
    """
    assert resolve_chapter_page_range(SAMPLE2_PDF_PATH, "1") == (2, 3)
    assert resolve_chapter_page_range(SAMPLE2_PDF_PATH, "2") == (4, 9)
    # 複数章指定はそれらを包含する最小の連続範囲になる
    assert resolve_chapter_page_range(SAMPLE2_PDF_PATH, "1,2") == (2, 9)
    assert resolve_chapter_page_range(SAMPLE2_PDF_PATH, "1-2") == (2, 9)


def test_resolve_chapter_page_range_out_of_range():
    """sample2.pdfの目次は9章分しかないため、章番号10の指定はエラーになる。"""
    with pytest.raises(ChapterResolutionError):
        resolve_chapter_page_range(SAMPLE2_PDF_PATH, "10")


def test_resolve_chapter_page_range_skips_roman_numeral_front_matter():
    """sample3.pdf（書籍PDF）はcov/i〜xviii（ローマ数字の前付け）の後に
    本文（算用数字ページ）が始まる構造を持つ。目次の最上位階層には
    "Preface"（物理ページ5、印刷ページ"v"）や"Introduction"（物理ページ8、
    印刷ページ"ix"）といった前付け自体の見出しも並んでいるが、これらは
    印刷ページラベルがローマ数字であるため「章」として数えず、本文の
    算用数字ページが始まって最初に現れる目次項目
    "Part I: Foundations..."（物理ページ17、印刷ページ"1"）を第1章とする。
    """
    # 第1章 = Part I（物理ページ17、印刷ページ"1"）〜 Part II開始（物理ページ50）の直前
    assert resolve_chapter_page_range(SAMPLE3_PDF_PATH, "1") == (17, 49)
    # 第1・2章 = Part I 〜 Part II（物理ページ88、Part III開始=物理ページ89の直前）
    assert resolve_chapter_page_range(SAMPLE3_PDF_PATH, "1,2") == (17, 88)


def test_resolve_chapter_page_range_without_page_labels_uses_all_top_level_entries():
    """sample2.pdf（学術論文PDF）は印刷ページラベルの情報を持たない
    （fitzのget_labelが全ページで空文字列を返す）ため、前付け判定は行わず、
    従来通り目次の最上位階層すべてを章として数える（後方互換の確認）。"""
    assert resolve_chapter_page_range(SAMPLE2_PDF_PATH, "1") == (2, 3)
    assert resolve_chapter_page_range(SAMPLE2_PDF_PATH, "1,2") == (2, 9)


def test_unnumbered_headings_get_synthetic_section_ids():
    """sample3.pdfのような書籍PDFは、見出し（MinerUのtext_level 1・2）に
    論文特有の章番号（"1. Introduction"等）が付いていないことがある。
    この場合も本文に埋もれさせず、文書全体で一意な合成の章番号
    ("u1", "u2", ...)を振ったHeadingElementとして扱われるべきで、直後の
    本文の文IDにもその章IDが正しく反映される。

    MinerUのcontent_list形式を模した最小限のitemsを直接
    analyze_structureに渡すことで、実際のMinerU実行やPDFファイルを
    使わずに高速に検証する。
    """
    items = [
        {"type": "text", "text": "Symbol Emergence Systems: Overview", "text_level": 1, "page_idx": 0},
        {"type": "text", "text": "This is the first body sentence of the chapter.", "text_level": None, "page_idx": 0},
        {"type": "text", "text": "Language as a Dynamic Equilibrium System", "text_level": 2, "page_idx": 0},
        {"type": "text", "text": "This is a sentence under the subsection.", "text_level": None, "page_idx": 0},
    ]
    # page_offset=16は、物理ページ17（sample3.pdfの本文開始ページ）を
    # 模している。0（絶対1ページ目）にすると前付け判定に吸われてしまうため。
    doc = analyze_structure(items, images_base=Path("."), page_offset=16)
    page = doc.pages[0]

    headings = [e for e in page.elements if isinstance(e, HeadingElement)]
    assert len(headings) == 2
    assert headings[0].section_id == "u1.symbol"
    assert headings[1].section_id == "u2.language"

    text_blocks = [e for e in page.elements if isinstance(e, TextBlockElement)]
    assert len(text_blocks) == 2
    assert text_blocks[0].sentence_ids[0].endswith("-u1.symbol-S1")
    assert text_blocks[1].sentence_ids[0].endswith("-u2.language-S1")


def test_unnumbered_headings_in_real_book_pdf(tmp_path):
    """test_unnumbered_headings_get_synthetic_section_idsが自作データで
    検証している合成章ID（"u1","u2",...）のロジックを、実際のsample3.pdf
    （印刷ページラベル55〜60、物理ページ67〜72）を処理して裏取りする。

    このページ範囲には、番号の付いていない見出し（例: "Fundamentals of
    Probability Theory"）と、番号付きの見出し（例: "1. Sampling-based
    methods:"）の両方が実際に含まれており、前者が合成ID・後者が通常の
    章番号として正しく処理されることを、process_pdfの実出力で確認する。
    CLAUDE.mdの運用規定に従い、sample3.pdfは印刷ページラベルで絞り込んだ
    最小限の範囲（6ページ分）のみを処理対象とする。
    """
    if not SAMPLE3_PDF_PATH.exists():
        pytest.skip("sample3.pdf がないためスキップ")

    start_page, end_page = resolve_physical_page_range(SAMPLE3_PDF_PATH, "55", "60")
    assert (start_page, end_page) == (67, 72)

    output_dir = tmp_path / "sample3_labels_recheck"
    md_paths = process_pdf(SAMPLE3_PDF_PATH, output_dir, start_page=start_page, end_page=end_page)
    texts = {p.name: p.read_text(encoding="utf-8") for p in md_paths}

    # MinerUはOCR結果の単語間にノーブレークスペース(\xa0)を挟むことがある
    # ため、この後付け見出しID検証の本質とは無関係な差異として正規化する。
    heading_lines = sorted(
        line.replace("\xa0", " ")
        for text in texts.values()
        for line in text.splitlines()
        if "-HEADING-" in line
    )

    expected_unnumbered = [
        '[P67-HEADING-u1.probabilistic] Probabilistic Generative Models: Foundational Theory for Cognitive Modeling Based on Bayesian Inference',
        '[P67-HEADING-u2.probabilistic] Probabilistic Generative Models and Bayesian Inference in Cognitive Modeling',
        '[P68-HEADING-u3.fundamentals] Fundamentals of Probability Theory',
        '[P69-HEADING-u4.bayesian] Bayesian Inference (Bayesian Estimation)',
        '[P70-HEADING-u5.graphical] Graphical Model Representation for PGMS',
        '[P71-HEADING-u6.cross] Cross-Modal Inference and Deep PGMs',
    ]
    expected_numbered = [
        '[P69-HEADING-1.sampling] 1. Sampling-based methods:',
        '[P70-HEADING-2.approximate] 2. Approximate inference methods:',
    ]
    assert heading_lines == sorted(expected_unnumbered + expected_numbered)


def test_translate_and_export_with_mocked_deepl(processed, monkeypatch):
    """CLAUDE.mdの規定により、pytestでの翻訳テストはDeepLの有料APIキーを
    消費しないよう、DeepL呼び出し自体をモックする
    （``translate_paper.translate_with_deepl`` を差し替える）。

    既にMinerU解析済みの``processed``フィクスチャのoutput_dir
    （sample0.pdfを2ページ分処理した一時ディレクトリ）に対し、Step2〜4
    （タグ解析・翻訳・PDF生成）を実行し、
    - 3種類のPDFが空でなく生成されること
    - paper_ja.pdfに実際に日本語訳が書き込まれていること（原文の
      英語のままではなく、恒等関数でもないこと）
    を確認する。MinerU自体は再実行しない（fixtureの結果を再利用する）ため、
    実行時間は翻訳・PDF生成の分のみで済む。
    """

    def _fake_translate_with_deepl(units, api_key, document_context, log=print):
        for unit in units:
            if unit.translatable:
                unit.ja_text = "これはテスト用の日本語訳です。"

    monkeypatch.setattr(translate_paper, "translate_with_deepl", _fake_translate_with_deepl)

    output_dir = processed["output_dir"]
    pdf_paths = translate_and_export(output_dir)

    assert len(pdf_paths) == 3
    for path in pdf_paths:
        assert path.exists(), f"{path} が生成されていない"
        assert path.stat().st_size > 0, f"{path} が空ファイルになっている"

    ja_pdf_path = next(p for p in pdf_paths if p.name == "paper_ja.pdf")
    with fitz.open(ja_pdf_path) as doc:
        ja_text = "".join(page.get_text() for page in doc)
    has_japanese = any("぀" <= ch <= "ヿ" or "一" <= ch <= "鿿" for ch in ja_text)
    assert has_japanese, "paper_ja.pdfに日本語文字が見つからない（翻訳が行われていない可能性がある）"


def test_translate_and_export_translates_all_translatable_units(tmp_path, monkeypatch):
    """test_translate_and_export_with_mocked_deeplは「日本語が1文字でも
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

    def _fake_translate_with_deepl(units, api_key, document_context, log=print):
        for unit in units:
            if unit.translatable:
                unit.ja_text = mock_ja

    monkeypatch.setattr(translate_paper, "translate_with_deepl", _fake_translate_with_deepl)

    pdf_paths = translate_and_export(output_dir)
    ja_pdf_path = next(p for p in pdf_paths if p.name == "paper_ja.pdf")
    with fitz.open(ja_pdf_path) as doc:
        ja_text = "".join(page.get_text() for page in doc)

    translated_count = ja_text.count(mock_ja)
    assert translated_count == 5, (
        f"翻訳対象の5文（title/heading/body_sentence x3）すべてに訳文が"
        f"反映されているはずが、{translated_count}回しか出現しなかった"
        f"（一部の文が未翻訳のまま残っている可能性がある）"
    )


def test_translate_and_export_excludes_references_section(tmp_path, monkeypatch):
    """md_tag_parser.exclude_references_sectionの動作を、Gのスイート内で
    直接検証する。sample0.pdf（2ページの論文抜粋）には参考文献セクション
    が含まれないため、既存のtest_translate_and_export_with_mocked_deepl
    ではこの分岐が実質検証されていなかった。参考文献セクション（見出し・
    本文とも）は翻訳対象から除外され、原文の英語のまま出力されるはず
    である。process_pdf/MinerUを一切経由しない。
    """
    output_dir = tmp_path / "g_references_input"
    output_dir.mkdir()
    mock_ja = "これはテスト用の日本語訳です。"
    reference_text = "Smith J. A landmark paper on foo. Journal of Bar, 2020."
    (output_dir / "page_01_en.md").write_text(
        "[P1-TITLE] A Study of Something Interesting\n"
        "[P1-HEADING-1.introduction] 1. INTRODUCTION\n"
        "[P1-S1-1.introduction-S1] This sentence should be translated.\n"
        "[P1-HEADING-2.references] REFERENCES\n"
        f"[P1-S2-2.references-S1] {reference_text}\n",
        encoding="utf-8",
    )

    def _fake_translate_with_deepl(units, api_key, document_context, log=print):
        for unit in units:
            if unit.translatable:
                unit.ja_text = mock_ja

    monkeypatch.setattr(translate_paper, "translate_with_deepl", _fake_translate_with_deepl)

    pdf_paths = translate_and_export(output_dir)
    ja_pdf_path = next(p for p in pdf_paths if p.name == "paper_ja.pdf")
    with fitz.open(ja_pdf_path) as doc:
        ja_text = "".join(page.get_text() for page in doc)

    assert mock_ja in ja_text, "本文（翻訳対象）に訳文が反映されていない"
    assert reference_text in ja_text, (
        "参考文献セクションが翻訳対象から除外されておらず、"
        "原文の英語がそのまま残っていない"
    )
    assert "REFERENCES" in ja_text, (
        "参考文献セクションの見出し自体も翻訳対象から除外されているはず"
    )


@pytest.mark.parametrize(
    "label,expected_physical_page",
    [
        ("cov", 1),   # 短縮形。実際のラベルは"Cover"（フルワード）
        ("COV", 1),   # 大文字・小文字を区別しない
        ("Cover", 1),
        ("i", 2),
        ("xviii", 16),
        ("55", 67),   # 本文開始後の算用数字ページ（ラベル55-60範囲の開始）
        ("60", 72),   # 同、ラベル55-60範囲の終了
    ],
)
def test_resolve_physical_page_for_sample3(label, expected_physical_page):
    """sample3.pdf（cov + i〜xviii前付け + 算用数字本文）で、印刷ページ
    ラベルから正しい物理ページ番号（1始まり）が求まるか。"""
    assert resolve_physical_page(SAMPLE3_PDF_PATH, label) == expected_physical_page


def test_resolve_physical_page_unknown_label_raises():
    with pytest.raises(PageLabelResolutionError):
        resolve_physical_page(SAMPLE3_PDF_PATH, "zzz")


def test_resolve_physical_page_without_page_labels_raises():
    """sample2.pdf（学術論文PDF）は印刷ページラベルの情報を持たないため、
    どのラベルを指定してもエラーになる（--start-labelではなく
    --start/--endを使うよう案内される）。"""
    with pytest.raises(PageLabelResolutionError):
        resolve_physical_page(SAMPLE2_PDF_PATH, "1")


def test_resolve_physical_page_range_matches_previously_verified_pages():
    """sample3.pdfの印刷ページ"36"〜"41"は物理ページ49〜53に対応する
    （印刷ページ"38"に対応するページが存在せず"37"の次が"39"になる
    ギャップがあるため、"36"〜"41"の6段のラベルは物理ページでは
    49〜53の5ページ分に収まる）。"""
    assert resolve_physical_page_range(SAMPLE3_PDF_PATH, "36", "41") == (49, 53)


def test_resolve_page_range_conflicting_start_and_start_label_raises():
    """--startと--start-labelを同時に指定するのは矛盾した指定であり、
    どちらを優先すべきか一意に決まらないためエラーとする。"""
    with pytest.raises(ValueError):
        resolve_page_range(SAMPLE3_PDF_PATH, None, 67, None, "55", None)


def test_resolve_page_range_prefers_page_label_over_chapter():
    """--start-label/--end-labelが指定された場合、--chapterより優先される
    （--start/--endが優先されるのと同じ優先順位）。"""
    start_page, end_page = resolve_page_range(SAMPLE3_PDF_PATH, "1", None, None, "55", "60")
    assert (start_page, end_page) == (67, 72)


def test_require_pdf_exists_raises_for_missing_file():
    """存在しない入力PDFパスを指定した場合、fitzの生の例外
    （FileNotFoundError）ではなく、分かりやすいSystemExitで終了すること
    を確認する。"""
    with pytest.raises(SystemExit, match="入力PDFファイルが見つかりません"):
        translate_paper._require_pdf_exists("input/does_not_exist.pdf")


def test_require_pdf_exists_passes_for_existing_file():
    """実在するPDFパスを指定した場合は何も送出しないこと（正常系）を
    確認する。"""
    translate_paper._require_pdf_exists(SAMPLE_PDF_PATH)
