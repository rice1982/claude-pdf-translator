"""
setup_inputs.py

テスト・動作確認に使うサンプルPDF（input/sample0.pdf 〜 sample3.pdf）と、
各サンプルの著作権表示（*_NOTICE.txt）を input/ 配下に準備するセットアップ
スクリプト。

- sample1.pdf / sample2.pdf: arXiv.orgの公式URLから実行のたびにダウンロード
  する（著作権上リポジトリには含めない。再配布不可のためローカル生成のみと
  する）。
- sample0.pdf: ダウンロードしたsample1.pdfの最初の2ページを抽出して生成する
  （軽量テスト用）。ページ抽出にはPyMuPDF（本プロジェクトの既存依存）を使用する。
- sample3.pdf: Springer（オープンアクセス書籍）の公式URLから実行のたびに
  ダウンロードする。SpringerのPDF配信はボット対策（Client Challenge）を
  備えており、Python標準のurllibでは弾かれることを確認済みのため、Windows
  標準同梱のcurlコマンドをサブプロセスとして呼び出す。curlが無い環境や、
  Springer側の対策強化で将来的に失敗するようになった場合は、
  sample3_NOTICE.txtの案内に従って手動でinput/へ配置すること
  （詳細はREADME.md「2-4. テストデータの準備方法」参照）。
- 上記いずれも著作権上リポジトリには含めない（.gitignoreでinput/*を除外
  済み）。ダウンロードに失敗した場合はエラーメッセージを表示して処理を続行
  する（他サンプルの準備は止めない）。

実行方法:
    python setup_inputs.py
"""

import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import fitz  # PyMuPDF（sample0.pdf生成用のページ抽出にのみ使用）

REPO_ROOT = Path(__file__).resolve().parent
INPUT_DIR = REPO_ROOT / "input"

DOWNLOAD_USER_AGENT = "Mozilla/5.0 (compatible; claude-pdf-translator-public/setup_inputs.py)"

SAMPLE1_URL = "https://arxiv.org/pdf/2602.16238v1"
SAMPLE1_PDF = INPUT_DIR / "sample1.pdf"
SAMPLE0_PDF = INPUT_DIR / "sample0.pdf"
SAMPLE0_PAGE_COUNT = 2

SAMPLE2_URL = "https://arxiv.org/pdf/2409.00102"
SAMPLE2_PDF = INPUT_DIR / "sample2.pdf"

SAMPLE3_URL = "https://link.springer.com/content/pdf/10.1007/978-981-95-1327-7.pdf"
SAMPLE3_PDF = INPUT_DIR / "sample3.pdf"


def ensure_input_dir() -> None:
    """input/ ディレクトリが無ければ作成する。"""
    INPUT_DIR.mkdir(exist_ok=True)


def download_pdf(url: str, dest: Path) -> bool:
    """指定URLからPDFをurllib（Python標準ライブラリ）でダウンロードしdestに保存する
    （既に存在する場合はスキップ）。"""
    if dest.exists():
        print(f"[スキップ] {dest} は既に存在します。")
        return True

    print(f"[ダウンロード] {url} -> {dest}")
    request = urllib.request.Request(url, headers={"User-Agent": DOWNLOAD_USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
    except urllib.error.URLError as exc:
        print(f"[エラー] {dest.name}のダウンロードに失敗しました: {exc}", file=sys.stderr)
        return False

    dest.write_bytes(data)
    print(f"[完了] {dest} を保存しました（{len(data):,} bytes）。")
    return True


def download_sample3_via_curl() -> bool:
    """sample3.pdfをcurl経由でダウンロードする（既に存在する場合はスキップ）。

    SpringerのPDF配信はボット対策（Client Challenge）を備えており、Python
    標準のurllibでリクエストするとPDFの代わりにJavaScriptチャレンジページ
    （HTML）が返ってくることを確認済み。curl（Windows 10/11に標準同梱）は
    このチャレンジを通過できるため、ここではcurlをサブプロセスとして呼び出す。
    curlが無い環境や、Springer側の対策強化で将来的にcurlでも失敗するように
    なった場合は、自動ダウンロードを諦めてエラーメッセージのみ表示する
    （sample3_NOTICE.txtに記載の案内に従って手動配置してもらう）。
    """
    if SAMPLE3_PDF.exists():
        print(f"[スキップ] {SAMPLE3_PDF} は既に存在します。")
        return True

    curl_path = shutil.which("curl")
    if curl_path is None:
        print(
            "[警告] curlコマンドが見つからないため、sample3.pdfを自動ダウンロード"
            "できません。sample3_NOTICE.txtの案内に従い、手動でinput/へ配置して"
            "ください。",
            file=sys.stderr,
        )
        return False

    print(f"[ダウンロード] {SAMPLE3_URL} -> {SAMPLE3_PDF} （curl経由）")
    tmp_path = SAMPLE3_PDF.with_suffix(".pdf.download")
    try:
        subprocess.run(
            [
                curl_path,
                "-sL",
                "-A", DOWNLOAD_USER_AGENT,
                "-o", str(tmp_path),
                "--max-time", "60",
                SAMPLE3_URL,
            ],
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"[エラー] sample3.pdfのダウンロードに失敗しました: {exc}", file=sys.stderr)
        tmp_path.unlink(missing_ok=True)
        return False

    if not tmp_path.exists() or tmp_path.stat().st_size == 0:
        print("[エラー] sample3.pdfのダウンロード結果が空でした。", file=sys.stderr)
        tmp_path.unlink(missing_ok=True)
        return False

    # ボット対策ページ（HTML）が返ってきていないか、PDFヘッダで簡易確認する。
    with open(tmp_path, "rb") as f:
        header = f.read(5)
    if header != b"%PDF-":
        print(
            "[エラー] ダウンロードした内容がPDFではありません"
            "（Springer側のボット対策に阻まれた可能性があります）。"
            "sample3_NOTICE.txtの案内に従い、手動でinput/へ配置してください。",
            file=sys.stderr,
        )
        tmp_path.unlink(missing_ok=True)
        return False

    tmp_path.replace(SAMPLE3_PDF)
    print(f"[完了] {SAMPLE3_PDF} を保存しました（{SAMPLE3_PDF.stat().st_size:,} bytes）。")
    return True


def build_sample0_from_sample1() -> None:
    """sample1.pdfの最初の2ページを抽出し、軽量テスト用のsample0.pdfを生成する。"""
    if SAMPLE0_PDF.exists():
        print(f"[スキップ] {SAMPLE0_PDF} は既に存在します。")
        return
    if not SAMPLE1_PDF.exists():
        print(
            f"[警告] {SAMPLE1_PDF} が無いためsample0.pdfを生成できません。",
            file=sys.stderr,
        )
        return

    print(
        f"[生成] {SAMPLE1_PDF} の最初の{SAMPLE0_PAGE_COUNT}ページから "
        f"{SAMPLE0_PDF} を生成します。"
    )
    src = fitz.open(SAMPLE1_PDF)
    try:
        page_count = min(SAMPLE0_PAGE_COUNT, src.page_count)
        dst = fitz.open()
        try:
            dst.insert_pdf(src, from_page=0, to_page=page_count - 1)
            dst.save(SAMPLE0_PDF)
        finally:
            dst.close()
    finally:
        src.close()
    print(f"[完了] {SAMPLE0_PDF} を保存しました。")


def write_notice(filename: str, content: str) -> None:
    """1件分の著作権表示（NOTICE.txt）をinput/配下に書き出す。"""
    path = INPUT_DIR / filename
    path.write_text(content, encoding="utf-8")
    print(f"[完了] {path} を書き出しました。")


def write_all_notices() -> None:
    """sample0〜sample3各PDFの著作権表示（NOTICE.txt）をinput/配下に書き出す。"""
    write_notice(
        "sample1_NOTICE.txt",
        (
            "sample1.pdf\n"
            "\n"
            f"取得元URL: {SAMPLE1_URL}\n"
            "\n"
            "arXiv.orgの利用規約に基づき取得。再配布不可のためローカル生成のみとする。\n"
            "本ファイルはリポジトリには含まれず、setup_inputs.py実行時に都度上記URLから\n"
            "ダウンロードされます。\n"
        ),
    )

    write_notice(
        "sample0_NOTICE.txt",
        (
            "sample0.pdf\n"
            "\n"
            f"sample1.pdf（取得元URL: {SAMPLE1_URL}）の最初の"
            f"{SAMPLE0_PAGE_COUNT}ページを抽出したものです。\n"
            "\n"
            "元ファイルと同様、arXiv.orgの利用規約に基づき取得。再配布不可のため\n"
            "ローカル生成のみとする。本ファイルはリポジトリには含まれず、\n"
            "setup_inputs.py実行時に都度sample1.pdfから生成されます。\n"
        ),
    )

    write_notice(
        "sample2_NOTICE.txt",
        (
            "sample2.pdf\n"
            "\n"
            "タイトル: Collective predictive coding as model of science\n"
            f"取得元URL: {SAMPLE2_URL} （arXiv:2409.00102、著者投稿稿）\n"
            "\n"
            "arXiv.orgの利用規約（非独占配布ライセンス、"
            "http://arxiv.org/licenses/nonexclusive-distrib/1.0/ ）に基づき取得。\n"
            "再配布不可のためローカル生成のみとする。\n"
            "\n"
            "補足: 本論文の査読済み・出版版（Royal Society Open Science誌、\n"
            "DOI: 10.1098/rsos.241678）はCC BY 4.0\n"
            "(https://creativecommons.org/licenses/by/4.0/) で公開されていますが、\n"
            "出版社側の配信はボット対策により本スクリプトから自動取得できないため、\n"
            "内容が同一のarXiv版をローカル検証目的で取得しています。\n"
            "本ファイルはリポジトリには含まれず、setup_inputs.py実行時に都度上記URLから\n"
            "ダウンロードされます。\n"
        ),
    )

    write_notice(
        "sample3_NOTICE.txt",
        (
            "sample3.pdf\n"
            "\n"
            "タイトル: Symbol Emergence Systems (Springer)\n"
            f"取得元URL: {SAMPLE3_URL} （DOI: 10.1007/978-981-95-1327-7）\n"
            "ライセンス: CC BY-NC-ND 4.0 "
            "(https://creativecommons.org/licenses/by-nc-nd/4.0/)\n"
            "注意事項: 非営利目的でのみ使用可能。改変不可。\n"
            "\n"
            "本ファイルはリポジトリには含まれず、setup_inputs.py実行時に都度上記URLから\n"
            "ダウンロードされます（Springer側のボット対策の都合上、curlコマンドを\n"
            "サブプロセスとして呼び出して取得します）。curlが無い環境や、Springer側の\n"
            "対策強化により自動取得に失敗した場合は、本ファイルの案内に従い上記URLに\n"
            "ブラウザでアクセスして手動でダウンロードし、input\\sample3.pdfとして\n"
            "配置してください。\n"
        ),
    )


def report_missing_pdfs() -> None:
    """ダウンロードに失敗するなどして無いままのサンプルPDFを案内する。"""
    for path in (SAMPLE0_PDF, SAMPLE1_PDF, SAMPLE2_PDF, SAMPLE3_PDF):
        if not path.exists():
            print(
                f"[案内] {path} が見つかりません。"
                "ネットワーク接続や取得元URLの変更が無いか確認の上、"
                "再度setup_inputs.pyを実行するか、対応する*_NOTICE.txtに記載の"
                "ライセンスに従いご自身で入手してinput/配下へ配置してください。"
            )


def main() -> None:
    ensure_input_dir()
    download_pdf(SAMPLE1_URL, SAMPLE1_PDF)
    build_sample0_from_sample1()
    download_pdf(SAMPLE2_URL, SAMPLE2_PDF)
    download_sample3_via_curl()
    write_all_notices()
    report_missing_pdfs()
    print("\nセットアップ処理が完了しました。")


if __name__ == "__main__":
    main()
