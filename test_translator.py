import os
import re
from pathlib import Path

import fitz  # PyMuPDF
import pytest
from dotenv import load_dotenv

import pdf_processor
from deepl_translator import translate_with_deepl
from math_protection import (
    check_unprotected_math_survival,
    find_untranslated_fragment_candidates,
    find_unprotected_math_like_tokens,
    normalize as normalize_math,
    protect,
    protect_confirmed_single_letter_leaks,
    report_untranslated_fragment_candidates,
    restore,
)
from md_tag_parser import (
    build_document_context,
    exclude_references_section,
    parse_output_dir,
    write_translated_pages,
)
from pdf_chapter_resolver import ChapterResolutionError, parse_chapter_spec, resolve_chapter_page_range
from pdf_models import HeadingElement, TextBlockElement
from pdf_page_label_resolver import (
    PageLabelResolutionError,
    resolve_physical_page,
    resolve_physical_page_range,
)
from pdf_processor import process_pdf, split_sentences
from pdf_structure_analyzer import analyze_structure
from pdf_text_utils import wrap_bare_greek_letters
import translate_paper
from translate_paper import resolve_page_range, translate_and_export
from translation_models import DocUnit

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
    構造化されているか。

    ここでassertしているのは2箇所の固定例のみの回帰用spot checkである
    （DeepL APIキーが無くても実行できる、無課金・高速なチェック）。
    page全体を対象にした網羅的なチェック（ギリシャ文字等、保護されて
    いない数式の全数検出）は、DEEPL_API_KEY設定時のみ実行される
    スイートKのtest_page_ja_md_and_candidate_detection_against_real_
    deeplが担う（2026-08-10、詳細はtestExplain.txtのスイートB/K参照）。
    """
    page2_text = processed["texts"]["page_02_en.md"]
    assert "$p ( y \\mid x )$" in page2_text

    # 単位行列を表す"I"（英語の代名詞"I"と同形）も、正しく数式側に含まれていること
    # （フォント名や辞書だけでは判定できず、以前は自前ヒューリスティックでは解決できなかった）
    assert "\\mathbf { I }" in page2_text


def test_math_protection_round_trips_all_real_inline_math(processed):
    """page_02_en.md（sample0.pdf）に実際に含まれる全てのインライン数式・
    ディスプレイ数式スパンが、math_protection.protect/restoreのラウンド
    トリップで（\\textless/\\textgreaterの正規化を除き）壊れず復元されるか。

    test_inline_math_is_wrapped_in_dollar_signsが"$p ( y \\mid x )$"と
    "\\mathbf { I }"の2箇所だけを直接assertしているのに対し、本テストは
    実データに含まれる数式スパンを全数（自作データではなく）対象にする
    ことで、math_protection.pyの直接ユニットテストが無かったギャップ
    （旧・testExplain.txtスイートA参照）を、実データの範囲で補う。

    2026-08-10、pdf_structure_analyzer._handle_text_item等が
    wrap_bare_greek_letters（pdf_text_utils.py）を経由するようになり、
    "scale γ"のように$...$で保護されていなかったギリシャ文字が構造解析
    段階で自動的に$γ$として保護されるようになった。これによりpage_02の
    数式スパン数が24個から25個に増えている（詳細は下記assertのコメント
    参照）。"""
    text = processed["texts"]["page_02_en.md"]

    protected, spans = protect(text)
    # page_02_en.mdに実際に含まれる数式スパン数（インライン22+ディスプレイ3。
    # 2026-08-10、wrap_bare_greek_lettersによる"$γ$"の自動保護が加わり
    # 21から22に増えた）。MinerUの数式検出結果が変われば変化しうる値の
    # ため、変化した場合は数式検出の挙動が変わっていないか確認すること。
    assert len(spans) == 25
    # プレースホルダ置換後のテキストに数式デリミタ$が一切残っていないこと
    # （＝検出された数式スパンがDeepLへの翻訳リクエストから完全に
    # 除外されることの確認）
    assert "$" not in protected

    restored = restore(protected, spans)
    # \textless/\textgreaterの正規化（KaTeXが解釈できないMinerU由来の
    # エスケープの置換）以外は、元テキストと完全に一致すること
    expected = text.replace("\\textless", "<").replace("\\textgreater", ">")
    assert restored == expected


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


@pytest.mark.parametrize(
    "text,expected",
    [
        # 地の文に単独で出現するギリシャ文字は、実在の英単語と衝突しない
        # ため自動的に$...$で保護してよい（2026-08-10追加）。TeXコマンド
        # 形式（例:"γ"→"\gamma"）に変換した上で保護する（2026-08-10、
        # 他の数式スパン（MinerU由来）との表記の一貫性のためユーザー
        # 指示により変更。KaTeX上は生の文字でも等価に描画されることを
        # 確認済みだが、明示的にコマンド形式へ統一する）。
        ("we control edge density via scale γ (Sec. 2.4).", "we control edge density via scale $\\gamma$ (Sec. 2.4)."),
        # 複数のギリシャ文字が連続する場合は1つの数式スパンとしてまとめ、
        # それぞれ個別のコマンドに変換して連結する。
        ("with schedule αβ decay", "with schedule $\\alpha\\beta$ decay"),
        # 既に$...$で保護済みのギリシャ文字（例: LaTeXの\gammaではなく
        # 直接埋め込まれた文字）は二重にラップ・変換しない。
        ("already protected $γ$ here", "already protected $γ$ here"),
        # ギリシャ文字が無ければ何も変化しない。
        ("no greek letters here at all.", "no greek letters here at all."),
        # ラテン文字と同形の大文字ギリシャ文字（例:"Α"=Alpha）はKaTeXに
        # 専用コマンドが無いため、変換せず元の文字のまま$...$で囲む。
        ("value Α here", "value $Α$ here"),
    ],
)
def test_wrap_bare_greek_letters(text, expected):
    """未保護のギリシャ文字が$...$で自動的に囲まれるかの単体テスト。"""
    assert wrap_bare_greek_letters(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("We integrate from t = 1 to t = 0.", ["t = 1", "t = 0"]),
        ("$t = 1$ is protected by dollar signs.", []),
        ("No math-like pattern here at all.", []),
        ("K discrete steps (no equals sign, not detected).", []),
    ],
)
def test_find_unprotected_math_like_tokens(text, expected):
    """$...$で保護されていない"文字=英数字"パターンの検出テスト。"""
    assert find_unprotected_math_like_tokens(text) == expected


def test_check_unprotected_math_survival_warns_when_token_missing():
    """未保護の数式らしき文字列が翻訳後に消えている場合、警告が出るか。"""
    units = [
        DocUnit(
            tag="P1-S1-intro-S1",
            kind="body_sentence",
            page=1,
            en_text="We integrate from t = 1 to t = 0.",
            ja_text="ノイズから清潔な状態まで積分する。",  # t = 1 / t = 0 が消えている
            translatable=True,
        )
    ]
    warnings = check_unprotected_math_survival(units, log=lambda _msg: None)
    assert len(warnings) == 2
    assert "t = 1" in warnings[0]
    assert "t = 0" in warnings[1]


def test_check_unprotected_math_survival_silent_when_token_preserved():
    """未保護の数式らしき文字列が翻訳後も残っていれば、警告が出ないか。"""
    units = [
        DocUnit(
            tag="P1-S1-intro-S1",
            kind="body_sentence",
            page=1,
            en_text="We integrate from t = 1 to t = 0.",
            ja_text="t = 1からt = 0まで積分する。",  # 両方とも保持されている
            translatable=True,
        )
    ]
    assert check_unprotected_math_survival(units, log=lambda _msg: None) == []


def test_check_unprotected_math_survival_skips_non_translatable_units():
    """翻訳対象外のunitは、en_text/ja_textが食い違っていても警告しないか。"""
    units = [
        DocUnit(
            tag="P1-EQ1-LATEX",
            kind="equation_latex",
            page=1,
            en_text="t = 1",
            ja_text="",  # 翻訳対象外なので不一致でも無視されるべき
            translatable=False,
        )
    ]
    assert check_unprotected_math_survival(units, log=lambda _msg: None) == []


@pytest.mark.parametrize(
    "ja_text,expected",
    [
        ("潜在変数zをエッジマップにデコードし、t=1からt=0まで積分する。", ["z", "t=1", "t=0"]),
        # スペースを挟む"t = 1"も、途中で分断されず1つの断片としてまとまるか
        # （実際のDeepL翻訳結果で確認された表記。2026-08-09追加）
        ("誘導された常微分方程式(ODE)を t = 1 から t = 0 まで積分する。", ["(ODE)", "t = 1", "t = 0"]),
        ("K個の離散ステップを用いて", ["K"]),
        ("条件付き生成 $p ( y \\mid x )$ として定式化し", []),  # 保護済み数式は候補にならない
        ("これは完全に日本語だけの文です。", []),
        # ギリシャ文字も、半角英数字と同様にDeepLが翻訳せず残す数式記号の
        # ため検出対象に含む（実際のDeepL翻訳結果で確認された表記。
        # 2026-08-10追加）。
        ("推論時にはスケールγを用いた分類器フリーガイダンスによって", ["γ"]),
    ],
)
def test_find_untranslated_fragment_candidates(ja_text, expected):
    """翻訳後も半角のまま残る、未検出の数式らしき候補の検出テスト。"""
    assert find_untranslated_fragment_candidates(ja_text) == expected


def test_report_untranslated_fragment_candidates_logs_info_not_warning():
    """候補が見つかった場合、[警告]ではなく[情報]としてログ出力されるか。"""
    units = [
        DocUnit(
            tag="P1-S1-intro-S1",
            kind="body_sentence",
            page=1,
            en_text="We decode a latent z back to an edge map.",
            ja_text="潜在変数zをエッジマップにデコードする。",
            translatable=True,
        )
    ]
    messages = []
    result = report_untranslated_fragment_candidates(units, log=messages.append)
    assert result == messages
    assert len(result) == 1
    assert "[情報]" in result[0]
    assert "z" in result[0]


def test_report_untranslated_fragment_candidates_skips_non_translatable_units():
    """翻訳対象外のunitは、半角断片が残っていても候補にならないか。"""
    units = [
        DocUnit(
            tag="P1-EQ1-LATEX",
            kind="equation_latex",
            page=1,
            en_text="z",
            ja_text="z",  # 翻訳対象外
            translatable=False,
        )
    ]
    assert report_untranslated_fragment_candidates(units, log=lambda _msg: None) == []


def test_protect_confirmed_single_letter_leaks_wraps_matching_occurrences():
    """未保護の単体アルファベット（例:"z"）が、en_text・ja_text双方の
    同じ箇所で$...$に置き換わるか（2026-08-10追加）。DeepLへの再翻訳は
    行わないため、テスト内でtranslate_with_deeplは一切呼ばない。"""
    units = [
        DocUnit(
            tag="P1-S1-body-S1",
            kind="body_sentence",
            page=1,
            en_text="We decode a latent z back to an edge map.",
            ja_text="潜在変数zをエッジマップにデコードする。",
            translatable=True,
        )
    ]
    messages = protect_confirmed_single_letter_leaks(units, log=lambda _msg: None)
    assert len(messages) == 1
    assert "'z'" in messages[0]
    assert units[0].en_text == "We decode a latent $z$ back to an edge map."
    assert units[0].ja_text == "潜在変数$z$をエッジマップにデコードする。"


def test_protect_confirmed_single_letter_leaks_does_not_touch_existing_math_spans():
    """既に$...$で保護済みの数式スパンの内部にある同名の文字（例:
    "$p ( y \\mid x )$"内の"y"/"x"）を、二重に$で囲んで壊さないことを
    確認する回帰テスト（2026-08-10、実データで見つかった不具合の修正）。
    保護済みスパンの外にある同じ文字（standaloneの"x"）は通常通り保護
    されること、既存スパン自体は一切変化しないことの両方を検証する。"""
    units = [
        DocUnit(
            tag="P2-S6-2.1.preliminaries-S1",
            kind="body_sentence",
            page=2,
            en_text="Given input image x, we formulate it as $p ( y \\mid x )$.",
            ja_text="入力画像xが与えられたとき、$p ( y \\mid x )$として定式化する。",
            translatable=True,
        )
    ]
    messages = protect_confirmed_single_letter_leaks(units, log=lambda _msg: None)
    assert len(messages) == 1
    assert "'x'" in messages[0]
    assert units[0].en_text == "Given input image $x$, we formulate it as $p ( y \\mid x )$."
    assert units[0].ja_text == "入力画像$x$が与えられたとき、$p ( y \\mid x )$として定式化する。"


def test_protect_confirmed_single_letter_leaks_skips_multi_char_tokens():
    """"DiT"のような複数文字のトークンは、単体アルファベットではないため
    保護されず、en_text/ja_textが変化しないことを確認する。"""
    units = [
        DocUnit(
            tag="P2-FIG1-CAPTION-S3",
            kind="caption_sentence",
            page=2,
            en_text="a frozen DiT-based foundation model",
            ja_text="凍結されたDiTベースのファウンデーションモデル",
            translatable=True,
        )
    ]
    original_en, original_ja = units[0].en_text, units[0].ja_text
    messages = protect_confirmed_single_letter_leaks(units, log=lambda _msg: None)
    assert messages == []
    assert units[0].en_text == original_en
    assert units[0].ja_text == original_ja


def test_protect_confirmed_single_letter_leaks_skips_non_translatable_units():
    """翻訳対象外のunitは、単体アルファベットが残っていても保護しないか。"""
    units = [
        DocUnit(
            tag="P1-EQ1-LATEX",
            kind="equation_latex",
            page=1,
            en_text="z",
            ja_text="z",
            translatable=False,
        )
    ]
    original_en = units[0].en_text
    assert protect_confirmed_single_letter_leaks(units, log=lambda _msg: None) == []
    assert units[0].en_text == original_en


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
    _assert_table_caption(
        texts, "page_04_en.md", 4, 1,
        ["Quantitative comparisons on BSDS500", "Our own implementation of GED, as no official code was released."],
    )
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
    正しく振られているか。

    両キャプションとも"Table N: ..."とコロン区切りのため、ピリオド区切りの
    略語リストによる分割は起こらず、1文（S1）にまとまる。"""
    texts = processed_sample2["texts"]
    _assert_table_caption(
        texts, "page_06_en.md", 6, 1,
        ["Table 1: Correspondences between mathematical notations"],
    )
    _assert_table_caption(
        texts, "page_23_en.md", 23, 2,
        ["Table 2: Nomenclature of CPC-MS"],
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


def _build_synthetic_toc_pdf(path: Path, page_count: int, toc: list[list]) -> None:
    """resolve_chapter_page_range系テスト専用のヘルパー。本文の無い白紙
    ページと目次（TOC/Outline）だけを持つ最小限のPDFをfitzで直接組み立てる。

    章番号→ページ範囲変換のアルゴリズム自体（最上位階層のみを章とする、
    次章開始の直前を終了ページとする等）は、特定の論文の実際の目次構成に
    依存しない一般的なロジックであるべきなので、実PDF（ダウンロードした
    arXiv論文等）ではなく決定的な合成データで検証する。実PDFの内容は
    論文の版が変わる・著者が改訂する等で意図せず変化しうるため、これに
    アルゴリズムのテストを依存させると、コードは正しいままテストだけが
    壊れる（本プロジェクトで実際に発生した問題）。実際の学術論文・書籍PDF
    に対する目次読み取りの実データ確認は、
    test_resolve_chapter_page_range_skips_roman_numeral_front_matter
    （sample3.pdf）が担当する。"""
    doc = fitz.open()
    for _ in range(page_count):
        doc.new_page()
    doc.set_toc(toc)
    doc.save(str(path))
    doc.close()


def test_resolve_chapter_page_range_requires_toc(tmp_path):
    """目次（TOC/Outline）が無いPDFでは、--chapterではなく--start/--endを
    使うよう案内する例外を送出する。"""
    pdf_path = tmp_path / "no_toc.pdf"
    _build_synthetic_toc_pdf(pdf_path, page_count=3, toc=[])
    with pytest.raises(ChapterResolutionError):
        resolve_chapter_page_range(pdf_path, "1")


def test_resolve_chapter_page_range_computes_ranges_from_toc(tmp_path):
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


def test_resolve_chapter_page_range_out_of_range(tmp_path):
    """目次に存在しない章番号を指定した場合はエラーになる（合成TOCは
    章3件分のみなので、章番号10の指定はエラーになる）。"""
    toc = [[1, "Chapter 1", 1], [1, "Chapter 2", 2], [1, "Chapter 3", 3]]
    pdf_path = tmp_path / "toc.pdf"
    _build_synthetic_toc_pdf(pdf_path, page_count=3, toc=toc)
    with pytest.raises(ChapterResolutionError):
        resolve_chapter_page_range(pdf_path, "10")


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


def test_resolve_chapter_page_range_without_page_labels_uses_all_top_level_entries(tmp_path):
    """印刷ページラベルの情報を持たないPDF（学術論文PDF等。fitzのget_label
    が全ページで空文字列を返す）では、前付け判定は行わず、従来通り目次の
    最上位階層すべてを章として数える（後方互換の確認）。

    合成PDFはfitz.Document.set_page_labelsを一切呼ばないため、実PDFの
    sample2.pdfと同じく「印刷ページラベル情報が無い」状態を再現する。"""
    toc = [[1, "Chapter 1", 1], [1, "Chapter 2", 3], [1, "Chapter 3", 5]]
    pdf_path = tmp_path / "no_page_labels.pdf"
    _build_synthetic_toc_pdf(pdf_path, page_count=6, toc=toc)

    assert resolve_chapter_page_range(pdf_path, "1") == (1, 2)
    assert resolve_chapter_page_range(pdf_path, "1,2") == (1, 4)


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

    # 2026-08-09追加: translate_and_export内でwrite_translated_pagesが
    # 呼ばれ、page_XX_en.mdと対になるpage_XX_ja.mdが生成されているか。
    ja_md_paths = sorted(output_dir.glob("page_*_ja.md"))
    assert [p.name for p in ja_md_paths] == ["page_01_ja.md", "page_02_ja.md"]
    for path in ja_md_paths:
        assert "これはテスト用の日本語訳です。" in path.read_text(encoding="utf-8")


def test_write_translated_pages_preserves_tag_format(tmp_path):
    """write_translated_pagesが、page_XX_en.mdと同じタグ形式でja_textを
    書き出せているかを、自作unitsで直接検証する（2026-08-09追加）。
    process_pdf/MinerU/DeepLを一切経由しないため高速。
    """
    units = [
        DocUnit(tag="P1-TITLE", kind="title", page=1, en_text="A Title", ja_text="タイトル"),
        DocUnit(
            tag="P1-FIG1",
            kind="figure_image",
            page=1,
            image_rel_path="images/fig_p1_1.png",
        ),
        DocUnit(
            tag="P1-FIG1-CAPTION-S1",
            kind="caption_sentence",
            page=1,
            en_text="Fig. 1: Example.",
            ja_text="図1: 例。",
        ),
        DocUnit(tag="P2-S1-body-S1", kind="body_sentence", page=2, en_text="Body.", ja_text="本文。"),
    ]

    output_dir = tmp_path / "write_translated_pages_output"
    output_dir.mkdir()
    written = write_translated_pages(units, output_dir)

    assert [p.name for p in written] == ["page_01_ja.md", "page_02_ja.md"]

    page1_text = (output_dir / "page_01_ja.md").read_text(encoding="utf-8")
    assert "[P1-TITLE] タイトル" in page1_text
    assert "![P1-FIG1](images/fig_p1_1.png) [P1-FIG1]" in page1_text
    assert "[P1-FIG1-CAPTION-S1] 図1: 例。" in page1_text

    page2_text = (output_dir / "page_02_ja.md").read_text(encoding="utf-8")
    assert "[P2-S1-body-S1] 本文。" in page2_text


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


# Suite K（test_page_ja_md_and_candidate_detection_against_real_deepl）の
# 未知候補チェックで使う許可リスト（2026-08-10追加）。sample0.pdfの
# page_01/page_02を実際にDeepLで翻訳し、find_untranslated_fragment_
# candidatesが検出した全候補を人間が目視確認した結果を記録したもの。
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
    # （言い回しの揺れ。2026-08-10、再実行時に新たに確認）。
    "DiT",  # 固有名詞（モデル名）。P2-FIG1-CAPTION-S3。
    "LoRA",  # 固有名詞（手法名）。P2-FIG1-CAPTION-S3。
    "FLUX",  # 固有名詞（モデル名）。P2-S13。
    "(ODE)",  # 略語（Ordinary Differential Equation）。P2-S7。
    "(i)", "(ii)", "(iii)",  # 列挙記号。P2-S4。
}
KNOWN_LEAKED_MATH_FRAGMENTS = {
    "t = 1", "t = 0",  # P2-S7。積分区間の端点（"="を含む複数文字の
    # トークンのため、単体アルファベット限定のprotect_confirmed_
    # single_letter_leaksでは自動保護されず、引き続き許可リストで容認）。
}
# 注1: "γ"（P2-FIG1-CAPTION-S3の"scale γ"）は当初ここに含めていたが、
# 2026-08-10にpdf_text_utils.wrap_bare_greek_lettersを追加し、構造解析
# 段階でギリシャ文字を自動的に$...$保護するようにしたため、find_
# untranslated_fragment_candidatesではもう検出されなくなった（許可リスト
# チェックの対象外＝リストに残す意味が無いため削除）。
#
# 注2: "x"/"y"/"z"/"K"（単体の半角アルファベット）も同様に、2026-08-10に
# math_protection.protect_confirmed_single_letter_leaksを追加し、翻訳後に
# 半角のまま生き残った単体アルファベットをen_text/ja_text双方で直接
# $...$保護するようにしたため、検出されなくなった（許可リストから削除）。
# ギリシャ文字・単体アルファベットはいずれも「実在の英単語との衝突が
# 起きにくい」という共通の性質を利用した自動保護だが、"DiT"/"NMS"の
# ような複数文字のトークンは実在の固有名詞・略語と区別できないため、
# 引き続き検出のみ・許可リストでの容認とする（詳細はpdf_text_utils.pyの
# wrap_bare_greek_letters、math_protection.pyのprotect_confirmed_
# single_letter_leaksのdocstring参照）。


def test_page_ja_md_and_candidate_detection_against_real_deepl(processed, tmp_path):
    """CLAUDE.mdの例外規定（2026-08-09、sample0.pdf限定でDeepL実課金
    呼び出しを許可）に従い、他のテストと異なりDeepLをモック化せず実際に
    呼び出す。write_translated_pagesによるpage_XX_ja.md生成に加え、
    2026-08-10より、page_01/page_02全体を対象にした未保護数式のフル
    チェックも行う。翻訳直後にprotect_confirmed_single_letter_leaksで
    単体アルファベットの数式変数（"x"/"y"/"z"/"K"）を自動保護する処理も
    本番と同じ位置で実行するため、これらは以降のフルチェックでは候補
    として検出されない（詳細は下記KNOWN_LEAKED_MATH_FRAGMENTSの注2）。

    test_inline_math_is_wrapped_in_dollar_signs（スイートB）が"$p ( y
    \\mid x )$"等2箇所の固定例だけをspot checkしているのに対し、本テストは
    実際のDeepL翻訳結果からfind_untranslated_fragment_candidatesで
    全候補を洗い出し、下記KNOWN_FALSE_POSITIVE_FRAGMENTS /
    KNOWN_LEAKED_MATH_FRAGMENTS という人間確認済みの許可リストに無い
    未知の候補が出現していないかを確認する、page全体を対象にした回帰
    テストである。

    ただしDeepLの翻訳結果は完全に決定的ではなく、言い回しの変化により
    許可リストに無い新しい候補（真の数式漏れとは限らず、誤検知の場合も
    ある）が出現しテストが失敗することがある。その場合は本テストの直前
    に定義された2つの許可リストのdocstringに従い、目視確認の上でどちらか
    に追加すること（“取りあえず許可リストに足してテストを通す”という
    運用は本テストの目的を損なうため避けること）。PDF生成（Playwright）
    は本テストの目的に不要なため実行しない。
    """
    load_dotenv()
    if os.environ.get("DEEPL_API_KEY") is None:
        pytest.skip("DEEPL_API_KEY が未設定のためスキップ")

    output_dir = processed["output_dir"]
    units = parse_output_dir(output_dir)
    for unit in units:
        if unit.kind in {"title", "heading", "body_sentence", "caption_sentence"}:
            unit.en_text = normalize_math(unit.en_text)
    exclude_references_section(units)
    document_context = build_document_context(units)

    translate_with_deepl(units, os.environ["DEEPL_API_KEY"], document_context, log=lambda _msg: None)

    # 本番のtranslate_and_exportと同じ位置（翻訳直後・write_translated_
    # pagesの前）で、単体アルファベットの数式変数を自動保護する
    # （2026-08-10追加。再翻訳はしない後処理のため、ここで実行しても
    # 追加のDeepL課金は発生しない）。
    protect_confirmed_single_letter_leaks(units, log=lambda _msg: None)

    ja_output_dir = tmp_path / "real_deepl_ja_output"
    ja_output_dir.mkdir()
    written = write_translated_pages(units, ja_output_dir)

    assert [p.name for p in written] == ["page_01_ja.md", "page_02_ja.md"]
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert text.strip(), f"{path} が空になっている"
        has_japanese = any("぀" <= ch <= "ヿ" or "一" <= ch <= "鿿" for ch in text)
        assert has_japanese, f"{path} に日本語文字が見つからない（翻訳が反映されていない可能性がある）"

    # 例外を出さず最後まで実行できることを確認する（ログ出力自体は
    # 下記の許可リストチェックとは別に、本番の実行経路がそのまま
    # 動くことの確認として残す）。
    report_untranslated_fragment_candidates(units, log=lambda _msg: None)

    # page全体（全translatable unit）を対象に、未知の候補が無いかを
    # 確認する（2026-08-10追加。フルチェックの本体）。
    known = KNOWN_FALSE_POSITIVE_FRAGMENTS | KNOWN_LEAKED_MATH_FRAGMENTS
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


def _build_synthetic_labeled_pdf(path: Path, page_count: int, page_labels: list[dict] | None = None) -> None:
    """resolve_physical_page系テスト専用のヘルパー。本文の無い白紙ページに
    印刷ページラベル（fitz.Document.set_page_labels）だけを設定した最小限の
    PDFを作る。page_labelsを省略した場合は印刷ページラベル情報を一切持た
    ないPDFになる（sample2.pdfのような学術論文PDFの状態を再現する）。

    "cov"/"COV"/"Cover"の大文字小文字同一視、ローマ数字前付け、印刷ページ
    番号のギャップ（本の誤植等で番号が飛ぶケース）といった性質は、特定の
    書籍PDFの実際のページ構成に依存しない一般的な仕様のため、決定的な
    合成データで検証する。ただし「実際の書籍PDFでfitzが返す生のラベルを
    正しく読めているか」という統合的な確認は、引き続き実データ
    （test_resolve_physical_page_for_sample3、sample3.pdf）が担当する。"""
    doc = fitz.open()
    for _ in range(page_count):
        doc.new_page()
    if page_labels is not None:
        doc.set_page_labels(page_labels)
    doc.save(str(path))
    doc.close()


def test_resolve_physical_page_unknown_label_raises(tmp_path):
    """存在しない印刷ページラベルを指定した場合にエラーになるか。"""
    pdf_path = tmp_path / "labeled.pdf"
    _build_synthetic_labeled_pdf(
        pdf_path, page_count=3,
        page_labels=[{"startpage": 0, "style": "D", "firstpagenum": 1}],
    )
    with pytest.raises(PageLabelResolutionError):
        resolve_physical_page(pdf_path, "zzz")


def test_resolve_physical_page_without_page_labels_raises(tmp_path):
    """印刷ページラベルの情報を持たないPDF（学術論文PDF等）では、
    どのラベルを指定してもエラーになる（--start-labelではなく
    --start/--endを使うよう案内される）。"""
    pdf_path = tmp_path / "no_labels.pdf"
    _build_synthetic_labeled_pdf(pdf_path, page_count=3, page_labels=None)
    with pytest.raises(PageLabelResolutionError):
        resolve_physical_page(pdf_path, "1")


def test_resolve_physical_page_range_matches_previously_verified_pages(tmp_path):
    """印刷ページ番号にギャップ（欠番）がある場合でも、印刷ページラベルから
    物理ページ範囲へ正しく変換できるか。

    合成PDFは物理1〜3ページに印刷ラベル"1"〜"3"、物理4〜6ページに印刷
    ラベル"5"〜"7"を割り当てる（"4"が欠番。sample3.pdfで印刷ページ"38"が
    欠番になっていた実際の構造と同種のギャップを再現している）。印刷ページ
    "3"〜"5"を指定すると、"4"は存在しないため隣接する物理3・4ページ
    （ラベル"3"と"5"）の範囲になる。"""
    pdf_path = tmp_path / "gap.pdf"
    page_labels = [
        {"startpage": 0, "style": "D", "firstpagenum": 1},
        {"startpage": 3, "style": "D", "firstpagenum": 5},
    ]
    _build_synthetic_labeled_pdf(pdf_path, page_count=6, page_labels=page_labels)

    assert resolve_physical_page_range(pdf_path, "3", "5") == (3, 4)


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
