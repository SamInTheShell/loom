# Vendored third-party frontend libraries

Everything the page loads is on disk — nothing is fetched from the
network at runtime (the app runs from a `file://` origin).

| file | project | version | license |
| --- | --- | --- | --- |
| `marked.umd.js` | [marked](https://github.com/markedjs/marked) | 18.0.9 | MIT (`marked.LICENSE`) |
| `purify.min.js` | [DOMPurify](https://github.com/cure53/DOMPurify) | 3.4.13 | Apache-2.0 / MPL-2.0 (`purify.LICENSE`) |
| `mcp-dark-icon.svg` | [Model Context Protocol](https://modelcontextprotocol.io) logo | — | MIT (logo glyph also inlined as the `mcp` icon in `js/util.js`) |

Hard rule: every string that passes through `marked` gets
`DOMPurify.sanitize()` before it touches `innerHTML`.
