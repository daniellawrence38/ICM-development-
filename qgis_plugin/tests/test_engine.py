import json
from types import SimpleNamespace as NS

import pytest

from wwnet.assistant import client as client_mod
from wwnet.assistant import prompt, tools
from wwnet.assistant.engine import Engine, ToolError, ToolResult, build_request  # noqa: F401

SYSTEM = [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]


def text(t):
    return NS(type="text", text=t)


def tool_use(i, name, inp):
    return NS(type="tool_use", id=i, name=name, input=inp)


def msg(content, stop="end_turn", **kw):
    return NS(content=content, stop_reason=stop, usage=NS(input_tokens=1, output_tokens=1,
              cache_read_input_tokens=0, cache_creation_input_tokens=0), **kw)


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        for b in self.message.content:
            if b.type == "text":
                yield from (b.text[:3], b.text[3:])

    def get_final_message(self):
        return self.message


class FakeAPI:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def stream(self, **kw):
        self.calls.append({**kw, "messages": [dict(m) for m in kw["messages"]]})
        nxt = self.script.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return FakeStream(nxt)


class FakeClient:
    def __init__(self, script):
        self.messages = FakeAPI(script)
        self.beta = NS(messages=self.messages)   # same script/calls regardless of path


def make(script, handlers=None, model="claude-opus-5-5", **kw):
    c = FakeClient(script)
    streamed, events = [], []
    e = Engine(c, model, SYSTEM, tools.tool_definitions(), handlers or {}, on_text=streamed.append,
               on_event=events.append, **kw)
    return e, c, streamed, events


def test_tool_round_trip_and_request_shape():
    seen = []
    first = msg([text("Let me check. "), tool_use("t1", "pipe_capacity", {"dn_mm": 225, "grade_1in": 300})],
                "tool_use")
    final = msg([text("Capacity is 21.7 L/s.")])
    e, c, streamed, events = make([first, final],
                                  {"pipe_capacity": lambda a: seen.append(a) or {"capacity_lps": 21.7}})
    res = e.send("What is the capacity of DN225 at 1:300?")
    assert res.stop_reason == "end_turn" and res.tool_calls == 1 and res.iterations == 2
    assert seen == [{"dn_mm": 225, "grade_1in": 300}]
    assert "".join(streamed) == "Let me check. Capacity is 21.7 L/s."
    # history: user, assistant(full content), user(tool_results), assistant
    roles = [m["role"] for m in e.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert e.messages[1]["content"] is first.content
    tr = e.messages[2]["content"]
    assert len(tr) == 1 and tr[0]["type"] == "tool_result" and tr[0]["tool_use_id"] == "t1"
    assert json.loads(tr[0]["content"]) == {"capacity_lps": 21.7} and "is_error" not in tr[0]
    # second request carried the tool_result
    assert c.messages.calls[1]["messages"][-1]["content"][0]["tool_use_id"] == "t1"
    p = c.messages.calls[0]
    assert p["thinking"] == {"type": "adaptive"} and p["output_config"] == {"effort": "medium"}
    assert p["betas"] == ["server-side-fallback-2026-07-01"] and p["fallbacks"] == "default"
    assert "tool_choice" not in p and p["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert any(ev["type"] == "tool_call" for ev in events)


def test_parallel_tools_single_user_message_with_errors():
    first = msg([tool_use("a", "ok_tool", {}), tool_use("b", "boom", {}), tool_use("c", "nope", {}),
                 tool_use("d", "soft", {})], "tool_use")
    def boom(a): raise RuntimeError("kaput")
    def soft(a): raise ToolError("layer missing")
    e, c, *_ = make([first, msg([text("done")])],
                    {"ok_tool": lambda a: "fine", "boom": boom, "soft": soft})
    res = e.send("go")
    assert res.tool_calls == 4
    results = e.messages[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b", "c", "d"]
    assert "is_error" not in results[0]
    assert results[1]["is_error"] and "kaput" in results[1]["content"]
    assert results[2]["is_error"] and "Unknown tool" in results[2]["content"]
    assert results[3]["is_error"] and results[3]["content"] == "layer missing"
    assert len([m for m in e.messages if m["role"] == "user" and isinstance(m["content"], list)]) == 1


def test_tool_result_object_and_truncation():
    first = msg([tool_use("a", "t", {}), tool_use("b", "big", {})], "tool_use")
    e, *_ = make([first, msg([text("x")])],
                 {"t": lambda a: ToolResult("denied", is_error=True), "big": lambda a: "x" * 100000})
    e.send("go")
    r = e.messages[2]["content"]
    assert r[0]["is_error"] and r[0]["content"] == "denied"
    assert len(r[1]["content"]) < 31000 and "truncated" in r[1]["content"]


def test_refusal_does_not_run_tools_and_rolls_back():
    called = []
    m = msg([tool_use("a", "t", {"x": 1})], "refusal",
            stop_details=NS(category="cyber", explanation="declined"))
    e, c, _, events = make([m], {"t": lambda a: called.append(a)})
    res = e.send("do something")
    assert res.stop_reason == "refusal" and called == []
    assert e.messages == []                      # turn dropped, history stays valid
    assert any(ev["type"] == "notice" and "declined" in ev["text"] for ev in events)


def test_max_tokens_with_tool_use_not_run():
    called = []
    e, *_ = make([msg([tool_use("a", "t", {})], "max_tokens")], {"t": lambda a: called.append(1)})
    res = e.send("go")
    assert res.stop_reason == "max_tokens" and called == []
    assert e.messages[-1]["role"] == "user"       # no dangling assistant tool_use


def test_max_tokens_text_only_keeps_message():
    e, *_ = make([msg([text("partial")], "max_tokens")])
    assert e.send("go").stop_reason == "max_tokens"
    assert e.messages[-1]["role"] == "assistant"


def test_pause_turn_continues():
    e, c, *_ = make([msg([text("part")], "pause_turn"), msg([text("rest")])])
    res = e.send("go")
    assert res.stop_reason == "end_turn" and len(c.messages.calls) == 2


def test_max_iterations():
    loop = [msg([tool_use(f"i{n}", "t", {})], "tool_use") for n in range(5)]
    e, *_ = make(loop, {"t": lambda a: "r"}, max_iterations=3)
    assert e.send("go").stop_reason == "max_iterations"


def test_multi_turn_keeps_history():
    e, c, *_ = make([msg([text("one")]), msg([text("two")])])
    e.send("a")
    e.send("b")
    assert [m["role"] for m in c.messages.calls[1]["messages"]] == ["user", "assistant", "user"]


def test_haiku_has_no_fallback_params():
    use_beta, p = build_request("claude-haiku-5-5", "low", SYSTEM, [], [], 100)
    assert not use_beta and "fallbacks" not in p and "betas" not in p
    e, c, *_ = make([msg([text("hi")])], model="claude-haiku-5-5", effort="low")
    e.send("x")
    assert "fallbacks" not in c.messages.calls[0] and c.messages.calls[0]["output_config"] == {"effort": "low"}


def test_api_error_friendly_and_rolled_back():
    anthropic = pytest.importorskip("anthropic")
    import httpx2
    resp = httpx2.Response(401, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    err = anthropic.AuthenticationError("invalid x-api-key", response=resp, body=None)
    e, *_ = make([err])
    res = e.send("hi")
    assert res.stop_reason == "error" and "API key was rejected" in res.error
    assert e.messages == []
    resp = httpx2.Response(429, headers={"retry-after": "12"},
                           request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    assert "12" in client_mod.friendly_error(anthropic.RateLimitError("slow", response=resp, body=None))
    assert "Network" in client_mod.friendly_error(
        anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")))


def test_cancel_before_tool_runs():
    flag = {"stop": False}
    first = msg([tool_use("a", "t", {})], "tool_use")
    e, *_ = make([first], {"t": lambda a: "never"}, should_stop=lambda: flag["stop"])
    flag["stop"] = False
    orig = e._stream_once

    def once():
        m = orig()
        flag["stop"] = True       # user presses Stop while the model is replying
        return m

    e._stream_once = once
    res = e.send("go")
    assert res.stop_reason == "cancelled"
    assert e.messages[-1]["content"][0]["is_error"]       # history still valid


def test_tool_definitions_strict_and_unique():
    defs = tools.tool_definitions()
    names = [d["name"] for d in defs]
    assert len(set(names)) == len(names)
    assert set(names) == {"list_layers", "get_selected_features", "query_features", "get_feature",
                          "check_conduit", "pipe_capacity", "residential_gsdf", "lookup_ds50",
                          "propose_edits", *tools.FLOW_TOOLS}
    for d in defs:
        assert d["strict"] is True and d["input_schema"]["additionalProperties"] is False
        assert set(d["input_schema"]["required"]) <= set(d["input_schema"]["properties"])
    item = next(d for d in defs if d["name"] == "propose_edits")["input_schema"]["properties"]["edits"]["items"]
    assert item["additionalProperties"] is False
    assert tools.tool_definitions() == defs         # deterministic (cache-stable)


def test_system_prompt_cacheable_and_complete():
    blocks = prompt.build_system_blocks()
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    t = blocks[0]["text"]
    assert "Ver 2 Rev 4" in t and "[[grades]]" in t and "wwn_conduits (features)" in t
    assert "Australian spelling" in t and "NEVER invent DS 50 values" in t
    assert prompt.build_system_blocks() == blocks   # byte-stable prefix


def test_pure_tool_handlers():
    h = tools.pure_handlers()
    cap = h["pipe_capacity"]({"dn_mm": 225, "grade_1in": 300, "depth_ratio": 0.6667})
    assert cap["capacity_lps"] == pytest.approx(21.7, abs=0.15)
    at = h["pipe_capacity"]({"dn_mm": 225, "grade_1in": 300, "flow_lps": 10})
    assert 0.3 < at["depth_ratio_at_flow"] < 0.5 and at["full_flow_lps"] > 25
    assert h["check_conduit"]({"dn_mm": 150, "grade_1in": 200})["source"] == "wwnet"
    assert h["lookup_ds50"]({"table": "grades", "key": "150"})["max_ac_spacing_m"] == 100
    assert h["residential_gsdf"]({"net_area_ha": 5, "rcode": "R30", "ground": "wet",
                                  "region": "north"})["gsdf_lps"] == pytest.approx(2.325)


def test_flow_tools_on_demo(tmp_path):
    pytest.importorskip("wwnet_native")
    import shutil
    import wwnet_native
    from conftest import REPO
    f = tmp_path / "flows.gpkg"
    shutil.copy(REPO / "examples" / "demo" / "demo_flows.gpkg", f)
    h = tools.flow_handlers(lambda: str(f))
    assert set(h) == set(tools.FLOW_TOOLS)
    d = h["design_summary"]({})
    assert d["summary"]["conduits"] == 11
    r = h["get_design_result"]({"conduit_id": "P01"})
    assert r["conduit_id"] == "P01" and r["checks"]
    with pytest.raises(ToolError):
        h["get_design_result"]({"conduit_id": "nope"})
    with pytest.raises(ToolError, match="No stored simulation runs"):
        h["get_sim_result"]({"object_id": "P01"})
    for label, scale in (("base", 1.0), ("x2", 2.0)):
        wwnet_native.simulate(str(f), 12.0, 6.0, flow_scale=scale, label=label)
    runs = h["list_sim_runs"]({})
    assert runs["count"] == 2 and runs["runs"][0]["label"] == "x2"
    sr = h["get_sim_result"]({"object_id": "P01", "run": "base"})
    assert sr["run"].startswith("base (") and sr["conduit"]["max_flow_lps"] > 0
    node = h["get_sim_result"]({"object_id": "N01"})
    assert "freeboard_m" in node["node"]
    cmp_ = h["compare_sim_runs"]({"run_a": "base", "run_b": "x2"})
    assert cmp_["counts"].get("worse", 0) + cmp_["counts"].get("new_surcharge", 0) > 0
    t = h["trace_network"]({"start_id": "P08", "direction": "upstream"})
    assert t["summary"]["start_conduit"] == "P08" and t["summary"]["adwf_lps"] > 0
    assert tools.conduit_derived({"length_m": 50, "us_invert": 10, "ds_invert": 9.5}) == \
        {"length_m_used": 50, "grade_1in": 100.0}


def test_validate_edits():
    ft = {"fid": "int", "conduit_id": "text", "dn": "int", "status": "text", "us_invert": "real",
          "material": "text", "length_m": "real"}
    ok, bad = tools.validate_edits("wwn_conduits", [
        {"id": "C1", "field": "dn", "value": 300.0}, {"id": "C1", "field": "us_invert", "value": "12.5"},
        {"id": "C1", "field": "status", "value": "built"}, {"id": "C1", "field": "conduit_id", "value": "X"},
        {"id": "C1", "field": "fid", "value": 3}, {"id": "C1", "field": "nope", "value": 1},
        {"id": "C1", "field": "dn", "value": None}, {"id": "C1", "field": "length_m", "value": "abc"},
        {"id": "C1", "field": "material", "value": "pvc"}], ft)
    assert [(e["field"], e["value"]) for e in ok] == [("dn", 300), ("us_invert", 12.5), ("material", "pvc")]
    assert len(bad) == 6
    assert tools.validate_edits("wwn_issues", [{"id": "1", "field": "message", "value": "x"}], ft)[1]
