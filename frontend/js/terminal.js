/* terminal.js — terminal tabs (Ctrl+T), lifted from CodeTree's emulator.
 *
 * A VT100/xterm subset broad enough for modern full-screen programs
 * (nvim, less, htop): alternate screen, scroll regions, insert/delete
 * lines & cells, 16/256/truecolor SGR, application cursor keys, mouse
 * reporting (SGR + legacy), bracketed paste, OSC titles and OSC 52
 * clipboard, DEC line drawing, and the query/reply pairs programs probe
 * with. No vendored xterm — the app ships no external resources.
 *
 * Gated "modern features" (alt screen, mouse, bracketed paste, title,
 * clipboard) go through ask/allow/deny; "ask" prompts stack in the
 * corner and the OLDEST owns Ctrl+Y / Ctrl+N. Render-critical features
 * PAUSE the stream while asking so an "allow" replays the setup bytes.
 *
 * Copy/paste: Ctrl+Shift+C / Ctrl+Shift+V, right-click menu, native
 * selection (Shift bypasses program mouse reporting); repaints never
 * destroy an active selection. Shift+PageUp/PageDown scroll the buffer.
 */
"use strict";

/* ==================== feature permission catalog ==================== */

const TERM_FEATURES = [
  { id: "altScreen", name: "Alternate screen", def: "allow", pause: true,
    desc: "Full-screen apps (nvim, less, htop) draw on a separate screen and restore your scrollback on exit." },
  { id: "mouse", name: "Mouse reporting", def: "allow", pause: true,
    desc: "The program receives clicks, drags and wheel events. While active, hold Shift to select text normally." },
  { id: "bracketedPaste", name: "Bracketed paste", def: "allow", pause: true,
    desc: "Pastes are wrapped in markers so programs can tell pasted text from typed keystrokes." },
  { id: "title", name: "Set the tab title", def: "allow", pause: false,
    desc: "The program renames this terminal tab (OSC 0/2)." },
  { id: "clipboardWrite", name: "Write the system clipboard", def: "ask", pause: false,
    desc: "OSC 52 — the program places text on your clipboard. It can do this invisibly, hence the prompt." },
  { id: "clipboardRead", name: "Read the system clipboard", def: "ask", pause: false,
    desc: "OSC 52 query — the program reads your clipboard contents." },
];

function termFeatureLevel(id) {
  try {
    const saved = JSON.parse(localStorage.getItem("loom-term-features") || "{}");
    if (["ask", "allow", "deny"].includes(saved[id])) return saved[id];
  } catch (e) { /* fresh */ }
  return TERM_FEATURES.find((f) => f.id === id)?.def || "ask";
}
function termFeatureRemember(id, decision) {
  try {
    const saved = JSON.parse(localStorage.getItem("loom-term-features") || "{}");
    saved[id] = decision;
    localStorage.setItem("loom-term-features", JSON.stringify(saved));
  } catch (e) { /* best effort */ }
}

/* ==================== the emulator core ==================== */

function ansi256(n) {
  n = Math.max(0, Math.min(255, n | 0));
  if (n < 16) return null;
  if (n < 232) {
    const c = n - 16;
    const v = (x) => (x === 0 ? 0 : 55 + x * 40);
    return `rgb(${v((c / 36) | 0)},${v(((c / 6) | 0) % 6)},${v(c % 6)})`;
  }
  const g = 8 + (n - 232) * 10;
  return `rgb(${g},${g},${g})`;
}

const DEC_GFX = { "j": "┘", "k": "┐", "l": "┌", "m": "└", "n": "┼", "q": "─",
  "t": "├", "u": "┤", "v": "┴", "w": "┬", "x": "│", "a": "▒", "`": "◆",
  "f": "°", "g": "±", "~": "·", "o": "⎺", "s": "⎽", "0": "█" };

const TERM_SB_MAX = 3000;

class TermScreen {
  constructor(cols, rows, cb) {
    this.cb = cb;
    this.cols = Math.max(20, cols | 0);
    this.rows = Math.max(5, rows | 0);
    this.reset();
    this._pend = "";
    this.paused = false;
    this.featDecision = {};
  }

  reset() {
    this.grid = this._blankGrid(this.rows);
    this.sb = [];
    this.sbNew = [];
    this.alt = null;
    this.cur = { r: 0, c: 0 };
    this.savedCur = { r: 0, c: 0 };
    this.attr = null;
    this.top = 0; this.bot = this.rows - 1;
    this.wrap = true; this.pendingWrap = false;
    this.originMode = false;
    this.appCursor = false;
    this.cursorVisible = true;
    this.cursorShape = "block";
    this.mouseMode = 0;
    this.mouseSGR = false;
    this.bracketedPaste = false;
    this.focusEvents = false;
    this.charset = "B"; this.charsets = { "(": "B", ")": "B" };
    this._st = "norm"; this._esc = ""; this._osc = ""; this._collect = "";
  }

  _blankGrid(n) { return Array.from({ length: n }, () => this._blankRow()); }
  _blankRow() {
    const row = new Array(this.cols);
    for (let i = 0; i < this.cols; i++) row[i] = null;
    return row;
  }

  resize(cols, rows) {
    cols = Math.max(20, cols | 0); rows = Math.max(5, rows | 0);
    if (cols === this.cols && rows === this.rows) return;
    const fix = (grid) => {
      for (const row of grid) row.length = cols;
      for (const row of grid) for (let i = 0; i < cols; i++) if (row[i] === undefined) row[i] = null;
      while (grid.length < rows) grid.push(this._blankRow());
      while (grid.length > rows) {
        if (!this.alt && grid === this.grid && this.cur.r > 0) { this._pushSb(grid.shift()); this.cur.r--; }
        else grid.pop();
      }
    };
    this.cols = cols;
    fix(this.grid);
    if (this.alt) fix(this.alt.grid);
    this.rows = rows;
    this.top = 0; this.bot = rows - 1;
    this.cur.r = Math.min(this.cur.r, rows - 1);
    this.cur.c = Math.min(this.cur.c, cols - 1);
    this.cb.refresh();
  }

  _feature(id, apply) {
    const cached = this.featDecision[id];
    if (cached === "allow") { apply(); return true; }
    if (cached === "deny") return true;
    const lvl = termFeatureLevel(id);
    if (lvl === "allow") { this.featDecision[id] = "allow"; apply(); return true; }
    if (lvl === "deny") { this.featDecision[id] = "deny"; return true; }
    const pause = !!TERM_FEATURES.find((f) => f.id === id)?.pause;
    this.cb.ask(id, apply);
    if (pause) { this.paused = true; return false; }
    return true;
  }

  _archiveGrid() {
    let last = -1;
    for (let r = 0; r < this.rows; r++) {
      if (this.grid[r] && this.grid[r].some((c) => c)) last = r;
    }
    for (let r = 0; r <= last; r++) this._pushSb(this.grid[r]);
    this.grid = this._blankGrid(this.rows);
  }

  archive() {
    if (this.alt) this._exitAlt(false);
    const keep = this.grid;
    const sb = this.sb;
    this.reset();
    this.sb = sb;
    let last = -1;
    for (let r = 0; r < keep.length; r++) if (keep[r] && keep[r].some((c) => c)) last = r;
    for (let r = 0; r <= last; r++) this._pushSb(keep[r]);
  }

  answerFeature(id, decision, applies) {
    this.featDecision[id] = decision;
    if (decision === "allow") for (const fn of applies) { try { fn(); } catch (e) { /* ignore */ } }
    if (this.paused) {
      this.paused = false;
      const rest = this._pend; this._pend = "";
      if (rest) this.feed(rest);
    }
    this.cb.refresh();
  }

  feed(s) {
    if (this.paused) { this._pend = (this._pend + s).slice(-2_000_000); return; }
    for (let i = 0; i < s.length; i++) {
      const ch = s[i];
      if (this._st === "norm") this._normal(ch);
      else if (this._st === "esc") this._escSt(ch);
      else if (this._st === "csi") this._csiSt(ch);
      else if (this._st === "osc") this._oscSt(ch);
      else if (this._st === "oscEsc") { if (ch === "\\") this._oscDone(); else this._st = "osc"; }
      else if (this._st === "dcs") { if (ch === "\x1b") this._st = "dcsEsc"; }
      else if (this._st === "dcsEsc") this._st = ch === "\\" ? "norm" : "dcs";
      else if (this._st === "charset") { this.charsets[this._collect] = ch; if (this._collect === "(") this.charset = ch; this._st = "norm"; }
      if (this.paused) { this._pend = s.slice(i + 1); break; }
    }
    this.cb.refresh();
  }

  _normal(ch) {
    const code = ch.charCodeAt(0);
    if (ch === "\x1b") { this._st = "esc"; return; }
    if (ch === "\r") { this.cur.c = 0; this.pendingWrap = false; return; }
    if (ch === "\n" || ch === "\x0b" || ch === "\x0c") { this._lf(); return; }
    if (ch === "\b") { if (this.cur.c > 0) this.cur.c--; this.pendingWrap = false; return; }
    if (ch === "\t") { this.cur.c = Math.min(this.cols - 1, (Math.floor(this.cur.c / 8) + 1) * 8); return; }
    if (ch === "\x07") { this.cb.bell(); return; }
    if (ch === "\x0e") { this.charset = this.charsets[")"] || "B"; return; }
    if (ch === "\x0f") { this.charset = this.charsets["("] || "B"; return; }
    if (code < 32) return;
    this._print(this.charset === "0" ? (DEC_GFX[ch] || ch) : ch);
  }

  _print(ch) {
    if (this.pendingWrap && this.wrap) { this.pendingWrap = false; this._lf(); this.cur.c = 0; }
    const row = this.grid[this.cur.r];
    if (!row) return;
    row[this.cur.c] = this.attr ? { c: ch, a: this.attr } : (ch === " " ? null : { c: ch, a: null });
    if (this.cur.c >= this.cols - 1) this.pendingWrap = true;
    else this.cur.c++;
  }

  _lf() {
    if (this.cur.r === this.bot) this._scrollUp(1);
    else if (this.cur.r < this.rows - 1) this.cur.r++;
  }

  _pushSb(row) {
    this.sb.push(row); this.sbNew.push(row);
    if (this.sb.length > TERM_SB_MAX * 2) this.sb.splice(0, this.sb.length - TERM_SB_MAX);
    if (this.sbNew.length > TERM_SB_MAX) this.sbNew.splice(0, this.sbNew.length - TERM_SB_MAX);
  }

  _scrollUp(n) {
    for (let i = 0; i < n; i++) {
      const gone = this.grid.splice(this.top, 1)[0];
      if (this.top === 0 && this.bot === this.rows - 1 && !this.alt) this._pushSb(gone);
      this.grid.splice(this.bot, 0, this._blankRow());
    }
  }
  _scrollDown(n) {
    for (let i = 0; i < n; i++) {
      this.grid.splice(this.bot, 1);
      this.grid.splice(this.top, 0, this._blankRow());
    }
  }

  _escSt(ch) {
    if (ch === "[") { this._esc = ""; this._st = "csi"; return; }
    if (ch === "]") { this._osc = ""; this._st = "osc"; return; }
    if (ch === "P" || ch === "^" || ch === "_" || ch === "X") { this._st = "dcs"; return; }
    if (ch === "(" || ch === ")") { this._collect = ch; this._st = "charset"; return; }
    this._st = "norm";
    if (ch === "7") this.savedCur = { ...this.cur };
    else if (ch === "8") { this.cur = { ...this.savedCur }; this.pendingWrap = false; }
    else if (ch === "D") this._lf();
    else if (ch === "E") { this._lf(); this.cur.c = 0; }
    else if (ch === "M") { if (this.cur.r === this.top) this._scrollDown(1); else if (this.cur.r > 0) this.cur.r--; }
    else if (ch === "c") this.reset();
  }

  _csiSt(ch) {
    if ((ch >= "0" && ch <= "9") || ch === ";" || ch === ":" || ch === "?" || ch === ">" ||
        ch === "<" || ch === "=" || ch === " " || ch === "!" || ch === '"' || ch === "$" || ch === "'") {
      this._esc += ch;
      if (this._esc.length > 64) this._st = "norm";
      return;
    }
    this._st = "norm";
    this._csi(this._esc, ch);
  }

  _params(raw) {
    return raw.replace(/^[?<>=]/, "").split(";").map((p) => {
      const n = parseInt(p.split(":")[0], 10);
      return Number.isFinite(n) ? n : 0;
    }).map((n) => n || 0);
  }

  _csi(raw, cmd) {
    const priv = raw.startsWith("?");
    const P = this._params(raw);
    const p0 = P[0] || 0;
    const n = Math.max(1, p0);
    const clampR = (r) => Math.max(0, Math.min(this.rows - 1, r));
    const clampC = (c) => Math.max(0, Math.min(this.cols - 1, c));
    this.pendingWrap = false;

    if (cmd === "m") { this._sgr(raw); return; }
    if (cmd === "H" || cmd === "f") {
      const base = this.originMode ? this.top : 0;
      this.cur.r = clampR(base + (P[0] ? P[0] - 1 : 0));
      this.cur.c = clampC(P[1] ? P[1] - 1 : 0);
      if (this.originMode) this.cur.r = Math.min(this.cur.r, this.bot);
      return;
    }
    if (cmd === "A") { this.cur.r = Math.max(this.originMode ? this.top : 0, this.cur.r - n); return; }
    if (cmd === "B") { this.cur.r = Math.min(this.originMode ? this.bot : this.rows - 1, this.cur.r + n); return; }
    if (cmd === "C") { this.cur.c = clampC(this.cur.c + n); return; }
    if (cmd === "D") { this.cur.c = clampC(this.cur.c - n); return; }
    if (cmd === "E") { this.cur.r = clampR(this.cur.r + n); this.cur.c = 0; return; }
    if (cmd === "F") { this.cur.r = clampR(this.cur.r - n); this.cur.c = 0; return; }
    if (cmd === "G" || cmd === "`") { this.cur.c = clampC(n - 1); return; }
    if (cmd === "d") { this.cur.r = clampR(n - 1); return; }
    if (cmd === "J") { this._ed(p0); return; }
    if (cmd === "K") { this._el(p0); return; }
    if (cmd === "L") { this._il(n); return; }
    if (cmd === "M") { this._dl(n); return; }
    if (cmd === "P") { this._dch(n); return; }
    if (cmd === "@") { this._ich(n); return; }
    if (cmd === "X") { const row = this.grid[this.cur.r]; for (let i = 0; i < n && this.cur.c + i < this.cols; i++) row[this.cur.c + i] = null; return; }
    if (cmd === "S") { this._scrollUp(n); return; }
    if (cmd === "T") { this._scrollDown(n); return; }
    if (cmd === "r") {
      const t = (P[0] ? P[0] - 1 : 0), b = (P[1] ? P[1] - 1 : this.rows - 1);
      if (b > t) { this.top = clampR(t); this.bot = clampR(b); this.cur = { r: this.originMode ? this.top : 0, c: 0 }; }
      return;
    }
    if (cmd === "s") { this.savedCur = { ...this.cur }; return; }
    if (cmd === "u") { this.cur = { ...this.savedCur }; return; }
    if (cmd === "h" || cmd === "l") { this._mode(priv, P, cmd === "h"); return; }
    if (cmd === "n") {
      if (p0 === 5) this.cb.reply("\x1b[0n");
      else if (p0 === 6) {
        const r = this.cur.r - (this.originMode ? this.top : 0) + 1;
        this.cb.reply(`\x1b[${r};${this.cur.c + 1}R`);
      }
      return;
    }
    if (cmd === "c") {
      if (raw.startsWith(">")) this.cb.reply("\x1b[>0;10;1c");
      else this.cb.reply("\x1b[?62;22c");
      return;
    }
    if (cmd === "t") {
      if (p0 === 18) this.cb.reply(`\x1b[8;${this.rows};${this.cols}t`);
      else if (p0 === 14) this.cb.reply(`\x1b[4;${this.rows * 17};${this.cols * 8}t`);
      return;
    }
    if (cmd === "q" && raw.endsWith(" ")) {
      this.cursorShape = p0 >= 5 ? "bar" : p0 >= 3 ? "underline" : "block";
      return;
    }
  }

  _mode(priv, P, on) {
    if (!priv) return;
    for (const p of P) {
      if (p === 1) this.appCursor = on;
      else if (p === 6) { this.originMode = on; this.cur = { r: on ? this.top : 0, c: 0 }; }
      else if (p === 7) this.wrap = on;
      else if (p === 25) this.cursorVisible = on;
      else if (p === 1004) this.focusEvents = on;
      else if (p === 1000 || p === 1002 || p === 1003) {
        if (!on) { this.mouseMode = 0; continue; }
        this._feature("mouse", () => { this.mouseMode = p; });
      }
      else if (p === 1006) {
        if (!on) { this.mouseSGR = false; continue; }
        this._feature("mouse", () => { this.mouseSGR = true; });
      }
      else if (p === 2004) {
        if (!on) { this.bracketedPaste = false; continue; }
        this._feature("bracketedPaste", () => { this.bracketedPaste = true; });
      }
      else if (p === 47 || p === 1047 || p === 1049) {
        if (on) this._feature("altScreen", () => this._enterAlt(p === 1049));
        else this._exitAlt(p === 1049);
      }
      else if (p === 1048) {
        if (on) this.savedCur = { ...this.cur }; else this.cur = { ...this.savedCur };
      }
      if (this.paused) return;
    }
  }

  _enterAlt(saveCursor) {
    if (this.alt) return;
    this.alt = { grid: this.grid, cur: saveCursor ? { ...this.cur } : null,
                 top: this.top, bot: this.bot };
    this.grid = this._blankGrid(this.rows);
    this.top = 0; this.bot = this.rows - 1;
    this.cur = { r: 0, c: 0 };
  }
  _exitAlt(restoreCursor) {
    if (!this.alt) return;
    this.grid = this.alt.grid;
    this.top = this.alt.top; this.bot = this.alt.bot;
    if (restoreCursor && this.alt.cur) this.cur = { ...this.alt.cur };
    this.alt = null;
    this.cur.r = Math.min(this.cur.r, this.rows - 1);
    this.cur.c = Math.min(this.cur.c, this.cols - 1);
  }

  _ed(p) {
    if (p === 3) return;   // never erase the user's scrollback
    if (p === 2) {
      if (this.alt) this.grid = this._blankGrid(this.rows);
      else this._archiveGrid();   // clear ARCHIVES, never discards
      return;
    }
    if (p === 1) {
      for (let r = 0; r < this.cur.r; r++) this.grid[r] = this._blankRow();
      const row = this.grid[this.cur.r];
      for (let c = 0; c <= this.cur.c; c++) row[c] = null;
      return;
    }
    const row = this.grid[this.cur.r];
    for (let c = this.cur.c; c < this.cols; c++) row[c] = null;
    for (let r = this.cur.r + 1; r < this.rows; r++) this.grid[r] = this._blankRow();
  }

  _el(p) {
    const row = this.grid[this.cur.r];
    const [a, b] = p === 2 ? [0, this.cols] : p === 1 ? [0, this.cur.c + 1] : [this.cur.c, this.cols];
    for (let c = a; c < b; c++) row[c] = null;
  }

  _il(n) {
    if (this.cur.r < this.top || this.cur.r > this.bot) return;
    for (let i = 0; i < n; i++) {
      this.grid.splice(this.bot, 1);
      this.grid.splice(this.cur.r, 0, this._blankRow());
    }
  }
  _dl(n) {
    if (this.cur.r < this.top || this.cur.r > this.bot) return;
    for (let i = 0; i < n; i++) {
      this.grid.splice(this.cur.r, 1);
      this.grid.splice(this.bot, 0, this._blankRow());
    }
  }
  _dch(n) {
    const row = this.grid[this.cur.r];
    row.splice(this.cur.c, n);
    while (row.length < this.cols) row.push(null);
  }
  _ich(n) {
    const row = this.grid[this.cur.r];
    for (let i = 0; i < n; i++) row.splice(this.cur.c, 0, null);
    row.length = this.cols;
  }

  _sgr(raw) {
    const parts = raw.split(";").flatMap((p) => {
      const sub = p.split(":");
      return sub.length > 1 ? [sub] : [[p]];
    });
    let a = this.attr ? { ...this.attr } : {};
    let i = 0;
    const flat = [];
    for (const p of parts) flat.push(p);
    while (i < flat.length) {
      const sub = flat[i];
      const v = parseInt(sub[0], 10) || 0;
      if (v === 0) a = {};
      else if (v === 1) a.b = 1;
      else if (v === 2) a.dim = 1;
      else if (v === 3) a.i = 1;
      else if (v === 4) a.u = 1;
      else if (v === 7) a.inv = 1;
      else if (v === 9) a.strike = 1;
      else if (v === 22) { delete a.b; delete a.dim; }
      else if (v === 23) delete a.i;
      else if (v === 24) delete a.u;
      else if (v === 27) delete a.inv;
      else if (v === 29) delete a.strike;
      else if (v >= 30 && v <= 37) a.fg = v - 30;
      else if (v >= 90 && v <= 97) a.fg = v - 90 + 8;
      else if (v === 39) delete a.fg;
      else if (v >= 40 && v <= 47) a.bg = v - 40;
      else if (v >= 100 && v <= 107) a.bg = v - 100 + 8;
      else if (v === 49) delete a.bg;
      else if (v === 38 || v === 48) {
        const key = v === 38 ? "fg" : "bg";
        let mode, args;
        if (sub.length > 1) { mode = parseInt(sub[1], 10); args = sub.slice(2).map((x) => parseInt(x, 10) || 0); }
        else {
          mode = parseInt(flat[i + 1]?.[0], 10);
          args = mode === 2 ? [flat[i + 2], flat[i + 3], flat[i + 4]].map((x) => parseInt(x?.[0], 10) || 0)
               : [parseInt(flat[i + 2]?.[0], 10) || 0];
          i += mode === 2 ? 4 : 2;
        }
        if (mode === 5) a[key] = args[0] | 0;
        else if (mode === 2) a[key] = `rgb(${args[0] | 0},${args[1] | 0},${args[2] | 0})`;
      }
      i++;
    }
    this.attr = Object.keys(a).length ? a : null;
  }

  _oscSt(ch) {
    if (ch === "\x07") { this._oscDone(); return; }
    if (ch === "\x1b") { this._st = "oscEsc"; return; }
    this._osc += ch;
    if (this._osc.length > 100_000) { this._osc = ""; this._st = "norm"; }
  }

  _oscDone() {
    const osc = this._osc; this._osc = "";
    this._st = "norm";
    const semi = osc.indexOf(";");
    const code = parseInt(semi >= 0 ? osc.slice(0, semi) : osc, 10);
    const rest = semi >= 0 ? osc.slice(semi + 1) : "";
    if (code === 0 || code === 2) {
      this._feature("title", () => this.cb.title(rest.slice(0, 120)));
    } else if (code === 52) {
      const m = rest.match(/^([^;]*);([\s\S]*)$/);
      const payload = m ? m[2] : "";
      if (payload === "?") {
        this._feature("clipboardRead", () => {
          const done = (text) => this.cb.reply("\x1b]52;c;" + btoa(unescape(encodeURIComponent(text || ""))) + "\x07");
          try { navigator.clipboard.readText().then(done, () => done("")); }
          catch (e) { done(""); }
        });
      } else if (payload) {
        this._feature("clipboardWrite", () => {
          try {
            const text = decodeURIComponent(escape(atob(payload)));
            copyText(text);
          } catch (e) { /* malformed base64 */ }
        });
      }
    } else if (code === 10 || code === 11) {
      if (rest.trim() === "?") {
        this.cb.reply(code === 10 ? "\x1b]10;rgb:fafa/fafa/fafa\x07"
                                  : "\x1b]11;rgb:0909/0909/0b0b\x07");
      }
    }
  }
}

/* ==================== rendering ==================== */

function _termCellStyle(cell, cursorHere, shape) {
  if (!cell && !cursorHere) return "";
  const a = (cell && cell.a) || {};
  let fg = a.fg, bg = a.bg;
  if (a.inv) {
    const t = fg;
    fg = bg !== undefined ? bg : "inv";
    bg = t !== undefined ? t : "inv";
  }
  const cls = [];
  const sty = [];
  const put = (val, kind) => {
    if (val === undefined) return;
    if (val === "inv") { cls.push("t" + kind + "inv"); return; }
    if (typeof val === "number") {
      if (val < 16) cls.push("t" + kind + val);
      else sty.push((kind === "f" ? "color" : "background-color") + ":" + ansi256(val));
    } else sty.push((kind === "f" ? "color" : "background-color") + ":" + val);
  };
  put(fg, "f"); put(bg, "b");
  if (a.b) cls.push("tbold");
  if (a.dim) cls.push("tdim");
  if (a.i) cls.push("tital");
  if (a.u) cls.push("tund");
  if (a.strike) cls.push("tstrike");
  if (cursorHere) cls.push("tcur", "tcur-" + shape);
  return (cls.length ? ' class="' + cls.join(" ") + '"' : "")
       + (sty.length ? ' style="' + sty.join(";") + '"' : "");
}

function _termRowHtml(row, cols, curCol, shape) {
  let html = "";
  let runStyle = "", run = "";
  const flush = () => { if (run) { html += "<span" + runStyle + ">" + esc(run) + "</span>"; run = ""; } };
  for (let c = 0; c < cols; c++) {
    const cell = row ? row[c] : null;
    const styleKey = _termCellStyle(cell, c === curCol, shape);
    if (styleKey !== runStyle) { flush(); runStyle = styleKey; }
    run += (cell && cell.c) || " ";
  }
  flush();
  return html || " ";
}

/* ==================== Loom tab integration ==================== */

/* st.terms: termId -> {container, folders:[{path,mode}], network,
 * title, started, running} — the tab's config, persisted in the session */
function termState(id) {
  return st.terms[id] || (st.terms[id] = {
    container: st.config?.containers?.default || "sandbox",
    folders: [], network: false, env: "",
    title: null, started: false, running: false,
  });
}

function newTerminal() {
  if (!st.library) return;
  // a new terminal inherits its setup from the active terminal tab —
  // or, when launched from elsewhere, from the most recently used
  // terminal, or the last terminal tab in the strip
  const active = tabById(st.activeTab);
  let src = active?.type === "term" ? st.terms[active.chatId] : null;
  if (!src && st._lastTermId) src = st.terms[st._lastTermId] || null;
  if (!src) {
    const termTabs = st.tabs.filter((tb) => tb.type === "term");
    const lastTab = termTabs[termTabs.length - 1];
    if (lastTab) src = st.terms[lastTab.chatId] || null;
  }
  const id = "t" + Math.random().toString(36).slice(2, 10);
  const t = termState(id);
  if (src) {
    t.container = src.container;
    t.folders = src.folders.map((f) => ({ ...f }));
    t.network = src.network;
    t.env = src.env || "";
  }
  openTab("term", id);
  saveSession();
}

function termTabTitle(id) {
  const t = st.terms[id];
  return (t?.title) || "terminal";
}

async function closeTerminalTab(id) {
  const t = st.terms[id];
  const finish = () => {
    Api.call("term_cleanup", id);
    const v = st._termViews?.[id];
    if (v) { v.dead = true; delete st._termViews[id]; }
    delete st.terms[id];
    removeTab("term:" + id);
    saveSession();
  };
  if (!t?.started || !t?.running) { finish(); return; }
  let procs = [];
  try {
    const res = await Api.call("term_procs", id);
    procs = res.ok ? res.data.procs || [] : [];
  } catch (e) { /* backend gone */ }
  if (!procs.length) { finish(); return; }
  modal("Programs still running",
    [el("p", { text: "This terminal is running more than a shell. Closing kills:" }),
     el("div", { class: "tool-args", text: procs.join("\n") })],
    [{ label: "Keep running" },
     { label: "Close anyway", cls: "btn-danger", fn: finish }]);
}

/* ---------- the tab panel ---------- */
function mountTermTab(panel, id) {
  panel.classList.add("termtab");
  renderTermTab(id);
}

function renderTermTab(id) {
  const panel = panelFor("term:" + id);
  if (!panel) return;
  const t = termState(id);
  panel.replaceChildren();
  if (!t.started) {
    panel.append(termSetupForm(id));
  } else {
    const v = ensureTermView(id);
    panel.append(termHeaderBar(id), v.el);
    setTimeout(() => {
      v.measure?.();
      if (!v.opening && !v.exited && !t.running) termAttachOrOpen(id, v);
      v.el.focus();
    }, 0);
  }
}

function termSetupForm(id) {
  const t = termState(id);
  const defs = (st.config?.containers?.definitions || []).map((d) => d.name);
  if (!defs.includes(t.container)) t.container = defs[0] || "sandbox";

  const contSel = el("select", { class: "compose-model term-sel" });
  for (const name of defs) contSel.append(el("option", { value: name, text: name }));
  contSel.value = t.container;
  contSel.addEventListener("change", () => { t.container = contSel.value; saveSession(); });

  const mounts = el("div", { class: "attach-bar" });
  const renderMounts = () => {
    mounts.replaceChildren();
    for (const f of t.folders) {
      mounts.append(el("span", { class: "pill", title: f.path },
        el("span", { html: icon("folder", 12) }),
        el("span", { class: "pname", text: baseName(f.path) }),
        el("button", {
          class: "mode" + (f.mode === "write" ? " write" : ""),
          text: f.mode === "write" ? "write" : "view",
          onclick: () => { f.mode = f.mode === "write" ? "view" : "write"; renderMounts(); saveSession(); },
        }),
        el("button", { text: "×", onclick: () => { t.folders = t.folders.filter((x) => x !== f); renderMounts(); saveSession(); } })));
    }
    mounts.append(el("button", {
      class: "btn btn-sm", text: "+ mount folder…",
      onclick: async () => {
        const d = await Api.get("pick_folder");
        if (!d.path) return;
        if (!t.folders.some((f) => f.path === d.path)) {
          t.folders.push({ path: d.path, mode: "view" });
          renderMounts();
          saveSession();
        }
      },
    }));
  };
  renderMounts();

  const netChk = el("input", { type: "checkbox" });
  netChk.checked = !!t.network;
  netChk.addEventListener("change", () => { t.network = netChk.checked; saveSession(); });

  const envSel = el("select", { class: "compose-model term-sel" });
  envSel.append(el("option", { value: "", text: "no environment" }));
  Api.call("envs_list").then((r) => {
    if (!r.ok) return;
    for (const n of r.data.envs || []) {
      envSel.append(el("option", { value: n, text: n }));
    }
    envSel.value = t.env || "";
  });
  envSel.addEventListener("change", () => { t.env = envSel.value; saveSession(); });

  return el("div", { class: "term-setup" },
    el("h2", { text: "New terminal" }),
    el("div", { class: "ts-row" }, el("label", { text: "Container" }), contSel),
    el("div", { class: "ts-row" }, el("label", { text: "Mounts" }), mounts),
    el("div", { class: "ts-row" }, el("label", { text: "Network" }),
      el("label", { class: "chk" }, netChk, "allow network access (off = --network=none)")),
    el("div", { class: "ts-row" }, el("label", { text: "Environment" }), envSel),
    el("div", { class: "ts-row" }, el("span"),
      el("button", {
        class: "btn btn-acc", text: "Start shell",
        onclick: () => { t.started = true; saveSession(); renderTermTab(id); },
      })),
    el("p", { class: "ts-hint", text: "Folders mount at /mnt/<name> (view = read-only). The shell runs as an unprivileged user in the container you pick." }));
}

/* change a LIVE terminal's setup (container / network). The running
 * container is killed and a fresh shell starts with the new setup —
 * the scrollback buffer is kept (the view keeps feeding into the same
 * screen, and the backend replays carried history above the new shell). */
async function switchTermSetup(id, changes, onCancel) {
  const t = termState(id);
  const v = st._termViews?.[id];
  if (!v || v.opening) { onCancel?.(); return; }
  let procs = [];
  if (t.running) {
    try {
      const res = await Api.call("term_procs", id);
      procs = res.ok ? res.data.procs || [] : [];
    } catch (e) { /* backend gone */ }
  }
  const go = () => {
    Object.assign(t, changes);
    saveSession();
    const what = "container" in changes
      ? "switching to container " + changes.container
      : "env" in changes
        ? (changes.env ? "loading environment " + changes.env
                       : "clearing the environment")
        : changes.network ? "enabling network" : "disabling network";
    v.screen.feed("\r\n\x1b[2m[loom] " + what + " — restarting shell…\x1b[0m\r\n");
    v.opening = false;
    v.exited = false;
    openTermSession(id, v);
    renderTermTab(id);   // the header reflects the new setup
  };
  if (!procs.length) { go(); return; }
  modal("Programs still running",
    [el("p", { text: "Switching restarts the shell and kills:" }),
     el("div", { class: "tool-args", text: procs.join("\n") })],
    [{ label: "Keep running", fn: () => { onCancel?.(); } },
     { label: "Switch anyway", cls: "btn-danger", fn: go }],
    { id: "term-switch:" + id });
}

function termHeaderBar(id) {
  const t = termState(id);
  // container: a live selector — switching restarts into the new image
  const defs = (st.config?.containers?.definitions || []).map((d) => d.name);
  if (!defs.includes(t.container)) defs.unshift(t.container);
  const contSel = el("select", {
    class: "term-sel term-head-sel",
    title: "Switch container — restarts the shell; scrollback is kept",
  });
  for (const name of defs) contSel.append(el("option", { value: name, text: name }));
  contSel.value = t.container;
  contSel.addEventListener("change", () => {
    if (contSel.value === t.container) return;
    switchTermSetup(id, { container: contSel.value },
      () => { contSel.value = t.container; });
  });
  const netBtn = el("button", {
    class: "pill term-net" + (t.network ? " on" : ""),
    text: t.network ? "network" : "no network",
    title: (t.network
      ? "The container CAN reach the network — click to turn off"
      : "The container runs with --network=none — click to allow network")
      + " (restarts the shell; scrollback is kept)",
  });
  netBtn.addEventListener("click", () => {
    switchTermSetup(id, { network: !t.network });
  });
  // environment: another live selector, same restart semantics
  const envSel = el("select", {
    class: "term-sel term-head-sel",
    title: "Switch environment — restarts the shell; scrollback is kept",
  });
  envSel.append(el("option", { value: "", text: "no env" }));
  Api.call("envs_list").then((r) => {
    if (!r.ok) return;
    for (const n of r.data.envs || []) {
      envSel.append(el("option", { value: n, text: n }));
    }
    envSel.value = t.env || "";
  });
  envSel.value = t.env || "";
  envSel.addEventListener("change", () => {
    if (envSel.value === (t.env || "")) return;
    switchTermSetup(id, { env: envSel.value },
      () => { envSel.value = t.env || ""; });
  });
  return el("div", { class: "term-head" },
    el("span", { class: "pill", title: "container" },
      el("span", { html: icon("servers", 12) })),
    contSel,
    ...t.folders.map((f) => el("span", { class: "pill", title: f.path },
      el("span", { html: icon("folder", 12) }),
      el("span", { class: "pname", text: "/mnt/" + baseName(f.path) }),
      el("span", { class: "mode" + (f.mode === "write" ? " write" : ""), text: f.mode }))),
    netBtn,
    el("span", { html: icon("key", 12), class: "term-envkey" }),
    envSel,
    el("span", { class: "spacer" }),
    el("button", {
      class: "btn btn-sm", text: "Restart",
      onclick: async () => {
        const v = st._termViews?.[id];
        if (!v || v.opening) return;
        // same busy guard as closing: restarting kills running programs
        let procs = [];
        try {
          const res = await Api.call("term_procs", id);
          procs = res.ok ? res.data.procs || [] : [];
        } catch (e) { /* backend gone */ }
        const go = () => {
          v.screen.feed("\r\n\x1b[2m[loom] restarting shell…\x1b[0m\r\n");
          v.opening = false;
          openTermSession(id, v);
        };
        if (!procs.length) { go(); return; }
        modal("Programs still running",
          [el("p", { text: "Restarting kills:" }),
           el("div", { class: "tool-args", text: procs.join("\n") })],
          [{ label: "Keep running" },
           { label: "Restart anyway", cls: "btn-danger", fn: go }],
          { id: "term-restart:" + id });
      },
    }));
}

/* ---------- the live view ---------- */
st._termViews = st._termViews || {};

function ensureTermView(id) {
  let v = st._termViews[id];
  if (v) return v;
  const sbEl = el("div", { class: "term-sb" });
  const gridEl = el("div", { class: "term-grid" });
  const scroller = el("div", { class: "term-scroller" }, sbEl, gridEl);
  const promptsEl = el("div", { class: "term-prompts" });
  const wrap = el("div", { class: "term-emu", tabindex: "0" }, scroller, promptsEl);

  v = { id, el: wrap, scroller, sbEl, gridEl, promptsEl,
        prompts: [], buf: "", flushT: 0, raf: 0, opening: false, exited: false,
        stick: true, cols: 120, rows: 32 };

  v.screen = new TermScreen(120, 32, {
    reply: (data) => Api.call("term_write", id, data),
    refresh: (hard) => scheduleTermPaint(v, hard),
    bell: () => { wrap.classList.remove("bell"); void wrap.offsetWidth; wrap.classList.add("bell"); },
    title: (txt) => { termState(id).title = txt; renderTabs(); saveSession(); },
    ask: (feature, apply) => {
      const open = v.prompts.find((p) => p.feature === feature);
      if (open) { open.applies.push(apply); return; }
      v.prompts.push({ feature, applies: [apply] });
      paintTermPrompts(v);
    },
  });

  const send = (data) => {
    const t = termState(id);
    if (!t.started) return;
    v.buf += data;
    if (!v.flushT) v.flushT = setTimeout(() => {
      const out = v.buf; v.buf = ""; v.flushT = 0;
      Api.call("term_write", id, out).then((res) => {
        if (res && res.ok) return;
        // dead PTY: queue the keystrokes, reconnect, replay on ready
        v.pendingInput = ((v.pendingInput || "") + out).slice(-4096);
        if (v._deadNotified) return;
        v._deadNotified = true;
        t.running = false;
        v.opening = false;
        termAttachOrOpen(id, v);
      });
    }, 8);
  };
  v.send = send;

  wrap.addEventListener("keydown", (e) => {
    if (v.prompts.length && e.ctrlKey && !e.altKey && !e.metaKey) {
      const k = e.key.toLowerCase();
      if (k === "y" || k === "n") {
        e.preventDefault(); e.stopPropagation();
        answerTermPrompt(v, v.prompts[0], k === "y" ? "allow" : "deny", false);
        return;
      }
    }
    if (e.ctrlKey && e.shiftKey) {
      const k = e.key.toLowerCase();
      if (k === "c") {
        const sel = String(getSelection() || "");
        if (sel) { copyText(sel); e.preventDefault(); }
        return;
      }
      if (k === "v") {
        e.preventDefault();
        navigator.clipboard?.readText().then((txt) => txt && pasteToTerm(v, txt));
        return;
      }
      return;
    }
    if (v.exited && e.key === "Enter") {
      e.preventDefault();
      v.screen.feed("\r\n\x1b[2m[loom] restarting shell…\x1b[0m\r\n");
      openTermSession(id, v);
      return;
    }
    if (e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey
        && (e.key === "PageUp" || e.key === "PageDown")) {
      e.preventDefault();
      const page = Math.max((v.cellH || 17) * 3, scroller.clientHeight - (v.cellH || 17) * 2);
      scroller.scrollTop += e.key === "PageUp" ? -page : page;
      return;
    }
    const seq = termKeySeq(e, v.screen);
    if (seq !== null) {
      e.preventDefault();
      const sel = getSelection();
      if (sel && !sel.isCollapsed && wrap.contains(sel.anchorNode)) sel.removeAllRanges();
      if (e.key === "Enter") {
        v.stick = true;
        scroller.scrollTop = scroller.scrollHeight;
      }
      send(seq);
    }
  });

  wrap.addEventListener("paste", (e) => {
    e.preventDefault();
    const txt = e.clipboardData?.getData("text/plain");
    if (txt) pasteToTerm(v, txt);
  });
  wrap.addEventListener("focus", () => { if (v.screen.focusEvents) send("\x1b[I"); paintTermGrid(v); });
  wrap.addEventListener("blur", () => { if (v.screen.focusEvents) send("\x1b[O"); paintTermGrid(v); });

  const mouseSeq = (e, kind) => {
    const s = v.screen;
    if (!s.mouseMode) return null;
    if (e.shiftKey) return null;
    const rect = gridEl.getBoundingClientRect();
    const col = Math.max(1, Math.min(s.cols, Math.floor((e.clientX - rect.left) / v.cellW) + 1));
    const row = Math.floor((e.clientY - rect.top) / v.cellH) + 1;
    if (row < 1 || row > s.rows) return null;
    let btn = kind === "wheel" ? (e.deltaY < 0 ? 64 : 65)
      : e.button === 1 ? 1 : e.button === 2 ? 2 : 0;
    if (kind === "move") btn += 32;
    if (e.ctrlKey) btn += 16;
    if (s.mouseSGR) {
      const final = kind === "up" ? "m" : "M";
      return `\x1b[<${btn};${col};${row}${final}`;
    }
    if (kind === "up") btn = 3;
    const enc = (n) => String.fromCharCode(Math.min(255, 32 + n));
    return "\x1b[M" + enc(btn) + enc(Math.min(223, col)) + enc(Math.min(223, row));
  };
  gridEl.addEventListener("mousedown", (e) => {
    wrap.focus();
    const s = mouseSeq(e, "down");
    if (s) { e.preventDefault(); send(s); v._mouseDown = true; }
    else if (e.button === 0) v._selecting = true;
  });
  gridEl.addEventListener("mouseup", (e) => {
    if (!v._mouseDown) return;
    v._mouseDown = false;
    const s = mouseSeq(e, "up");
    if (s) { e.preventDefault(); send(s); }
  });
  gridEl.addEventListener("mousemove", (e) => {
    const m = v.screen.mouseMode;
    if (m !== 1003 && !(m === 1002 && v._mouseDown)) return;
    const now = performance.now();
    if (now - (v._lastMove || 0) < 33) return;
    v._lastMove = now;
    const s = mouseSeq(e, "move");
    if (s) send(s);
  });
  scroller.addEventListener("wheel", (e) => {
    const s = v.screen;
    if (s.mouseMode && !e.shiftKey) {
      const seq = mouseSeq(e, "wheel");
      if (seq) { e.preventDefault(); send(seq); return; }
    }
    if (s.alt) {
      e.preventDefault();
      const key = e.deltaY < 0 ? (s.appCursor ? "\x1bOA" : "\x1b[A") : (s.appCursor ? "\x1bOB" : "\x1b[B");
      send(key.repeat(3));
    }
  }, { passive: false });
  scroller.addEventListener("scroll", () => {
    v.stick = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 24;
  });
  wrap.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    if (v.screen.mouseMode && !e.shiftKey) return;
    const selText = String(getSelection() || "");
    ctxMenu(e.clientX, e.clientY, [
      { label: "Copy  (Ctrl+Shift+C)", fn: () => { if (selText) copyText(selText); else toast("Nothing selected", "warn"); v.el.focus(); } },
      { label: "Paste  (Ctrl+Shift+V)", fn: () => navigator.clipboard?.readText().then((txt) => { if (txt) pasteToTerm(v, txt); v.el.focus(); }) },
      "-",
      { label: "Select all", fn: () => {
          const r = document.createRange();
          r.selectNodeContents(v.scroller);
          const s2 = getSelection();
          s2.removeAllRanges();
          s2.addRange(r);
        } },
    ]);
  });

  v.measure = () => {
    if (scroller.clientWidth < 60 || scroller.clientHeight < 40) return;
    const probeSpan = el("span", { text: "M".repeat(20) });
    const probe = el("div", { class: "trow", style: "position:absolute;visibility:hidden;white-space:pre" }, probeSpan);
    gridEl.appendChild(probe);
    const rw = probeSpan.getBoundingClientRect().width;
    const rh = probe.getBoundingClientRect().height;
    probe.remove();
    v.cellW = (rw / 20) || 8;
    v.cellH = rh || 17;
    const padX = 12, padY = 8;
    const cols = Math.max(20, Math.floor((scroller.clientWidth - padX) / v.cellW));
    const rows = Math.max(5, Math.floor((scroller.clientHeight - padY) / v.cellH));
    gridEl.style.minHeight = (scroller.clientHeight - padY) + "px";
    if (cols !== v.cols || rows !== v.rows) {
      v.cols = cols; v.rows = rows;
      v.screen.resize(cols, rows);
      if (termState(id).started) Api.call("term_resize", id, cols, rows);
    }
  };
  v.ro = new ResizeObserver(() => { if (wrap.isConnected) v.measure(); });
  v.ro.observe(scroller);

  st._termViews[id] = v;
  return v;
}

function pasteToTerm(v, text) {
  const t = String(text).replace(/\r\n/g, "\r").replace(/\n/g, "\r");
  v.send(v.screen.bracketedPaste ? "\x1b[200~" + t + "\x1b[201~" : t);
}

function termKeySeq(e, s) {
  const app = s.appCursor;
  const csi = (x) => "\x1b[" + x;
  const ss3 = (x) => "\x1b" + (app ? "O" : "[") + x;
  const key = e.key;
  if (e.metaKey) return null;
  const mods = 1 + (e.shiftKey ? 1 : 0) + (e.altKey ? 2 : 0) + (e.ctrlKey ? 4 : 0);
  const modded = (base, final) => mods > 1 ? csi(base + ";" + mods + final) : ss3(final);
  switch (key) {
    case "Enter": return e.altKey ? "\x1b\r" : "\r";
    case "Backspace": return e.altKey ? "\x1b\x7f" : (e.ctrlKey ? "\x08" : "\x7f");
    case "Tab": return e.shiftKey ? csi("Z") : "\t";
    case "Escape": return "\x1b";
    case "ArrowUp": return modded("1", "A");
    case "ArrowDown": return modded("1", "B");
    case "ArrowRight": return modded("1", "C");
    case "ArrowLeft": return modded("1", "D");
    case "Home": return modded("1", "H");
    case "End": return modded("1", "F");
    case "PageUp": return csi(mods > 1 ? "5;" + mods + "~" : "5~");
    case "PageDown": return csi(mods > 1 ? "6;" + mods + "~" : "6~");
    case "Insert": return csi("2~");
    case "Delete": return csi(mods > 1 ? "3;" + mods + "~" : "3~");
    case "F1": case "F2": case "F3": case "F4": {
      const f = { F1: "P", F2: "Q", F3: "R", F4: "S" }[key];
      return mods > 1 ? csi("1;" + mods + f) : "\x1bO" + f;
    }
    case "F5": return csi("15~"); case "F6": return csi("17~");
    case "F7": return csi("18~"); case "F8": return csi("19~");
    case "F9": return csi("20~"); case "F10": return csi("21~");
    case "F11": return csi("23~"); case "F12": return csi("24~");
  }
  if (key.length === 1) {
    if (e.ctrlKey) {
      const c = key.toLowerCase();
      if (c >= "a" && c <= "z") return String.fromCharCode(c.charCodeAt(0) - 96);
      const extra = { " ": "\x00", "[": "\x1b", "\\": "\x1c", "]": "\x1d", "^": "\x1e", "_": "\x1f", "/": "\x1f" };
      if (extra[c] !== undefined) return extra[c];
      return null;
    }
    return e.altKey ? "\x1b" + key : key;
  }
  return null;
}

/* ---------- painting ---------- */

function scheduleTermPaint(v, hard) {
  if (hard) { v.sbEl.replaceChildren(); }
  if (v.raf) return;
  v.raf = requestAnimationFrame(() => { v.raf = 0; paintTermGrid(v); });
}

function paintTermGrid(v) {
  const s = v.screen;
  if (s.sbNew.length) {
    const FRAME_SB_MAX = 500;
    if (s.sbNew.length > FRAME_SB_MAX) {
      v.sbEl.replaceChildren();
      s.sbNew = s.sbNew.slice(-FRAME_SB_MAX);
    }
    const frag = document.createDocumentFragment();
    for (const row of s.sbNew) {
      const d = document.createElement("div");
      d.className = "trow";
      d.innerHTML = _termRowHtml(row, row.length, -1, "block");
      frag.appendChild(d);
    }
    s.sbNew = [];
    v.sbEl.appendChild(frag);
    while (v.sbEl.childElementCount > TERM_SB_MAX) v.sbEl.firstElementChild.remove();
  }
  // active selections survive repaints: skip the grid rebuild while one
  // exists; catch up on the paint after it clears
  const sel = getSelection();
  const selHere = (sel && !sel.isCollapsed && v.el.contains(sel.anchorNode)) || v._selecting;
  if (selHere) { v._paintSkipped = true; return; }
  const focused = document.activeElement === v.el;
  const curR = s.cursorVisible && !v.exited ? s.cur.r : -1;
  const shape = focused ? s.cursorShape : "unfocused";
  let html = "";
  for (let r = 0; r < s.rows; r++) {
    html += '<div class="trow">' + _termRowHtml(s.grid[r], s.cols, r === curR ? s.cur.c : -1, shape) + "</div>";
  }
  v.gridEl.innerHTML = html;
  if (v.stick) v.scroller.scrollTop = v.scroller.scrollHeight;
}

function paintTermPrompts(v) {
  v.promptsEl.replaceChildren(...v.prompts.map((p, i) => {
    const f = TERM_FEATURES.find((x) => x.id === p.feature) || { name: p.feature, desc: "" };
    return el("div", { class: "term-ask" + (i === 0 ? " first" : "") },
      el("div", { class: "ta-title" },
        el("b", { text: "Program requests: " + f.name }),
        i === 0 ? el("span", { class: "ta-keys", text: "Ctrl+Y allow · Ctrl+N deny" }) : null),
      el("div", { class: "ta-desc", text: f.desc }),
      el("div", { class: "ta-actions" },
        el("button", { class: "btn btn-sm btn-acc", text: "Allow", onclick: () => answerTermPrompt(v, p, "allow", false) }),
        el("button", { class: "btn btn-sm", text: "Always", onclick: () => answerTermPrompt(v, p, "allow", true) }),
        el("button", { class: "btn btn-sm", text: "Deny", onclick: () => answerTermPrompt(v, p, "deny", false) }),
        el("button", { class: "btn btn-sm", text: "Never", onclick: () => answerTermPrompt(v, p, "deny", true) })));
  }));
}

function answerTermPrompt(v, p, decision, always) {
  const i = v.prompts.indexOf(p);
  if (i < 0) return;
  v.prompts.splice(i, 1);
  if (always) termFeatureRemember(p.feature, decision);
  v.screen.answerFeature(p.feature, decision, p.applies);
  paintTermPrompts(v);
  v.el.focus();
}

/* ---------- session lifecycle ---------- */

function openTermSession(id, v) {
  if (v.opening) return;
  v.opening = true;
  v.exited = false;
  const t = termState(id);
  Api.call("term_open", {
    tabId: id, container: t.container, folders: t.folders,
    network: !!t.network, env: t.env || "", cols: v.cols, rows: v.rows,
  });
}

async function termAttachOrOpen(id, v) {
  if (v.opening) return;
  v.opening = true;
  let attached = false;
  try {
    const res = await Api.call("term_attach", id, v.cols, v.rows);
    attached = !!(res.ok && res.data.attached);
  } catch (e) { /* backend unreachable */ }
  if (attached) {
    setTimeout(() => { v.opening = false; }, 15000);
    return;
  }
  v.opening = false;
  openTermSession(id, v);
}

/* Resync after the page was hidden (close-to-tray, minimize): rAF stops
 * firing in hidden windows, so pending paints stall — on visibility/focus
 * every live terminal re-measures and repaints, and the ACTIVE terminal
 * reattaches if its session was lost while away. */
function resyncTerms() {
  for (const id in st._termViews) {
    const v = st._termViews[id];
    if (v.dead || !v.el.isConnected) continue;
    v.measure?.();
    scheduleTermPaint(v);
  }
  const active = tabById(st.activeTab);
  if (active?.type === "term") {
    const v = st._termViews[active.chatId];
    if (v && !v.dead && !v.opening && !v.exited
        && !st.terms[active.chatId]?.running) {
      termAttachOrOpen(active.chatId, v);
    }
  }
  if (active?.type === "chat") queueThreadRedraw(active.chatId);
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) resyncTerms();
});
window.addEventListener("focus", resyncTerms);

/* selection lifecycle (module-level) */
document.addEventListener("mouseup", () => {
  for (const id in st._termViews) {
    const v = st._termViews[id];
    if (v._selecting) { v._selecting = false; if (v._paintSkipped) { v._paintSkipped = false; scheduleTermPaint(v); } }
  }
});
document.addEventListener("selectionchange", () => {
  const sel = getSelection();
  for (const id in st._termViews) {
    const v = st._termViews[id];
    if (!v._paintSkipped || v._selecting) continue;
    if (!sel || sel.isCollapsed || !v.el.contains(sel.anchorNode)) {
      v._paintSkipped = false;
      scheduleTermPaint(v);
    }
  }
});

/* backend term events */
function onTermEvent(ev) {
  const v = st._termViews[ev.sid];
  const t = st.terms[ev.sid];
  if (!v || v.dead) return;
  if (ev.kind === "data") v.screen.feed(ev.data || "");
  else if (ev.kind === "line") v.screen.feed("\x1b[2m" + (ev.text || "") + "\x1b[0m\r\n");
  else if (ev.kind === "snapshot") {
    if (ev.data) v.screen.reset();
    else v.screen.archive();
    v.screen.sbNew = v.screen.sb.slice();
    v.screen.paused = false;
    v.screen._pend = "";
    v.prompts = [];
    paintTermPrompts(v);
    scheduleTermPaint(v, true);
    if (ev.data) v.screen.feed(ev.data);
  } else if (ev.kind === "ready") {
    v.opening = false; v.exited = false; v._deadNotified = false;
    if (t) t.running = true;
    if (v.el.isConnected) v.measure?.();
    Api.call("term_resize", ev.sid, v.cols, v.rows);
    if (v.pendingInput) {
      const replay = v.pendingInput;
      v.pendingInput = "";
      Api.call("term_write", ev.sid, replay);
    }
    renderTabs();
    if (st.activeTab === "term:" + ev.sid) v.el.focus();
  } else if (ev.kind === "exit") {
    v.opening = false; v.exited = true;
    if (t) t.running = false;
    v.screen.feed(`\r\n\x1b[2m[session ended — exit ${ev.code ?? "?"} · Enter restarts the shell]\x1b[0m\r\n`);
    renderTabs();
  } else if (ev.kind === "error") {
    v.opening = false; v.exited = true;
    if (t) t.running = false;
    v.screen.feed(`\r\n\x1b[31m[terminal error] ${ev.detail || "unknown"}\x1b[0m\r\n`);
    renderTabs();
  }
}
