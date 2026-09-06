/* mdedit.js - the decorated-source editor, lifted from databox's personal
 * cloud markdown editor and stripped of its collaboration layer (no ops,
 * no SSE, no presence - the file on disk is the document).
 *
 * It is a SOURCE view: the `#` markers, fences, and emphasis asterisks
 * stay visible, and the styling happens around them - neon per-depth
 * heading blocks, syntax-highlighted fenced code with copy buttons,
 * dimmed markers. Two modes:
 *   'md'    markdown decoration + per-fence code highlighting
 *   'code'  the whole file highlighted as one language (yaml, sh, ...)
 *
 * SAFETY: every rendered fragment is built from TEXT NODES and class-only
 * spans - no innerHTML of file content anywhere.
 *
 * Caret rule (hard-won upstream): re-rendering the line under the caret
 * moves the caret - every re-render captures the character offset first
 * and restores it after; decoration waits out IME composition and any
 * live multi-line selection.
 *
 * API: new LoomEditor(host, {text, mode, lang, readOnly, onDirty, onSave})
 *      .getValue()  .setText(text)  .isDirty()  .markClean()  .destroy()
 */
"use strict";

/* ---------- syntax highlighter ---------- */
const ED_KW = {
  js: 'const let var function return if else for while do switch case break continue new class extends super import export from default try catch finally throw typeof instanceof in of delete void yield async await static get set this null undefined true false',
  go: 'package import func return if else for range switch case break continue type struct interface map chan go defer select var const goto fallthrough nil true false make new len cap append copy delete panic recover error string int int8 int16 int32 int64 uint uint8 uint16 uint32 uint64 float32 float64 bool byte rune any',
  py: 'def return if elif else for while in not and or is None True False class import from as with try except finally raise lambda yield global nonlocal pass break continue del assert async await match case self',
  rs: 'fn let mut return if else for while loop match impl trait struct enum pub use mod crate self super where as in ref move async await dyn box const static type unsafe true false Some None Ok Err',
  c: 'int char long short float double void unsigned signed struct union enum typedef static extern const volatile return if else for while do switch case break continue goto sizeof new delete class public private protected virtual template typename namespace using this nullptr true false NULL final override import package boolean String var val fun when object interface null',
  sh: 'if then else elif fi for while do done case esac function in echo exit return local export set unset shift source true false read cd test',
  sql: 'select from where insert into values update set delete create table index view drop alter add join left right inner outer on as and or not null primary key foreign references group by order having limit offset union distinct count sum avg min max between like in exists case when then else end',
  css: '',
  yaml: 'true false null yes no on off',
  json: 'true false null',
  html: '',
  dockerfile: 'FROM RUN CMD ENTRYPOINT COPY ADD ENV ARG WORKDIR USER EXPOSE VOLUME LABEL SHELL STOPSIGNAL HEALTHCHECK ONBUILD MAINTAINER AS',
};
const ED_ALIAS = {
  javascript: 'js', ts: 'js', typescript: 'js', jsx: 'js', tsx: 'js', node: 'js',
  golang: 'go', python: 'py', python3: 'py', rust: 'rs',
  'c++': 'c', cpp: 'c', h: 'c', hpp: 'c', java: 'c', kotlin: 'c', cs: 'c', csharp: 'c',
  bash: 'sh', shell: 'sh', zsh: 'sh', console: 'sh',
  yml: 'yaml', xml: 'html', htm: 'html', markdown: '', md: '',
  containerfile: 'dockerfile',
};
const ED_LINE_COMMENT = { js: '//', go: '//', rs: '//', c: '//', py: '#', sh: '#', yaml: '#', sql: '--', dockerfile: '#' };
const ED_BLOCK_COMMENT = { js: ['/*', '*/'], go: ['/*', '*/'], rs: ['/*', '*/'], c: ['/*', '*/'], css: ['/*', '*/'], html: ['<!--', '-->'] };

function edLangFor(tag) {
  const t = String(tag || '').toLowerCase();
  const l = ED_ALIAS[t] !== undefined ? ED_ALIAS[t] : t;
  return ED_KW[l] !== undefined ? l : '';
}
const edKwSets = {};
for (const [l, words] of Object.entries(ED_KW)) edKwSets[l] = new Set(words.split(' ').filter(Boolean));

const YAML_KEY_RE = /^(\s*(?:-\s+)?)((?:"[^"]*"|'[^']*'|[^\s:#][^:#]*?))(:)(\s|$)/;

/* find-in-file: matches past this cap aren't tracked (the counter shows
 * "2000+") - keeps a 1-char query in a huge file from stalling the UI */
const ED_FIND_MAX = 2000;

/* scan lines for a plain-text query → [{line, start, end}], non-
 * overlapping, capped at `max`. Pure - node-testable. */
function edFindMatches(lines, query, caseSense, max = ED_FIND_MAX) {
  const out = [];
  if (!query) return out;
  const fold = caseSense ? (x) => x : (x) => x.toLowerCase();
  const needle = fold(query);
  for (let li = 0; li < lines.length && out.length < max; li++) {
    const hay = fold(String(lines[li]));
    let idx = hay.indexOf(needle);
    while (idx !== -1 && out.length < max) {
      out.push({ line: li, start: idx, end: idx + needle.length });
      idx = hay.indexOf(needle, idx + needle.length);
    }
  }
  return out;
}

/* Toggle line comments over a run of lines (Ctrl+/), VS Code semantics:
 * every non-blank line already commented → uncomment them all; otherwise
 * comment every non-blank line, inserting `marker + space` at the run's
 * minimum indentation. An all-blank run gets commented at column 0 (so a
 * lone empty line still toggles). Pure: returns {texts, deltas} - deltas
 * are per-line length changes, for caret restoration. */
function edToggleCommentLines(texts, marker) {
  const escMarker = marker.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const commentedRe = new RegExp('^(\\s*)' + escMarker + ' ?');
  let idxs = [];
  texts.forEach((t, i) => { if (t.trim() !== '') idxs.push(i); });
  if (!idxs.length) idxs = texts.map((_, i) => i);
  const allCommented = idxs.every((i) => commentedRe.test(texts[i]));
  const out = texts.slice();
  const deltas = texts.map(() => 0);
  if (allCommented) {
    for (const i of idxs) {
      out[i] = texts[i].replace(commentedRe, '$1');
      deltas[i] = out[i].length - texts[i].length;
    }
  } else {
    let ind = Infinity;
    for (const i of idxs) {
      if (texts[i].trim() === '') continue;   // blank lines don't set indent
      ind = Math.min(ind, /^\s*/.exec(texts[i])[0].length);
    }
    if (!isFinite(ind)) ind = 0;
    for (const i of idxs) {
      const t = texts[i];
      out[i] = t.slice(0, Math.min(ind, t.length)) + marker + ' '
        + t.slice(Math.min(ind, t.length));
      deltas[i] = out[i].length - t.length;
    }
  }
  return { texts: out, deltas };
}

/* highlight tokenizes one line; inBlock carries block-comment state between
 * lines. Returns { segs: [[text, cls|null]…], inBlock }. */
function edHighlight(text, lang, inBlock) {
  if (!lang) return { segs: [[text, null]], inBlock: false };
  const segs = [];
  const push = (s, c) => { if (s) segs.push([s, c]); };
  const lc = ED_LINE_COMMENT[lang];
  const bc = ED_BLOCK_COMMENT[lang];
  const kws = edKwSets[lang];
  let i = 0;
  if (inBlock) {
    const end = bc ? text.indexOf(bc[1]) : -1;
    if (end < 0) return { segs: [[text, 'tok-com']], inBlock: true };
    push(text.slice(0, end + bc[1].length), 'tok-com');
    i = end + bc[1].length;
  }
  // yaml: color the mapping key up front, then tokenize the remainder
  if (lang === 'yaml' && i === 0) {
    const m = YAML_KEY_RE.exec(text);
    if (m && !m[2].startsWith('#')) {
      push(m[1], null);
      push(m[2], 'tok-key');
      push(m[3], 'tok-key');
      i = m[1].length + m[2].length + m[3].length;
    }
  }
  let plain = '';
  const flush = () => { push(plain, null); plain = ''; };
  while (i < text.length) {
    const c = text[i];
    if (lc && text.startsWith(lc, i)) { flush(); push(text.slice(i), 'tok-com'); i = text.length; break; }
    if (bc && text.startsWith(bc[0], i)) {
      flush();
      const end = text.indexOf(bc[1], i + bc[0].length);
      if (end < 0) { push(text.slice(i), 'tok-com'); return { segs, inBlock: true }; }
      push(text.slice(i, end + bc[1].length), 'tok-com');
      i = end + bc[1].length;
      continue;
    }
    if (c === '"' || c === "'" || c === '`') {
      flush();
      let j = i + 1;
      while (j < text.length && text[j] !== c) j += text[j] === '\\' ? 2 : 1;
      push(text.slice(i, Math.min(j + 1, text.length)), 'tok-str');
      i = Math.min(j + 1, text.length);
      continue;
    }
    if (/[0-9]/.test(c) && !/[A-Za-z0-9_$]/.test(text[i - 1] || '')) {
      flush();
      let j = i;
      while (j < text.length && /[0-9a-fA-FxX._]/.test(text[j])) j++;
      push(text.slice(i, j), 'tok-num');
      i = j;
      continue;
    }
    if (/[A-Za-z_$]/.test(c)) {
      let j = i;
      while (j < text.length && /[A-Za-z0-9_$]/.test(text[j])) j++;
      const word = text.slice(i, j);
      if (kws.has(word) || ((lang === 'sql' || lang === 'dockerfile') && kws.has(word.toUpperCase())) || (lang === 'sql' && kws.has(word.toLowerCase()))) { flush(); push(word, 'tok-kw'); } else plain += word;
      i = j;
      continue;
    }
    plain += c;
    i++;
  }
  flush();
  return { segs, inBlock: false };
}

/* ---------- inline markdown decoration ---------- */
const ED_INLINE_RE = /(`+)([^`]+)\1|(\*\*|__)([^*_]+)\3|([*_])([^*_]+)\5|~~([^~]+)~~|(!?\[)([^\]]*)(\]\()([^)]*)(\))/g;
function edInlineSegs(text) {
  const segs = [];
  let last = 0;
  // emphasis wraps other inline syntax all the time (**[link](doc.md)**
  // - the docs do exactly this) - recurse into the inner text so a link
  // inside bold stays a clickable md-link, just also styled bold
  const nest = (inner, cls) => edInlineSegs(inner).map(([t, c, href]) =>
    [t, c ? c + ' ' + cls : cls, href]);
  for (const m of text.matchAll(ED_INLINE_RE)) {
    if (m.index > last) segs.push([text.slice(last, m.index), null]);
    if (m[1] !== undefined) {
      segs.push([m[1], 'md-mark'], [m[2], 'md-codei'], [m[1], 'md-mark']);
    } else if (m[3] !== undefined) {
      segs.push([m[3], 'md-mark'], ...nest(m[4], 'md-b'), [m[3], 'md-mark']);
    } else if (m[5] !== undefined) {
      segs.push([m[5], 'md-mark'], ...nest(m[6], 'md-i'), [m[5], 'md-mark']);
    } else if (m[7] !== undefined) {
      segs.push(['~~', 'md-mark'], ...nest(m[7], 'md-s'), ['~~', 'md-mark']);
    } else {
      // any non-empty target is clickable: http(s) opens externally,
      // anything else is a library-relative document link the host app
      // resolves (docs cross-reference each other this way)
      const target = (m[11] || '').trim() || null;
      segs.push([m[8], 'md-mark'], [m[9], 'md-link', target], [m[10], 'md-mark'], [m[11], 'md-url'], [m[12], 'md-mark']);
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) segs.push([text.slice(last), null]);
  return segs;
}

/* ---------- the editor ---------- */
class LoomEditor {
  constructor(host, opts) {
    this.host = host;
    this.opts = opts || {};
    this.mode = this.opts.mode === 'code' ? 'code' : 'md';
    this.lang = edLangFor(this.opts.lang || '');
    this.readOnly = !!this.opts.readOnly;
    this._dirty = false;
    this._composing = false;
    this._undo = [];
    this._redo = [];
    this._destroyed = false;

    host.classList.add('mdapp');
    host.replaceChildren();

    this.toolbar = el('div', { class: 'md-toolbar' });
    this.scroller = el('div', { class: 'md-scroll' });
    this.wrapper = el('div', { class: 'md-wrapper' });
    this.surface = el('div', { class: 'md-surface' + (this.mode === 'code' ? ' md-codefile' : '') });
    this.surface.contentEditable = this.readOnly ? 'false' : 'true';
    this.surface.spellcheck = this.mode === 'md';
    this.copyLayer = el('div', { class: 'md-copylayer' });
    this.wrapper.append(this.surface, this.copyLayer);
    this.scroller.append(this.wrapper);
    this.status = el('div', { class: 'md-status' });
    this.counts = el('span');
    this.saveState = el('span');
    this.status.append(this.counts, el('span', { class: 'spacer' }), this.saveState);
    this._buildFindbar();
    host.append(this.toolbar, this.findbar, this.scroller, this.status);

    this._buildToolbar();
    this._wire();
    this.setText(String(this.opts.text ?? ''));
    this._applyView();
  }

  /* ---- view prefs (per-user, localStorage) ---- */
  _pref(k, dflt) {
    const v = localStorage.getItem('loom-md-' + k);
    return v === null ? dflt : v !== '0';
  }
  /* wrap is OFF by default and remembered PER FILE TYPE - markdown, yaml,
   * python… each keeps its own toggle (the wizard's yaml editor shares
   * the yaml preference) */
  _wrapKey() {
    return 'wrap:' + (this.mode === 'md' ? 'md' : (this.lang || 'plain'));
  }
  _buildToolbar() {
    this.wrapOn = this._pref(this._wrapKey(), false);
    this.wrapW = Math.min(200, Math.max(40, +localStorage.getItem('loom-md-wrapw') || 88));
    this.neon = this._pref('neon', true);
    this.nums = this._pref('nums', this.mode === 'code');

    const tbtn = (label, title, fn) => {
      const b = el('button', { type: 'button', class: 'gtb', text: label, title });
      b.addEventListener('mousedown', (e) => e.preventDefault());
      b.addEventListener('click', fn);
      this.toolbar.append(b);
      return b;
    };
    if (!this.readOnly) {
      tbtn('Undo', 'Undo (Ctrl+Z)', () => { this._diff(); this._undoStep(); });
      tbtn('Redo', 'Redo (Ctrl+Y)', () => { this._diff(); this._redoStep(); });
      if (this.mode === 'md') {
        tbtn('B', 'Bold - wrap selection in ** (Ctrl+B)', () => this._wrapSel('**'));
        tbtn('I', 'Italic - wrap selection in * (Ctrl+I)', () => this._wrapSel('*'));
        tbtn('`', 'Inline code - wrap selection in backticks (Ctrl+E)', () => this._wrapSel('`'));
      }
    }
    tbtn('Find', 'Find in file (Ctrl+F)', () => this.openFind());
    this.wrapBtn = tbtn('Wrap', 'Toggle word wrapping (remembered per file type)', () => {
      this.wrapOn = !this.wrapOn;
      localStorage.setItem('loom-md-' + this._wrapKey(), this.wrapOn ? '1' : '0');
      this._applyView();
    });
    this.widthIn = el('input', { type: 'number', min: '40', max: '200', step: '4', class: 'md-widthin', title: 'Wrap width (characters)' });
    this.widthIn.value = String(this.wrapW);
    this.widthIn.addEventListener('change', () => {
      this.wrapW = Math.min(200, Math.max(40, +this.widthIn.value || 88));
      this.widthIn.value = String(this.wrapW);
      localStorage.setItem('loom-md-wrapw', String(this.wrapW));
      this._applyView();
    });
    this.toolbar.append(this.widthIn);
    if (this.mode === 'md') {
      this.neonBtn = tbtn('Neon', 'Toggle the highlighter heading style', () => {
        this.neon = !this.neon;
        localStorage.setItem('loom-md-neon', this.neon ? '1' : '0');
        this._applyView();
      });
    }
    this.numsBtn = tbtn('Lines', 'Toggle line numbers', () => {
      this.nums = !this.nums;
      localStorage.setItem('loom-md-nums', this.nums ? '1' : '0');
      this._applyView();
    });
  }
  _applyView() {
    this.surface.classList.toggle('md-nowrap', !this.wrapOn);
    this.surface.classList.toggle('md-neon', this.mode === 'md' && this.neon);
    this.surface.classList.toggle('md-nums', this.nums);
    this.surface.style.maxWidth = this.wrapOn ? this.wrapW + 'ch' : 'none';
    this.wrapBtn.textContent = 'Wrap: ' + (this.wrapOn ? 'on' : 'off');
    this.wrapBtn.classList.toggle('np-on', this.wrapOn);
    this.widthIn.disabled = !this.wrapOn;
    if (this.neonBtn) {
      this.neonBtn.textContent = 'Neon: ' + (this.neon ? 'on' : 'off');
      this.neonBtn.classList.toggle('np-on', this.neon);
    }
    this.numsBtn.textContent = 'Lines: ' + (this.nums ? 'on' : 'off');
    this.numsBtn.classList.toggle('np-on', this.nums);
    this._scheduleDecorate();
  }

  /* ---- public API ---- */
  getValue() {
    this._normalize();
    return [...this.surface.children].map((n) => n.textContent).join('\n');
  }
  setText(text) {
    this.surface.replaceChildren();
    for (const line of String(text ?? '').split('\n')) {
      this.surface.append(this._mkLine(line));
    }
    if (!this.surface.firstElementChild) this.surface.append(this._mkLine(''));
    this._model = this._snapshot();
    this._undo.length = 0;
    this._redo.length = 0;
    this._setDirty(false);
    this._updateCounts();
    this._decorate();
  }
  isDirty() { return this._dirty; }
  markClean() {
    this._model = this._snapshot();
    this._setDirty(false);
  }
  focus() { this.surface.focus(); }
  destroy() {
    this._destroyed = true;
    clearTimeout(this._decoTimer);
    clearTimeout(this._diffTimer);
    this._clearFindHl();
    this.host.replaceChildren();
    this.host.classList.remove('mdapp');
  }

  _setDirty(d) {
    if (this._dirty === d) return;
    this._dirty = d;
    this.saveState.textContent = this.readOnly ? 'read-only'
      : (d ? 'unsaved changes' : 'saved');
    if (this.opts.onDirty) this.opts.onDirty(d);
  }

  /* ---- caret bookkeeping ---- */
  _lineOf(node) {
    if (!node || !this.surface.contains(node)) return null;
    let e = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    while (e && e.parentElement !== this.surface) e = e.parentElement;
    return e;
  }
  _caretInfo() {
    const s = getSelection();
    if (!s || !s.rangeCount || !s.isCollapsed) return null;
    const e = this._lineOf(s.anchorNode);
    if (!e) return null;
    const r = s.getRangeAt(0).cloneRange();
    r.selectNodeContents(e);
    r.setEnd(s.getRangeAt(0).endContainer, s.getRangeAt(0).endOffset);
    return { el: e, offset: r.toString().length };
  }
  /* resolve an arbitrary selection endpoint into {el: line, offset} -
   * the un-collapsed sibling of _caretInfo */
  _pointOf(container, offset) {
    const e = this._lineOf(container);
    if (!e) return null;
    try {
      const r = document.createRange();
      r.selectNodeContents(e);
      r.setEnd(container, offset);
      return { el: e, offset: r.toString().length };
    } catch (err) {
      return null;
    }
  }
  _setCaret(e, offset) {
    let left = Math.max(0, offset);
    const walker = document.createTreeWalker(e, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    while (node) {
      if (left <= node.textContent.length) {
        const r = document.createRange();
        r.setStart(node, left);
        r.collapse(true);
        const s = getSelection();
        s.removeAllRanges();
        s.addRange(r);
        return;
      }
      left -= node.textContent.length;
      node = walker.nextNode();
    }
    const r = document.createRange();
    r.selectNodeContents(e);
    r.collapse(false);
    const s = getSelection();
    s.removeAllRanges();
    s.addRange(r);
  }

  /* ---- line rendering ---- */
  _mkLine(text) {
    const div = document.createElement('div');
    div.className = 'mdline';
    if (text) div.append(document.createTextNode(text));
    else div.append(document.createElement('br'));
    return div;
  }
  _renderPlain(e, text) {
    e.replaceChildren();
    if (text) e.append(document.createTextNode(text));
    else e.append(document.createElement('br'));
    e.dataset.sig = '';
  }
  _renderSegs(e, cls, segs) {
    e.className = 'mdline' + (cls ? ' ' + cls : '');
    e.replaceChildren();
    let any = false;
    for (const [text, c, href] of segs) {
      if (!text) continue;
      any = true;
      if (c) {
        const sp = document.createElement('span');
        sp.className = c;
        sp.textContent = text;
        if (href) { sp.dataset.href = href; sp.title = href + ' - Ctrl+Click to open'; }
        e.append(sp);
      } else {
        e.append(document.createTextNode(text));
      }
    }
    if (!any) e.append(document.createElement('br'));
  }

  _scheduleDecorate() {
    clearTimeout(this._decoTimer);
    this._decoTimer = setTimeout(() => this._decorate(), 120);
  }
  _decorate() {
    if (this._destroyed || this._composing) return;
    const s = getSelection();
    if (s && s.rangeCount && !s.isCollapsed && this.surface.contains(s.anchorNode)) {
      this._scheduleDecorate();  // never rewrite lines under a live selection
      return;
    }
    const caret = this._caretInfo();
    const fenceOpens = [];
    let fence = null;         // md mode: { lang, inBlock }
    let codeState = false;    // code mode: block-comment state
    for (const e of this.surface.children) {
      const text = e.textContent;
      let cls = '', segsFn = null, state = '';
      if (this.mode === 'code') {
        state = (codeState ? '1' : '0');
        const out = edHighlight(text, this.lang, codeState);
        codeState = out.inBlock;
        segsFn = () => out.segs;
      } else {
        const fm = /^(```+|~~~+)\s*(\S*)\s*$/.exec(text);
        if (fence) {
          if (fm && fm[2] === '') {
            cls = 'md-fence';
            segsFn = () => [[text, 'md-mark']];
            fence = null;
          } else {
            state = fence.lang + (fence.inBlock ? '1' : '0');
            const out = edHighlight(text, fence.lang, fence.inBlock);
            fence.inBlock = out.inBlock;
            cls = 'md-code';
            segsFn = () => out.segs;
          }
        } else if (fm) {
          fence = { lang: edLangFor(fm[2]), inBlock: false };
          fenceOpens.push(e);
          cls = 'md-fence';
          segsFn = () => [[fm[1], 'md-mark'], [text.slice(fm[1].length), 'md-lang']];
        } else if (/^\s*\|.*\|\s*$/.test(text) && text.trim().length > 1) {
          // table row: cells render as table-cells; consecutive rows form
          // a CSS anonymous table, so columns align while the line stays
          // plain editable source (textContent === the source line)
          const sep = /^\s*\|[\s\-:|]+\|\s*$/.test(text);
          cls = 'md-trow' + (sep ? ' md-tsep' : '');
          segsFn = 'table';
        } else {
          const h = /^(#{1,6})(\s+)(.*)$/.exec(text);
          if (h) {
            cls = 'md-h' + h[1].length;
            segsFn = () => [[h[1] + h[2], 'md-mark'], ...edInlineSegs(h[3])];
          } else if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(text)) {
            cls = 'md-hrline';
            segsFn = () => [[text, 'md-mark']];
          } else {
            const q = /^(>\s?)(.*)$/.exec(text);
            const li = q ? null : /^(\s*(?:[-*+]|\d{1,3}[.)])\s+)(.*)$/.exec(text);
            if (q) {
              cls = 'md-quote';
              segsFn = () => [[q[1], 'md-mark'], ...edInlineSegs(q[2])];
            } else if (li) {
              cls = 'md-li';
              segsFn = () => [[li[1], 'md-bullet'], ...edInlineSegs(li[2])];
            } else {
              segsFn = () => edInlineSegs(text);
            }
          }
        }
      }
      const sig = cls + '\x01' + state + '\x01' + text;
      if (e.dataset.sig !== sig) {
        const mine = caret && caret.el === e;
        if (segsFn === 'table') this._renderTableRow(e, cls, text);
        else this._renderSegs(e, cls, segsFn());
        e.dataset.sig = sig;
        if (mine) this._setCaret(e, caret.offset);
      }
    }
    this.copyLayer.replaceChildren();
    for (const e of fenceOpens) this._addCopyBtn(e);
    this._positionCopyBtns();
    // re-render pass killed the highlight ranges on rewritten lines -
    // rebuild them (and recount: the text may have changed)
    if (this._find && this._find.open) this._findApply(false);
  }

  /* one markdown table line → table-cells. Every source character stays
   * in the DOM text (pipes ride inside their cell, dimmed), so the caret
   * offset walker and the line diff are untouched. */
  _renderTableRow(rowEl, cls, text) {
    rowEl.className = 'mdline ' + cls;
    rowEl.replaceChildren();
    const first = text.indexOf('|');
    if (first > 0) rowEl.append(document.createTextNode(text.slice(0, first)));
    // chunks: each starts with its leading pipe; the trailing "|" (and
    // anything after it) forms the closing chunk
    const chunks = [];
    let cur = '';
    for (let i = first; i < text.length; i++) {
      const ch = text[i];
      if (ch === '|' && cur) { chunks.push(cur); cur = '|'; }
      else cur += ch;
    }
    if (cur) chunks.push(cur);
    chunks.forEach((chunk, idx) => {
      const cell = document.createElement('span');
      const body = chunk.slice(1);
      cell.className = 'md-tcell'
        + (idx === chunks.length - 1 && body.trim() === '' ? ' md-tclose' : '');
      const mark = document.createElement('span');
      mark.className = 'md-mark';
      mark.textContent = '|';
      cell.append(mark);
      for (const [t, c, href] of edInlineSegs(body)) {
        if (!t) continue;
        if (c) {
          const sp = document.createElement('span');
          sp.className = c;
          sp.textContent = t;
          if (href) { sp.dataset.href = href; sp.title = href + ' - Ctrl+Click to open'; }
          cell.append(sp);
        } else {
          cell.append(document.createTextNode(t));
        }
      }
      rowEl.append(cell);
    });
    if (!rowEl.firstChild) rowEl.append(document.createElement('br'));
  }

  /* ---- code copy buttons ---- */
  _fenceText(openEl) {
    const lines = [];
    for (let e = openEl.nextElementSibling; e; e = e.nextElementSibling) {
      if (e.classList.contains('md-fence')) break;
      lines.push(e.textContent);
    }
    return lines.join('\n');
  }
  _addCopyBtn(openEl) {
    const b = el('button', { type: 'button', class: 'md-copybtn', text: 'Copy', title: 'Copy this code block' });
    b.addEventListener('mousedown', (e) => e.preventDefault());
    b.addEventListener('click', async () => {
      const ok = await copyText(this._fenceText(openEl));
      b.textContent = ok ? '✓ copied' : 'copy failed';
      setTimeout(() => { b.textContent = 'Copy'; }, 1500);
    });
    b._fence = openEl;
    this.copyLayer.append(b);
  }
  _positionCopyBtns() {
    const visRight = this.scroller.scrollLeft + this.scroller.clientWidth - 24;
    for (const b of this.copyLayer.children) {
      const e = b._fence;
      if (!e || !e.isConnected) continue;
      const lineRight = e.offsetLeft + e.offsetWidth;
      b.style.top = (e.offsetTop + 2) + 'px';
      b.style.left = Math.max(0, Math.min(lineRight, visRight) - b.offsetWidth - 6) + 'px';
    }
  }

  /* ---- find in file (Ctrl+F) ----
   * Matches are highlighted via the CSS Custom Highlight API (no DOM
   * mutation - the decorator's sig cache and the caret walker never see
   * them; ranges are simply rebuilt after every decorate pass). Where the
   * API is missing the current match falls back to a plain selection. */
  _buildFindbar() {
    this._find = { open: false, matches: [], cur: -1, caseSense: false,
                   anchor: null };
    this.findIn = el('input', { type: 'text', class: 'md-findin',
      placeholder: 'Find in file…' });
    this.findCount = el('span', { class: 'md-findcount' });
    const fbtn = (label, title, fn) => {
      const b = el('button', { type: 'button', class: 'gtb', text: label, title });
      b.addEventListener('mousedown', (e) => e.preventDefault());
      b.addEventListener('click', fn);
      return b;
    };
    this.caseBtn = fbtn('Aa', 'Match case', () => {
      this._find.caseSense = !this._find.caseSense;
      this.caseBtn.classList.toggle('np-on', this._find.caseSense);
      this._findApply(true);
      this.findIn.focus();
    });
    this.findbar = el('div', { class: 'md-findbar' },
      this.findIn, this.caseBtn,
      fbtn('↑', 'Previous match (Shift+Enter)', () => this._findStep(-1)),
      fbtn('↓', 'Next match (Enter)', () => this._findStep(1)),
      this.findCount,
      el('span', { class: 'spacer' }),
      fbtn('✕', 'Close (Esc)', () => this.closeFind()));
    this.findbar.style.display = 'none';
    this.findIn.addEventListener('input', () => this._findApply(true));
    this.findIn.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === 'F3') {
        e.preventDefault();
        this._findStep(e.shiftKey ? -1 : 1);
      } else if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        this.closeFind();
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') {
        e.preventDefault();
        this.findIn.select();
      }
    });
  }
  openFind() {
    // anchor: search starts from where the caret was, like every editor
    const caret = this._caretInfo();
    this._find.anchor = caret
      ? { line: [...this.surface.children].indexOf(caret.el),
          offset: caret.offset }
      : null;
    const s = getSelection();
    if (s && s.rangeCount && !s.isCollapsed
        && this.surface.contains(s.anchorNode)) {
      const t = s.toString();
      if (t && t.length <= 200 && !t.includes('\n')) this.findIn.value = t;
    }
    this._find.open = true;
    this.findbar.style.display = 'flex';
    this.findIn.focus();
    this.findIn.select();
    this._findApply(true);
  }
  closeFind() {
    const f = this._find;
    if (!f.open) return;
    const m = f.matches[f.cur];
    const r = m ? this._matchRange(m) : null;   // resolve BEFORE teardown
    f.open = false;
    f.matches = [];
    f.cur = -1;
    this.findbar.style.display = 'none';
    this.findCount.textContent = '';
    this._clearFindHl();
    // preventScroll is the whole fix: a bare focus() scrolls the tall
    // surface into view, which reads as "jump to the top" and throws
    // away the position find just earned
    this.surface.focus({ preventScroll: true });
    if (m && !this.readOnly) {
      // leave the caret ON the match the user was looking at
      const line = this.surface.children[m.line];
      if (line) this._setCaret(line, m.start);
    }
    // and leave the VIEWPORT there too - ending up at the match is the
    // point of the feature
    if (r) this._scrollToRange(r);
  }
  _findApply(userAction) {
    const f = this._find;
    if (!f.open) return;
    const q = this.findIn.value;
    const prev = f.matches[f.cur] || null;
    f.matches = edFindMatches(this._snapshot(), q, f.caseSense);
    if (!f.matches.length) {
      f.cur = -1;
    } else if (userAction || !prev) {
      f.cur = this._findFrom(f.anchor);
    } else {
      // a decorate refresh mid-find: stay on (or nearest after) the match
      // the user was on
      f.cur = f.matches.findIndex((m) => m.line > prev.line
        || (m.line === prev.line && m.start >= prev.start));
      if (f.cur === -1) f.cur = f.matches.length - 1;
    }
    this._renderFindHl(userAction);
  }
  _findFrom(anchor) {
    if (!anchor) return 0;
    const i = this._find.matches.findIndex((m) => m.line > anchor.line
      || (m.line === anchor.line && m.start >= anchor.offset));
    return i === -1 ? 0 : i;
  }
  _findStep(dir) {
    const f = this._find;
    if (!f.matches.length) return;
    f.cur = (f.cur + dir + f.matches.length) % f.matches.length;
    this._renderFindHl(true);
  }
  _matchRange(m) {
    const line = this.surface.children[m.line];
    if (!line) return null;
    const r = document.createRange();
    const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
    let pos = 0, node, haveStart = false;
    while ((node = walker.nextNode())) {
      const len = node.textContent.length;
      if (!haveStart && m.start <= pos + len) {
        r.setStart(node, m.start - pos);
        haveStart = true;
      }
      if (haveStart && m.end <= pos + len) {
        r.setEnd(node, m.end - pos);
        return r;
      }
      pos += len;
    }
    return null;
  }
  _renderFindHl(scroll) {
    const f = this._find;
    const n = f.matches.length;
    this.findCount.textContent = !this.findIn.value ? ''
      : n ? `${f.cur + 1}/${n}${n >= ED_FIND_MAX ? '+' : ''}`
          : 'no matches';
    this.findCount.classList.toggle('md-findmiss',
      !!this.findIn.value && !n);
    const cur = f.cur >= 0 ? f.matches[f.cur] : null;
    if (!(window.Highlight && CSS.highlights)) {
      // no Custom Highlight API: show the current match as a selection
      if (scroll && cur) {
        const r = this._matchRange(cur);
        if (r) {
          const s = getSelection();
          s.removeAllRanges();
          s.addRange(r.cloneRange());
          this._scrollToRange(r);
        }
      }
      return;
    }
    const all = [];
    for (const m of f.matches) {
      if (m === cur) continue;
      const r = this._matchRange(m);
      if (r) all.push(r);
    }
    CSS.highlights.set('loom-find', new Highlight(...all));
    const curR = cur ? this._matchRange(cur) : null;
    if (curR) CSS.highlights.set('loom-find-cur', new Highlight(curR));
    else CSS.highlights.delete('loom-find-cur');
    if (scroll && curR) this._scrollToRange(curR);
  }
  _clearFindHl() {
    if (window.CSS && CSS.highlights) {
      CSS.highlights.delete('loom-find');
      CSS.highlights.delete('loom-find-cur');
    }
  }
  _scrollToRange(r) {
    const rect = r.getBoundingClientRect();
    const sr = this.scroller.getBoundingClientRect();
    if (rect.top < sr.top + 8 || rect.bottom > sr.bottom - 8) {
      this.scroller.scrollTop += rect.top - sr.top
        - this.scroller.clientHeight / 3;
    }
    if (rect.left < sr.left + 8 || rect.right > sr.right - 24) {
      this.scroller.scrollLeft += rect.left - sr.left
        - this.scroller.clientWidth / 3;
    }
  }

  /* ---- surface ↔ model ---- */
  _snapshot() { return [...this.surface.children].map((n) => n.textContent); }
  _renderAllFrom(lines) {
    this.surface.replaceChildren();
    for (const line of lines) this.surface.append(this._mkLine(line));
    if (!this.surface.firstElementChild) this.surface.append(this._mkLine(''));
    this._updateCounts();
    this._decorate();
  }

  /* normalize repairs what editing physics leave behind: stray nodes become
   * lines; embedded newlines / interior <br>s split lines. */
  _normalize() {
    const caret = this._caretInfo();
    for (const node of [...this.surface.childNodes]) {
      if (node.nodeType === Node.TEXT_NODE) {
        if (!node.textContent) { node.remove(); continue; }
        this.surface.insertBefore(this._mkLine(node.textContent), node);
        node.remove();
      } else if (node.nodeType !== Node.ELEMENT_NODE) {
        node.remove();
      } else if (node.tagName !== 'DIV') {
        node.replaceWith(this._mkLine(node.textContent || ''));
      }
    }
    for (const e of [...this.surface.children]) {
      const text = e.textContent;
      const brCount = e.querySelectorAll('br').length;
      if (!text.includes('\n') && (brCount === 0 || (brCount === 1 && text === ''))) continue;
      const flat = [];
      let cur = '';
      const walk = (n) => {
        for (const ch of n.childNodes) {
          if (ch.nodeType === Node.TEXT_NODE) cur += ch.textContent;
          else if (ch.nodeName === 'BR') { flat.push(cur); cur = ''; } else walk(ch);
        }
      };
      walk(e);
      flat.push(cur);
      let pieces = flat.flatMap((p) => p.split('\n'));
      if (pieces.length > 1 && pieces[pieces.length - 1] === '' && text !== '') pieces = pieces.slice(0, -1);
      if (pieces.length < 2) continue;
      const mine = caret && caret.el === e;
      const divs = pieces.map((p) => this._mkLine(p));
      e.replaceWith(divs[0]);
      let after = divs[0];
      for (let i = 1; i < divs.length; i++) { after.after(divs[i]); after = divs[i]; }
      if (mine) {
        let left = caret.offset;
        for (const d of divs) {
          const len = d.textContent.length;
          if (left <= len) { this._setCaret(d, left); break; }
          left -= len + 1;
        }
      }
    }
    if (!this.surface.firstElementChild) this.surface.append(this._mkLine(''));
  }

  _diff() {
    if (this.readOnly || this._destroyed) return;
    this._normalize();
    const now = this._snapshot();
    const before = this._model;
    if (now.length === before.length && now.every((t, i) => t === before[i])) {
      this._updateCounts();
      return;
    }
    this._undo.push(before);
    if (this._undo.length > 100) this._undo.shift();
    this._redo.length = 0;
    this._model = now;
    this._setDirty(true);
    this._updateCounts();
  }
  _undoStep() {
    const s = this._undo.pop();
    if (!s) return;
    this._redo.push(this._snapshot());
    this._model = s.slice();
    this._renderAllFrom(s);
    this._setDirty(true);
  }
  _redoStep() {
    const s = this._redo.pop();
    if (!s) return;
    this._undo.push(this._snapshot());
    this._model = s.slice();
    this._renderAllFrom(s);
    this._setDirty(true);
  }

  _updateCounts() {
    const lines = this.surface.children.length;
    let chars = 0, words = 0;
    for (const e of this.surface.children) {
      const t = e.textContent;
      chars += t.length + 1;
      words += (t.match(/\S+/g) || []).length;
    }
    this.counts.textContent = words + ' words · ' + Math.max(0, chars - 1)
      + ' characters · ' + lines + (lines === 1 ? ' line' : ' lines');
  }

  /* Ctrl+/ - toggle line comments on the caret line / selected lines.
   * Code mode only, and only for languages with a line-comment marker
   * (yaml, sh, py, js, dockerfile, ...). */
  _toggleComment() {
    const marker = this.mode === 'code' ? ED_LINE_COMMENT[this.lang] : null;
    if (!marker || this.readOnly) return;
    const s = getSelection();
    if (!s || !s.rangeCount || !this.surface.contains(s.anchorNode)) return;
    const a = this._lineOf(s.anchorNode);
    const f = this._lineOf(s.focusNode);
    if (!a || !f) return;
    const lines = [...this.surface.children];
    let ai = lines.indexOf(a), fi = lines.indexOf(f);
    if (ai < 0 || fi < 0) return;
    if (ai > fi) [ai, fi] = [fi, ai];
    const targets = lines.slice(ai, fi + 1);
    const collapsed = s.isCollapsed;
    const caret = collapsed ? this._caretInfo() : null;

    const { texts, deltas } = edToggleCommentLines(
      targets.map((e) => e.textContent), marker);
    targets.forEach((e, i) => {
      if (e.textContent !== texts[i]) this._renderPlain(e, texts[i]);
    });

    if (collapsed && caret) {
      const i = targets.indexOf(caret.el);
      this._setCaret(caret.el, Math.max(0, caret.offset + (deltas[i] || 0)));
    } else {
      // keep the whole line range selected so Ctrl+/ toggles back
      const r = document.createRange();
      const last = targets[targets.length - 1];
      r.setStart(targets[0], 0);
      r.setEnd(last, last.childNodes.length);
      s.removeAllRanges();
      s.addRange(r);
    }
    this._diff();               // records the undo step
    this._scheduleDecorate();
  }

  _wrapSel(marker) {
    const s = getSelection();
    if (!s || !s.rangeCount || !this.surface.contains(s.anchorNode)) return;
    if (this._lineOf(s.anchorNode) !== this._lineOf(s.focusNode)) return;
    const text = s.toString();
    this.surface.focus();
    document.execCommand('insertText', false, marker + text + marker);
    this._scheduleDiff();
    this._scheduleDecorate();
  }

  /* Enter is handled BY HAND: the browser's split clones the current line's
   * class onto the new one (a heading's neon block flashes tall then
   * collapses when decorate catches up). Splitting ourselves renders both
   * halves plain and decorates synchronously - no flash. */
  _splitAtCaret() {
    const s = getSelection();
    if (!s || !s.rangeCount || !this.surface.contains(s.anchorNode)) return;
    if (!s.isCollapsed) document.execCommand('delete');
    const caret = this._caretInfo();
    if (!caret) return;
    const text = caret.el.textContent;
    this._renderPlain(caret.el, text.slice(0, caret.offset));
    const div = this._mkLine(text.slice(caret.offset));
    caret.el.after(div);
    this._setCaret(div, 0);
    div.scrollIntoView({ block: 'nearest' });
    this._scheduleDiff();
    this._decorate();
  }

  _scheduleDiff() {
    clearTimeout(this._diffTimer);
    this._diffTimer = setTimeout(() => this._diff(), 350);
  }

  _wire() {
    this.surface.addEventListener('compositionstart', () => { this._composing = true; });
    this.surface.addEventListener('compositionend', () => { this._composing = false; this._scheduleDecorate(); });
    this.surface.addEventListener('input', () => { this._scheduleDiff(); this._scheduleDecorate(); });
    this.surface.addEventListener('blur', () => { this._diff(); this._scheduleDecorate(); });

    // paste: always plain text, split into real line divs. The whole
    // edit happens in OUR line model - execCommand('delete'/'insertText')
    // is never involved, because Chromium's editing engine normalizes
    // whitespace around the edit point (pasting over a word eats the
    // space before it).
    this.surface.addEventListener('paste', (e) => {
      if (this.readOnly) return;
      e.preventDefault();
      const text = e.clipboardData.getData('text/plain').replace(/\r\n?/g, '\n');
      const s = getSelection();
      if (!s || !s.rangeCount || !this.surface.contains(s.anchorNode)) return;
      const range = s.getRangeAt(0);
      let a = this._pointOf(range.startContainer, range.startOffset);
      let b = this._pointOf(range.endContainer, range.endOffset);
      if (!a || !b) {
        // an endpoint we can't map (surface-level selection) - the old
        // best-effort path
        if (!s.isCollapsed) document.execCommand('delete');
        document.execCommand('insertText', false, text);
        this._scheduleDiff();
        this._scheduleDecorate();
        return;
      }
      const lines = [...this.surface.children];
      let ai = lines.indexOf(a.el);
      let bi = lines.indexOf(b.el);
      if (ai > bi || (ai === bi && a.offset > b.offset)) {
        [a, b] = [b, a];
        [ai, bi] = [bi, ai];
      }
      const head = a.el.textContent.slice(0, a.offset);
      const tail = b.el.textContent.slice(b.offset);
      for (let i = bi; i > ai; i--) lines[i].remove();
      const parts = text.split('\n');
      this._renderPlain(a.el,
        head + parts[0] + (parts.length === 1 ? tail : ''));
      let after = a.el;
      for (let i = 1; i < parts.length; i++) {
        const div = this._mkLine(parts[i]
          + (i === parts.length - 1 ? tail : ''));
        after.after(div);
        after = div;
      }
      this._setCaret(after, after.textContent.length - tail.length);
      this._scheduleDiff();
      this._scheduleDecorate();
    });

    this.surface.addEventListener('keydown', (e) => {
      const mod = e.ctrlKey || e.metaKey;
      // find works in read-only editors too - handled before the gate
      if (mod && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'f') {
        e.preventDefault();
        this.openFind();
        return;
      }
      if (e.key === 'F3') {
        e.preventDefault();
        if (this._find.open) this._findStep(e.shiftKey ? -1 : 1);
        else this.openFind();
        return;
      }
      if (e.key === 'Escape' && this._find.open) {
        e.preventDefault();
        this.closeFind();
        return;
      }
      if (this.readOnly) return;
      if (mod && !e.altKey) {
        const k = e.key.toLowerCase();
        if (this.mode === 'md') {
          if (k === 'b') { e.preventDefault(); this._wrapSel('**'); return; }
          if (k === 'i' && !e.shiftKey) { e.preventDefault(); this._wrapSel('*'); return; }
          if (k === 'e') { e.preventDefault(); this._wrapSel('`'); return; }
        }
        if (k === '/') { e.preventDefault(); this._toggleComment(); return; }
        if (k === 'z') { e.preventDefault(); this._diff(); e.shiftKey ? this._redoStep() : this._undoStep(); return; }
        if (k === 'y') { e.preventDefault(); this._diff(); this._redoStep(); return; }
        if (k === 's') {
          e.preventDefault();
          this._diff();
          if (this.opts.onSave) this.opts.onSave();
          return;
        }
      }
      if (e.key === 'Enter') { e.preventDefault(); this._splitAtCaret(); return; }
      if (e.key === 'Tab') {
        e.preventDefault();
        document.execCommand('insertText', false, '  ');
        this._scheduleDiff();
      }
    });

    // Ctrl/Cmd+Click follows a link (this is a SOURCE editor - a plain
    // click is the user placing the caret to edit). Web URLs open
    // externally; relative targets go to the host app's resolver.
    this.surface.addEventListener('click', (e) => {
      if (!e.ctrlKey && !e.metaKey) return;
      const a = e.target.closest && e.target.closest('.md-link[data-href]');
      if (!a || !this.surface.contains(a)) return;
      e.preventDefault();
      const href = a.dataset.href;
      // any real URI scheme → the OS default handler, after the user
      // confirms in-app; a bare target is a library document
      if (typeof isAbsoluteUri === 'function' && isAbsoluteUri(href)) {
        if (typeof confirmOpenExternal === 'function') {
          confirmOpenExternal(href);
        }
      } else if (this.opts.onOpenLink) {
        this.opts.onOpenLink(href);
      }
    });

    let scrollRAF = 0;
    this.scroller.addEventListener('scroll', () => {
      if (scrollRAF) return;
      scrollRAF = requestAnimationFrame(() => { scrollRAF = 0; this._positionCopyBtns(); });
    });
  }
}

/* editor mode for a file name: markdown gets the decorated view, yaml and
 * friends get whole-file code highlighting, everything else plain. */
function editorModeFor(name) {
  const n = String(name || '').toLowerCase();
  if (n.endsWith('.md') || n.endsWith('.markdown')) return { mode: 'md' };
  if (n.endsWith('.yaml') || n.endsWith('.yml')) return { mode: 'code', lang: 'yaml' };
  if (n.endsWith('.json')) return { mode: 'code', lang: 'json' };
  if (n.endsWith('.sh') || n.endsWith('.bash')) return { mode: 'code', lang: 'sh' };
  if (n.endsWith('.py')) return { mode: 'code', lang: 'py' };
  if (n.endsWith('.js')) return { mode: 'code', lang: 'js' };
  if (n.endsWith('.css')) return { mode: 'code', lang: 'css' };
  if (n.endsWith('.containerfile') || n.endsWith('.dockerfile') || n === 'dockerfile' || n === 'containerfile')
    return { mode: 'code', lang: 'dockerfile' };
  return { mode: 'code', lang: '' };
}
