"""Compose frontend/index.html from the Jinja2 template - no HTTP server.

The frontend loads straight off disk via ``file://``: Chromium blocks
ES-module imports and fetch() there, but classic <script src> and
<link rel=stylesheet> work fine - so the page is plain .js/.css files and
only index.html itself is assembled, at app launch (~1 ms).

Regenerate manually with:  uv run python -m loom.compose
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
TEMPLATES_DIR = FRONTEND_DIR / "templates"
OUTPUT = FRONTEND_DIR / "index.html"
DIAG_OUTPUT = FRONTEND_DIR / "diagwin.html"
ART_OUTPUT = FRONTEND_DIR / "artwin.html"
TERM_OUTPUT = FRONTEND_DIR / "termwin.html"

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
    "js/apisrv.js",
    "js/mcptab.js",
    "js/configtab.js",
    "js/archive.js",
    "js/alerts.js",
    "js/envstab.js",
    "js/diag.js",
    "js/main.js",
]


# the popped-out diagnostics window: a lean page reusing the same css
# and the diag view, plus a tiny bootstrap (diagwin.js). No chat.js, no
# tabs - the window shows ONE chat's diagnostics and nothing else.
DIAG_STYLES = [
    "css/tokens.css",
    "css/app.css",
    "css/term.css",
]

DIAG_SCRIPTS = [
    "js/util.js",
    "js/api.js",
    "js/diag.js",
    "js/diagwin.js",
]

# the artifact preview/editor window: the real markdown/code editor over
# one artifact file (text saves back IN PLACE), images render, anything
# else gets a download button.
ART_STYLES = [
    "css/tokens.css",
    "css/app.css",
    "css/mdedit.css",
]

ART_SCRIPTS = [
    "js/util.js",
    "js/api.js",
    "js/mdedit.js",
    "js/artwin.js",
]


# the chat-mirror terminal window: the terminal emulator over one chat's
# exact container setup. termwin.js loads BEFORE terminal.js - it defines
# the `st` global and the main-app stubs terminal.js touches at load time.
TERMWIN_STYLES = [
    "css/tokens.css",
    "css/app.css",
    "css/chat.css",
    "css/term.css",
]

TERMWIN_SCRIPTS = [
    "js/util.js",
    "js/api.js",
    "js/termwin.js",
    "js/terminal.js",
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
    diag = env.get_template("diagwin.html.j2").render(
        styles=DIAG_STYLES,
        scripts=DIAG_SCRIPTS,
    )
    DIAG_OUTPUT.write_text(diag, encoding="utf-8")
    art = env.get_template("artwin.html.j2").render(
        styles=ART_STYLES,
        scripts=ART_SCRIPTS,
    )
    ART_OUTPUT.write_text(art, encoding="utf-8")
    term = env.get_template("termwin.html.j2").render(
        styles=TERMWIN_STYLES,
        scripts=TERMWIN_SCRIPTS,
    )
    TERM_OUTPUT.write_text(term, encoding="utf-8")
    return OUTPUT


if __name__ == "__main__":
    print(compose())
