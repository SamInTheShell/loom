"""Time-travel tests: the per-chat signal offset - arming/clearing,
signalTs stamping at send, the wire honoring it, fork carry-over, and
the worker's field refresh. Script-style: run with
`uv run python tests/test_timetravel.py`; nonzero exit on failure."""

import datetime
import os
import sys
import tempfile
from pathlib import Path

os.environ["LOOM_HOME"] = tempfile.mkdtemp(prefix="loomtest-tt-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loom import chats, libconfig  # noqa: E402
from loom import chat as chatmod  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL") + f"  {name}"
          + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def stamp(ms):
    return datetime.datetime.fromtimestamp(
        ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


from loom.app import Bus, JsApi  # noqa: E402

api = JsApi(Bus())
HOUR = 3600 * 1000
with tempfile.TemporaryDirectory(prefix="loomtest-ttlib-") as d:
    r = api.library_create(d + "/lib")
    check("library created", r["ok"], str(r))
    rt = api._need_root()
    (rt / "loom.yaml").write_text(
        "providers:\n- name: ws\n  url: http://127.0.0.1:9\n")

    c = chats.new_chat(rt)
    cid = c["id"]

    # ---- arming / clearing ----
    r = api.chat_set_time_travel(cid, 26 * HOUR)
    check("offset arms and persists",
          r["ok"] and r["data"]["offsetMs"] == 26 * HOUR
          and chats.load_chat(rt, cid)["timeTravelMs"] == 26 * HOUR, str(r))
    r = api.chat_set_time_travel(cid, 400 * 365 * 24 * HOUR)
    check("a silly offset is refused",
          not r["ok"] and "10 years" in r["error"], str(r))
    check("the refused offset did not stick",
          chats.load_chat(rt, cid)["timeTravelMs"] == 26 * HOUR)

    # ---- send stamps signalTs = real ts + offset ----
    r = api.chat_send(cid, "hello from the future")
    m = r["data"]["message"]
    check("a sent message carries the shifted signal",
          r["ok"] and m.get("signalTs") == m["ts"] + 26 * HOUR, str(m))
    got = chats.load_chat(rt, cid)["messages"][-1]
    check("signalTs persists alongside the real ts",
          got.get("signalTs") == got["ts"] + 26 * HOUR
          and got["signalTs"] != got["ts"], str(got))

    # ---- the wire reports the SIGNAL time, not the real one ----
    cfg = libconfig.load(rt)
    wire = chatmod._wire_messages(rt, cfg, chats.load_chat(rt, cid))
    user = [w for w in wire if w["role"] == "user"][-1]
    check("the wire bracket carries the shifted stamp",
          user["content"].startswith("[" + stamp(got["signalTs"]) + "]"),
          user["content"][:60])
    check("the real time is NOT on the wire",
          stamp(got["ts"]) not in user["content"], user["content"][:60])

    # ---- clearing: new messages report real time again ----
    r = api.chat_set_time_travel(cid, 0)
    check("clearing removes the field",
          r["ok"] and "timeTravelMs" not in chats.load_chat(rt, cid), str(r))
    r = api.chat_send(cid, "back to the present")
    check("a message after clearing has no signalTs",
          r["ok"] and "signalTs" not in r["data"]["message"], str(r))
    wire = chatmod._wire_messages(rt, cfg, chats.load_chat(rt, cid))
    users = [w for w in wire if w["role"] == "user"]
    check("old messages keep the stamps they reported",
          users[0]["content"].startswith("[" + stamp(got["signalTs"]) + "]"),
          users[0]["content"][:60])

    # ---- backwards works too ----
    api.chat_set_time_travel(cid, -3 * HOUR)
    r = api.chat_send(cid, "yesterday-ish")
    m = r["data"]["message"]
    check("negative offsets shift backwards",
          m.get("signalTs") == m["ts"] - 3 * HOUR, str(m))

    # ---- fork carries the armed offset ----
    r = api.chat_fork(cid, 0)
    check("a fork keeps time travel armed",
          r["ok"] and r["data"]["chat"].get("timeTravelMs") == -3 * HOUR,
          str(r["data"]["chat"].get("timeTravelMs")))

    # ---- the worker's field refresh picks up mid-run changes ----
    doc = chats.load_chat(rt, cid)          # the loop's in-memory copy
    api.chat_set_time_travel(cid, 5 * HOUR)
    chatmod._refresh_user_fields(rt, doc)
    check("the loop refreshes the offset from disk",
          doc.get("timeTravelMs") == 5 * HOUR, str(doc.get("timeTravelMs")))
    api.chat_set_time_travel(cid, 0)
    chatmod._refresh_user_fields(rt, doc)
    check("the loop drops a cleared offset",
          "timeTravelMs" not in doc, str(doc.get("timeTravelMs")))

    # ---- assistant time signals (chat.assistant_signals) ----
    from loom import configedit
    T = 1_700_000_000_000
    ac = chats.new_chat(rt)
    ac["messages"] = [
        {"role": "user", "content": "u", "ts": T},
        {"role": "assistant", "content": "first", "ts": T + 1000},
        {"role": "assistant", "content": "warped", "thinking": "hm",
         "ts": T + 2000, "signalTs": T + 2000 + 26 * HOUR},
    ]
    chats.save_chat(rt, ac)

    cfg = libconfig.load(rt)
    check("assistant_signals defaults to on",
          cfg["chat"]["assistant_signals"] is True)
    wire = chatmod._wire_messages(rt, cfg, chats.load_chat(rt, ac["id"]))
    asst = [w for w in wire if w["role"] == "assistant"]
    check("assistant replies carry their generation time",
          asst[0]["content"] == "[" + stamp(T + 1000) + "] first",
          asst[0]["content"])
    check("an assistant signalTs (time travel) wins over the real ts",
          "[" + stamp(T + 2000 + 26 * HOUR) + "] warped" in asst[1]["content"]
          and stamp(T + 2000) not in asst[1]["content"], asst[1]["content"])
    check("the think block stays first, the stamp prefixes the text",
          asst[1]["content"].startswith("<think>\nhm\n</think>\n["),
          asst[1]["content"][:40])
    sys_msg = wire[0]["content"]
    check("the env note explains assistant stamps when on",
          "your own reply" in sys_msg)

    # toggled off in loom.yaml: every assistant time signal disappears
    (rt / "loom.yaml").write_text(
        "providers:\n- name: ws\n  url: http://127.0.0.1:9\n"
        "chat:\n  assistant_signals: false\n")
    cfg = libconfig.load(rt)
    check("assistant_signals: false parses",
          cfg["chat"]["assistant_signals"] is False)
    wire = chatmod._wire_messages(rt, cfg, chats.load_chat(rt, ac["id"]))
    asst = [w for w in wire if w["role"] == "assistant"]
    check("toggled off strips every assistant stamp",
          asst[0]["content"] == "first"
          and "warped" in asst[1]["content"]
          and "[" not in asst[1]["content"].replace("<think>\nhm\n</think>\n", ""),
          str([a["content"] for a in asst]))
    user0 = [w for w in wire if w["role"] == "user"][0]
    check("user stamps stay when assistant signals are off",
          user0["content"].startswith("[" + stamp(T) + "]"),
          user0["content"][:40])
    check("the env note falls back to the user-only wording",
          "your own reply" not in wire[0]["content"])

    # the serializer writes the toggle only when it is off
    check("chat_lines omits assistant_signals when on",
          "assistant_signals" not in "\n".join(
              configedit.chat_lines({"model": "m", "assistant_signals": True})))
    lines = configedit.chat_lines({"assistant_signals": False})
    check("chat_lines writes assistant_signals: false",
          "  assistant_signals: false" in lines
          and libconfig.parse_text("\n".join(lines) + "\n")
          ["chat"]["assistant_signals"] is False, str(lines))

    # ---- the agent's display name (chat.assistant_name) ----
    check("assistant_name defaults to loom",
          libconfig.parse_text("chat: {}\n")["chat"]["assistant_name"]
          == "loom")
    check("a custom assistant_name parses",
          libconfig.parse_text("chat:\n  assistant_name: HAL\n")
          ["chat"]["assistant_name"] == "HAL")
    check("chat_lines omits the default name",
          "assistant_name" not in "\n".join(
              configedit.chat_lines({"model": "m",
                                     "assistant_name": "loom"})))
    check("chat_lines writes a custom name",
          "  assistant_name: HAL" in
          configedit.chat_lines({"assistant_name": "HAL"}))

    # ---- echoed-stamp stripping: the signal never lands in the visible
    # message, even when the model imitates its own stamped history ----
    strip = chatmod._strip_echoed_stamp
    check("an echoed leading stamp is stripped",
          strip("[2026-09-05 14:02 UTC] Sure, here is") == "Sure, here is")
    check("only the leading stamp goes",
          strip("[2026-09-05 14:02 UTC] at [2026-09-05 14:02 UTC] I did")
          == "at [2026-09-05 14:02 UTC] I did")
    check("a mid-text bracket is untouched",
          strip("The time [2026-09-05 14:02 UTC] matters")
          == "The time [2026-09-05 14:02 UTC] matters")
    check("ordinary brackets are untouched",
          strip("[citation] hello") == "[citation] hello")

    # ---- Ctrl+R on an empty chat: the model may open the conversation ----
    ec = chats.new_chat(rt)
    r = api.chat_continue(ec["id"])
    check("chat_continue accepts an empty chat",
          r["ok"], str(r))
    r = api.chat_continue("nope-such-chat")
    check("chat_continue still refuses an unknown chat",
          not r["ok"], str(r))

    # ---- the master toggle: NO datetime signals reach the model ----
    (rt / "loom.yaml").write_text(
        "providers:\n- name: ws\n  url: http://127.0.0.1:9\n")
    cfg = libconfig.load(rt)
    sc = chats.new_chat(rt)
    sc["messages"] = [
        {"role": "user", "content": "u", "ts": T},
        {"role": "assistant", "content": "a", "thinking": "hm",
         "ts": T + 1000},
    ]
    chats.save_chat(rt, sc)
    r = api.chat_set_time_signals(sc["id"], False)
    check("the toggle persists",
          r["ok"] and r["data"]["timeSignals"] is False
          and chats.load_chat(rt, sc["id"])["timeSignalsOff"] is True,
          str(r))
    wire = chatmod._wire_messages(rt, cfg, chats.load_chat(rt, sc["id"]))
    check("signals off: no user bracket",
          not [w for w in wire if w["role"] == "user"][0]["content"]
          .startswith("["),
          str(wire[-2]))
    asst = [w for w in wire if w["role"] == "assistant"][-1]
    check("signals off: no assistant bracket",
          "UTC]" not in asst["content"], asst["content"])
    check("signals off: no session-start stamp",
          "Session started" not in wire[0]["content"]
          and "brackets" not in wire[0]["content"])
    r = api.chat_set_time_signals(sc["id"], True)
    wire = chatmod._wire_messages(rt, cfg, chats.load_chat(rt, sc["id"]))
    check("re-enabling restores every signal",
          r["ok"]
          and [w for w in wire if w["role"] == "user"][0]["content"]
          .startswith("[" + stamp(T) + "]")
          and "Session started" in wire[0]["content"], str(r))

    api.chat_set_time_signals(sc["id"], False)
    doc2 = chats.load_chat(rt, sc["id"])
    r = api.chat_fork(sc["id"], 0)
    check("a fork carries signals-off",
          r["ok"] and r["data"]["chat"].get("timeSignalsOff") is True,
          str(r))
    api.chat_set_time_signals(sc["id"], True)
    chatmod._refresh_user_fields(rt, doc2)
    check("the loop drops a cleared signals-off",
          "timeSignalsOff" not in doc2, str(doc2.get("timeSignalsOff")))

if FAILS:
    print("\nFAILURES:", ", ".join(FAILS))
    sys.exit(1)
print("\nALL PASS")
