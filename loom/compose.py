"""Compose frontend/index.html from the Jinja2 template — no HTTP server.

The frontend loads straight off disk via ``file://``: Chromium blocks
ES-module imports and fetch() there, but classic <script src> and
<link rel=stylesheet> work fine — so the page is plain .js/.css files and
only index.html itself is assembled, at app launch (~1 ms).

Regenerate manually with:  uv run python -m loom.compose
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
TEMPLATES_DIR = FRONTEND_DIR / "templates"
OUTPUT = FRONTEND_DIR / "index.html"

# Order matters: plain script tags, no modules. Later files may use
# globals defined by earlier ones.
STYLES = [
    "css/tokens.css",
    "css/app.css",
    "css/mdedit.css",
    "css/chat.css",
    "css/term.css",
]

THIRD_PARTY = [
    "3rdparty/marked.umd.js",
    "3rdparty/purify.min.js",
]

APP_SCRIPTS = [
    "js/util.js",
    "js/api.js",
    "js/state.js",
    "js/hotkeys.js",
    "js/mdedit.js",
    "js/tabs.js",
    "js/picker.js",
    "js/librarytab.js",
    "js/servers.js",
    "js/chat.js",
    "js/terminal.js",
    "js/modelstab.js",
    "js/downloader.js",
    "js/apisrv.js",
    "js/mcptab.js",
    "js/archive.js",
    "js/alerts.js",
    "js/envstab.js",
    "js/diag.js",
    "js/main.js",
]


def compose() -> Path:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    html = env.get_template("index.html.j2").render(
        styles=STYLES,
        scripts=THIRD_PARTY + APP_SCRIPTS,
    )
    OUTPUT.write_text(html, encoding="utf-8")
    return OUTPUT


if __name__ == "__main__":
    print(compose())
