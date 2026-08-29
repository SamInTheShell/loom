// Ctrl+/ toggle-comment logic (edToggleCommentLines in mdedit.js).
// Script-style: node tests/test_toggle_comment.js — nonzero exit on failure.
"use strict";

const fs = require("fs");
const vm = require("vm");
const path = require("path");

const src = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "js", "mdedit.js"), "utf8");
const sandbox = { console };
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "mdedit.js" });
const toggle = vm.runInContext("edToggleCommentLines", sandbox);

let fails = 0;
function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  console.log((ok ? "ok  " : "FAIL") + "  " + name
    + (ok ? "" : ` — got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`));
  if (!ok) fails = 1;
}

// single uncommented line → commented, delta +2
check("comment one line",
  toggle(["context: 128000"], "#"),
  { texts: ["# context: 128000"], deltas: [2] });

// single commented line → uncommented (marker + one space removed)
check("uncomment one line",
  toggle(["# context: 128000"], "#"),
  { texts: ["context: 128000"], deltas: [-2] });

// marker without trailing space still uncomments
check("uncomment tight marker",
  toggle(["#context: 128000"], "#"),
  { texts: ["context: 128000"], deltas: [-1] });

// indentation preserved: marker lands at the run's minimum indent
check("comment keeps indent",
  toggle(["  model: /x.gguf", "    mmproj: /mm.gguf"], "#").texts,
  ["  # model: /x.gguf", "  #   mmproj: /mm.gguf"]);

// uncomment keeps leading whitespace
check("uncomment keeps indent",
  toggle(["  # model: /x.gguf"], "#").texts,
  ["  model: /x.gguf"]);

// mixed run (some commented, some not) → everything commented
check("mixed run comments all",
  toggle(["# a: 1", "b: 2"], "#").texts,
  ["# # a: 1", "# b: 2"]);

// blank lines inside a run are left alone and don't set the indent column
check("blank lines skipped",
  toggle(["a: 1", "", "b: 2"], "#"),
  { texts: ["# a: 1", "", "# b: 2"], deltas: [2, 0, 2] });

// all-commented run (blanks ignored) → uncomment
check("round trip with blanks",
  toggle(["# a: 1", "", "# b: 2"], "#").texts,
  ["a: 1", "", "b: 2"]);

// a lone blank line still toggles (VS Code does this too)
check("lone blank line comments",
  toggle([""], "#").texts, ["# "]);
check("lone blank line uncomments",
  toggle(["# "], "#").texts, [""]);

// two-char markers (js //) — regex escaping must hold
check("double-slash marker",
  toggle(["const x = 1;"], "//"),
  { texts: ["// const x = 1;"], deltas: [3] });
check("double-slash uncomment",
  toggle(["// const x = 1;"], "//").texts,
  ["const x = 1;"]);

// sql marker contains regex-significant nothing, but dashes are fine
check("sql marker",
  toggle(["select 1"], "--").texts, ["-- select 1"]);

// perfect round trip over a realistic yaml block
const block = [
  "models:",
  "- name: Qwen 3.8 27B 128k",
  "  context: 128000",
  "",
  "  flags: |",
  "    -ngl 99",
];
check("yaml block round trip",
  toggle(toggle(block, "#").texts, "#").texts, block);

console.log();
console.log(fails ? "FAILURES above" : "ALL PASS");
process.exit(fails);
