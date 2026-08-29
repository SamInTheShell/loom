# Programming knowledge

Knowledge about programming languages and their tooling, one folder per
language. Language-agnostic tooling can live at this level if it truly
spans languages; when in doubt, file it under the language it is used
with. Like everything in the knowledge base, these are practices for
whoever does the work — the same steps apply at a keyboard or in a chat.

- **python/** — Python: the `uv` package manager, building desktop GUI
  apps with pywebview, and the agent-harness component catalog for a
  desktop AI coding workbench (agentic tool-calling loop, streaming,
  LLM provider adapters and routing, bare-repo git depot, sandboxed
  shell containers, tool catalogs, secret envs, hybrid search,
  attachments, UI bridge, persistence).
- **go/** — Go: reliable testing practice, urfave/cli v3, web apps
  (gorilla/mux, html/template, MVC monolith structure), the
  distributed-systems component catalog for building a databox-style
  distributed key-value + blob store (etcd raft, multi-group, sharding,
  Pebble, BadgerDB, erasure coding, blob management, user systems, wire
  protocols, frontends), and self-hosted service patterns (pairing
  crypto, gateway relay, postoffice mail relay, embedded git forge, and
  a protocols atlas).
- **containers/** — language-agnostic container & build orchestration:
  the house Makefile conventions (help by default, self-documenting
  targets, podman/docker auto-detection and override), kind-up/kind-down
  local Kubernetes patterns, and serving a directory with nginx.
- **web/** — browser-side HTML/CSS/JS without frameworks or external
  libraries: a desktop-first design language with its token sheet, a
  widget/component catalog, the no-framework app architecture
  (state, rendering, routing, popouts), and interactive SVG
  node-graph visualizations.

To find something: search for the topic, then read the match; folder
READMEs say which file answers what. General engineering methodology
(planning, verification) lives in the `general` folder.

To add something: one folder per language, one file per topic; give each
file a `# heading`, lead with the recommendation, write it as practice
anyone can execute, and list it in the folder's README.

This folder is plain files in the library — edit or delete freely in the
Library tab.
