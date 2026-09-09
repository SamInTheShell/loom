"""Vendor-layer tests: per-vendor auth headers and paths, probe
behavior (ctx from modern llama-server meta only; vendors without a
model listing), libconfig vendor validation (defaults, ninfer removal),
and the Anthropic dialect adapter - request translation out, SSE
translation back. Script-style: run with
`uv run python tests/test_vendors.py`; nonzero exit on failure."""

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["LOOM_HOME"] = tempfile.mkdtemp(prefix="loomtest-vend-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loom import libconfig, providers, sshtunnel  # noqa: E402
from loom import chat as chatmod  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL") + f"  {name}"
          + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------- auth headers per vendor ----------
captured = {}
_orig_req = sshtunnel.request


def _fake_request(method, url, headers, body, timeout, ssh, abort_box=None):
    captured.update(method=method, url=url, headers=dict(headers or {}),
                    body=body)
    class _R:
        def read(self):
            return b'{"data": []}'
        def close(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    return _R()


sshtunnel.request = _fake_request

providers.request({"vendor": "llama-cpp", "url": "http://h:1",
                   "key": "k1"}, "GET", "/v1/models")
check("llama-cpp auth is Bearer",
      captured["headers"].get("Authorization") == "Bearer k1"
      and "x-api-key" not in captured["headers"], str(captured["headers"]))

providers.request({"vendor": "anthropic",
                   "url": "https://api.anthropic.com", "key": "k2"},
                  "GET", "/v1/models")
check("anthropic auth is x-api-key + version header",
      captured["headers"].get("x-api-key") == "k2"
      and captured["headers"].get("anthropic-version") == "2023-06-01"
      and "Authorization" not in captured["headers"],
      str(captured["headers"]))

providers.request({"vendor": "openai", "url": "https://api.openai.com/v1",
                   "key": "k3"}, "GET", "/models")
check("hosted openai-family auth is Bearer",
      captured["headers"].get("Authorization") == "Bearer k3")

# legacy records still resolve (registry entries carried `type`)
check("legacy `type` records resolve their vendor",
      providers.vendor_key({"type": "llama-cpp"}) == "llama-cpp"
      and providers.vendor_key({}) == "llama-cpp"
      and providers.vendor_key({"vendor": "gemini"}) == "gemini")

# ---------- probing ----------
_orig_json = providers._get_json
paths = []


def _fake_json(prov, path, timeout=10):
    paths.append(path)
    if providers.vendor_key(prov) == "llama-cpp":
        return {"data": [{"id": "m1", "meta": {"n_ctx": 4096}},
                         {"id": "m2", "meta": {"n_ctx_train": 32768}},
                         {"id": "m3", "max_model_len": 262144}]}
    return {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]}


providers._get_json = _fake_json
rec = providers.probe({"name": "l", "vendor": "llama-cpp",
                       "url": "http://h:1"}, register=False)
check("ctx: meta.n_ctx first, n_ctx_train next, vLLM-style "
      "max_model_len (ninfer et al) last",
      [m["ctx"] for m in rec["models"]] == [4096, 32768, 262144],
      str(rec))
check("llama-cpp lists at /v1/models", paths[-1] == "/v1/models")
rec = providers.probe({"name": "o", "vendor": "openai",
                       "url": "https://api.openai.com/v1"}, register=False)
check("hosted vendors list at /models with ctx unknown",
      paths[-1] == "/models"
      and [m["ctx"] for m in rec["models"]] == [0, 0], str(rec))
n_before = len(paths)
rec = providers.probe({"name": "v", "vendor": "vertex",
                       "url": "https://x"}, register=False)
check("a vendor without a listing probes OK without a request",
      rec["state"] == "ok" and "type the model id" in rec["detail"]
      and len(paths) == n_before, str(rec))
providers._get_json = _orig_json
sshtunnel.request = _orig_req

# ---------- libconfig vendor validation ----------
with tempfile.TemporaryDirectory(prefix="loomtest-vendlib-") as d:
    root = Path(d)

    def load(text):
        (root / "loom.yaml").write_text(text)
        return libconfig.load(root)

    cfg = load("providers:\n- name: oa\n  vendor: openai\n")
    check("a hosted vendor's url defaults in",
          cfg["providers"][0]["url"] == "https://api.openai.com/v1",
          str(cfg["providers"]))
    cfg = load("providers:\n- name: ws\n  type: llama-cpp\n"
               "  url: http://h:1\n")
    check("legacy `type:` is honored as an alias",
          cfg["providers"][0]["vendor"] == "llama-cpp")
    cfg = load("providers:\n- name: ws\n  url: http://h:1\n"
               "chat:\n  max_output: 32768\n")
    check("chat.max_output parses", cfg["chat"]["max_output"] == 32768)
    cfg = load("providers:\n- name: ws\n  url: http://h:1\n")
    check("chat.max_output defaults to 0 (server decides)",
          cfg["chat"]["max_output"] == 0)
    try:
        load("providers:\n- name: ws\n  url: http://h:1\n"
             "chat:\n  max_output: -5\n")
        check("negative max_output rejected", False)
    except libconfig.ConfigError:
        check("negative max_output rejected", True)
    for bad, why in (
            ("providers:\n- name: x\n  type: ninfer\n  url: http://h\n",
             "ninfer removed"),
            ("providers:\n- name: x\n  vendor: vertex\n", "vertex no url"),
            ("providers:\n- name: x\n  vendor: watsonx\n", "unknown vendor")):
        try:
            load(bad)
            check(f"rejected: {why}", False)
        except libconfig.ConfigError as e:
            check(f"rejected: {why}",
                  "removed" in str(e) if why == "ninfer removed" else True,
                  str(e))

# ---------- the Anthropic request translation ----------
body = {
    "model": "claude-sonnet-4-5",
    "stream": True,
    "stream_options": {"include_usage": True},
    "messages": [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "calling a tool",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "shell",
                                      "arguments": '{"command": "ls"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "exit 0"},
        {"role": "tool", "tool_call_id": "c2", "content": "exit 1"},
        {"role": "user", "content": [
            {"type": "text", "text": "see this"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,QUJD"}}]},
    ],
    "tools": [{"type": "function", "function": {
        "name": "shell", "description": "run it",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}}}}}],
    "reasoning_effort": "high",
}
w = chatmod._anthropic_body(body)
check("system rides top-level", w["system"] == "be helpful"
      and all(m["role"] != "system" for m in w["messages"]))
check("no llama.cpp/openai-only fields leak",
      "stream_options" not in w and "reasoning_effort" not in w
      and "timings_per_token" not in w)
asst = next(m for m in w["messages"] if m["role"] == "assistant")
tu = next(b for b in asst["content"] if b["type"] == "tool_use")
check("assistant tool_calls become tool_use blocks with parsed input",
      tu["id"] == "c1" and tu["name"] == "shell"
      and tu["input"] == {"command": "ls"}, str(asst))
# both tool results and the following user turn merge into ONE user
# message (Anthropic wants strict alternation)
after = w["messages"][w["messages"].index(asst) + 1]
kinds = [b["type"] for b in after["content"]]
check("tool results + next user turn merge into one alternating message",
      after["role"] == "user"
      and kinds == ["tool_result", "tool_result", "text", "image"],
      str(kinds))
check("tool_result carries its call id",
      after["content"][0]["tool_use_id"] == "c1"
      and after["content"][1]["tool_use_id"] == "c2")
check("data-URI images become base64 blocks",
      after["content"][3]["source"] == {"type": "base64",
                                        "media_type": "image/png",
                                        "data": "QUJD"})
check("tools map to input_schema",
      w["tools"][0]["name"] == "shell"
      and w["tools"][0]["input_schema"]["properties"]["command"]
      == {"type": "string"})
check("reasoning_effort maps to a thinking budget under max_tokens",
      w["thinking"] == {"type": "enabled", "budget_tokens": 16384}
      and w["max_tokens"] > 16384, str(w.get("thinking")))
w2 = chatmod._anthropic_body({"model": "m", "messages": [
    {"role": "user", "content": "hi"}]})
check("no effort → no thinking, default max_tokens",
      "thinking" not in w2 and w2["max_tokens"] == 8192)
w3 = chatmod._anthropic_body({"model": "m", "max_tokens": 32768,
                              "messages": [{"role": "user",
                                            "content": "hi"}]})
check("a configured output budget rides through to Anthropic",
      w3["max_tokens"] == 32768)
w4 = chatmod._anthropic_body({"model": "m", "max_tokens": 32768,
                              "reasoning_effort": "high",
                              "messages": [{"role": "user",
                                            "content": "hi"}]})
check("the thinking budget rides ON TOP of the configured budget",
      w4["max_tokens"] == 16384 + 32768)

# ---------- the Anthropic stream translation ----------
EVENTS = [
    {"type": "message_start",
     "message": {"usage": {"input_tokens": 11}}},
    {"type": "ping"},
    {"type": "content_block_start", "index": 0,
     "content_block": {"type": "thinking"}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "thinking_delta", "thinking": "hmm"}},
    {"type": "content_block_start", "index": 1,
     "content_block": {"type": "text"}},
    {"type": "content_block_delta", "index": 1,
     "delta": {"type": "text_delta", "text": "hello"}},
    {"type": "content_block_start", "index": 2,
     "content_block": {"type": "tool_use", "id": "tc9", "name": "shell"}},
    {"type": "content_block_delta", "index": 2,
     "delta": {"type": "input_json_delta", "partial_json": '{"comm'}},
    {"type": "content_block_delta", "index": 2,
     "delta": {"type": "input_json_delta", "partial_json": 'and":"ls"}'}},
    {"type": "message_delta", "usage": {"output_tokens": 7}},
    {"type": "message_stop"},
]
resp = [("data: " + json.dumps(e) + "\n").encode() for e in EVENTS]
got = list(chatmod._anthropic_chunks(resp))
texts = [c["choices"][0]["delta"].get("content") for c in got
         if c.get("choices") and "content" in c["choices"][0]["delta"]]
thinks = [c["choices"][0]["delta"].get("reasoning_content") for c in got
          if c.get("choices")
          and "reasoning_content" in c["choices"][0]["delta"]]
tcs = [c["choices"][0]["delta"]["tool_calls"][0] for c in got
       if c.get("choices")
       and c["choices"][0]["delta"].get("tool_calls")]
usage = next((c["usage"] for c in got if c.get("usage")), None)
check("text deltas translate", texts == ["hello"])
check("thinking deltas translate", thinks == ["hmm"])
check("the tool call opens with id+name then streams arguments",
      tcs[0] == {"index": 0, "id": "tc9",
                 "function": {"name": "shell"}}
      and tcs[1]["function"]["arguments"] == '{"comm'
      and tcs[2]["function"]["arguments"] == 'and":"ls"}'
      and all(t["index"] == 0 for t in tcs), str(tcs))
check("usage totals combine input and output tokens",
      usage == {"prompt_tokens": 11, "completion_tokens": 7,
                "total_tokens": 18}, str(usage))
err = list(chatmod._anthropic_chunks(
    [b'data: {"type": "error", "error": {"message": "overloaded"}}\n']))
check("error events surface as error chunks",
      err == [{"error": {"message": "overloaded"}}], str(err))

# ---------- entry_lines writes the vendor field ----------
lines = providers.entry_lines("claude", "anthropic",
                              "https://api.anthropic.com", "")
check("entry_lines writes vendor:", "  vendor: anthropic" in lines,
      str(lines))

if FAILS:
    print("\nFAILURES:", ", ".join(FAILS))
    sys.exit(1)
print("\nALL PASS")
