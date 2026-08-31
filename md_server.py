"""プロジェクト配下の.mdファイルを、ブラウザから直接URLでレンダリング表示する
ローカルサーバー。Windowsのスタートアップフォルダに置かれたバッチファイル
（%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\
start_md_server.bat。venv\\Scripts\\pythonw.exeで本スクリプトを起動するだけ
の1行）により、ログオン時に自動起動される。

使い方:
    ブラウザで http://localhost:8765/doc/architecture.md のように、
    プロジェクトルートからの相対パスをそのままURLに続けてアクセスする。
    毎回ファイルをディスクから読み直すので、編集後はブラウザの再読み込み
    （F5）だけで最新内容が見られる（このスクリプトの再起動は不要）。
    トップページ（http://localhost:8765/）はdoc/配下の.md一覧を表示する。
"""
from __future__ import annotations

import html
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import markdown

PROJECT_ROOT = Path(__file__).resolve().parent
PORT = 8765

PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
body {{
  max-width: 900px;
  margin: 40px auto;
  padding: 0 20px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Hiragino Sans",
    "Yu Gothic", sans-serif;
  line-height: 1.7;
  color: #1f2328;
}}
pre {{ background: #f6f8fa; padding: 12px; border-radius: 6px; overflow-x: auto; }}
code {{ background: #f6f8fa; padding: 2px 5px; border-radius: 4px; font-size: 0.9em; }}
pre code {{ background: none; padding: 0; }}
h1, h2, h3 {{ border-bottom: 1px solid #d0d7de; padding-bottom: 6px; }}
table {{ border-collapse: collapse; }}
th, td {{ border: 1px solid #d0d7de; padding: 6px 12px; }}
blockquote {{ border-left: 4px solid #d0d7de; margin-left: 0; padding-left: 16px; color: #57606a; }}
</style>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
</head>
<body>
{body}
<script>
  document.querySelectorAll("code.language-mermaid").forEach(function (codeEl) {{
    var pre = document.createElement("pre");
    pre.className = "mermaid";
    pre.textContent = codeEl.textContent;
    codeEl.parentElement.replaceWith(pre);
  }});
  mermaid.initialize({{ startOnLoad: true }});
</script>
</body>
</html>
"""


def _render_markdown_page(md_path: Path) -> bytes:
    text = md_path.read_text(encoding="utf-8")
    body = markdown.markdown(text, extensions=["fenced_code", "tables", "toc"])
    html_out = PAGE_TEMPLATE.format(title=md_path.name, body=body)
    return html_out.encode("utf-8")


def _render_index() -> bytes:
    links = []
    for md_path in sorted(PROJECT_ROOT.glob("doc/**/*.md")):
        rel = md_path.relative_to(PROJECT_ROOT).as_posix()
        links.append(f'<li><a href="/{rel}">{html.escape(rel)}</a></li>')
    body = "<h1>doc/ 配下の.mdファイル一覧</h1><ul>" + "".join(links) + "</ul>"
    return PAGE_TEMPLATE.format(title="md_server index", body=body).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.serverの規約に合わせる)
        path = urllib.parse.unquote(self.path.lstrip("/"))

        if path == "":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_render_index())
            return

        if not path.endswith(".md"):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(".md ファイルのみ表示できます".encode("utf-8"))
            return

        md_path = (PROJECT_ROOT / path).resolve()
        if PROJECT_ROOT not in md_path.parents or not md_path.is_file():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"file not found")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(_render_markdown_page(md_path))

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # コンソールへのアクセスログ出力を抑制


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"http://localhost:{PORT}/ で待機中（Ctrl+Cで終了）")
    server.serve_forever()


if __name__ == "__main__":
    main()
