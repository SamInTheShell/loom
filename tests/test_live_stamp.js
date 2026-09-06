// Live signal-stamp hold/strip (liveVisibleText in chat.js): an echoed
// [.. UTC] stamp never shows while streaming, partial prefixes are held
// back, ordinary brackets pass through.
// Script-style: node tests/test_live_stamp.js - nonzero exit on failure.
"use strict";

const fs = require("fs");
const vm = require("vm");
const path = require("path");

const src = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "js", "chat.js"), "utf8");
const sandbox = { console };
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "chat.js" });
const vis = vm.runInContext("liveVisibleText", sandbox);

let fails = 0;
function check(name, got, want) {
  const ok = got === want;
  console.log((ok ? "ok  " : "FAIL") + "  " + name
    + (ok ? "" : ` - got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`));
  if (!ok) fails++;
}

check("plain text passes through", vis("Hello there"), "Hello there");
check("a full echoed stamp is stripped",
  vis("[2026-09-05 14:02 UTC] Hello"), "Hello");
check("leading whitespace before the stamp goes too",
  vis("  [2026-09-05 14:02 UTC] Hi"), "Hi");
check("empty stream shows nothing", vis(""), "");
check("a bare opening bracket is held back", vis("["), "");
check("a partial date is held back", vis("[2026-09-0"), "");
check("a partial up to UTC is held back", vis("[2026-09-05 14:02 UT"), "");
check("a resolved non-stamp bracket shows",
  vis("[citation needed] sure"), "[citation needed] sure");
check("letters outside the stamp charset show immediately",
  vis("[foo"), "[foo");
check("a stamp-like bracket mid-text is untouched",
  vis("At [2026-09-05 14:02 UTC] I did"), "At [2026-09-05 14:02 UTC] I did");
check("stamp then newline content survives",
  vis("[2026-09-05 14:02 UTC] \nline"), "line");

if (fails) {
  console.log("\nFAILURES: " + fails);
  process.exit(1);
}
console.log("\nALL PASS");
