// Ctrl+F match scanning (edFindMatches in mdedit.js).
// Script-style: node tests/test_find_matches.js — nonzero exit on failure.
"use strict";

const fs = require("fs");
const vm = require("vm");
const path = require("path");

const src = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "js", "mdedit.js"), "utf8");
const sandbox = { console };
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "mdedit.js" });
const find = vm.runInContext("edFindMatches", sandbox);

let fails = 0;
function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  console.log((ok ? "ok  " : "FAIL") + "  " + name
    + (ok ? "" : ` — got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`));
  if (!ok) fails++;
}

check("simple hit",
  find(["hello world"], "world", false),
  [{ line: 0, start: 6, end: 11 }]);

check("case-insensitive by default",
  find(["Hello WORLD"], "world", false),
  [{ line: 0, start: 6, end: 11 }]);

check("case-sensitive mode",
  find(["Hello WORLD", "hello world"], "world", true),
  [{ line: 1, start: 6, end: 11 }]);

check("multiple hits on one line, non-overlapping",
  find(["ababab"], "abab", false),
  [{ line: 0, start: 0, end: 4 }]);

check("hits across lines carry their line index",
  find(["a x", "no", "a y a"], "a", false),
  [{ line: 0, start: 0, end: 1 },
   { line: 2, start: 0, end: 1 }, { line: 2, start: 4, end: 5 }]);

check("empty query finds nothing", find(["abc"], "", false), []);

check("no match", find(["abc"], "zzz", false), []);

check("cap respected",
  find(["aaaa", "aaaa"], "a", false, 3).length, 3);

check("adjacent repeats all found",
  find(["aaa"], "a", false),
  [{ line: 0, start: 0, end: 1 }, { line: 0, start: 1, end: 2 },
   { line: 0, start: 2, end: 3 }]);

console.log();
if (fails) { console.log("FAILURES: " + fails); process.exit(1); }
console.log("ALL PASS");
