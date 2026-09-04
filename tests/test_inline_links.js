// Inline markdown tokenizing (edInlineSegs in mdedit.js) - above all that
// links stay CLICKABLE inside emphasis: the library docs bold their
// cross-references (**[Quick start](quick-start.md)**) and Ctrl+Click
// must keep working on them.
// Script-style: node tests/test_inline_links.js - nonzero exit on failure.
"use strict";

const fs = require("fs");
const vm = require("vm");
const path = require("path");

const src = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "js", "mdedit.js"), "utf8");
const sandbox = { console };
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "mdedit.js" });
const segsOf = vm.runInContext("edInlineSegs", sandbox);

let fails = 0;
function check(name, cond, detail) {
  console.log((cond ? "ok  " : "FAIL") + "  " + name
    + (cond ? "" : " - " + (detail || "")));
  if (!cond) fails = 1;
}

const firstLink = (segs) =>
  segs.find(([t, c]) => c && c.split(" ").includes("md-link"));

let s = segsOf("see [docs](library.md) now");
let l = firstLink(s);
check("plain link carries its target",
  l && l[0] === "docs" && l[2] === "library.md", JSON.stringify(s));

s = segsOf("- **[Quick start](quick-start.md)** - from zero");
l = firstLink(s);
check("link inside bold still a link",
  !!l && l[2] === "quick-start.md", JSON.stringify(s));
check("bold styling kept on the nested link",
  l && l[1].split(" ").includes("md-b"), JSON.stringify(l));

s = segsOf("*[a](b.md)* and ~~[c](d.md)~~");
check("link inside italic", firstLink(s)?.[2] === "b.md", JSON.stringify(s));
check("link inside strikethrough",
  s.filter(([t, c]) => c && c.includes("md-link")).length === 2,
  JSON.stringify(s));

s = segsOf("**bold** then [x](y.md)");
check("plain bold unaffected",
  s.some(([t, c]) => t === "bold" && c === "md-b"), JSON.stringify(s));
check("sibling link unaffected", firstLink(s)?.[2] === "y.md");

s = segsOf("[ext](https://example.com)");
check("web link target intact", firstLink(s)?.[2] === "https://example.com");

s = segsOf("no markup at all");
check("plain text passes through",
  s.length === 1 && s[0][1] === null, JSON.stringify(s));

console.log(fails ? "\nFAILURES above" : "\nALL PASS");
process.exit(fails);
