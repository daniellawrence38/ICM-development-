import pytest

import fake_arcpy as fa
from conftest import rows
from fakes import FakeClient, msg, text, tool_use
from wwnet_arcgis import arcpy_tools, assistant_run, gpkg, settings, shared, tools_impl
from wwnet_arcgis.messages import ChunkedWriter, Messages


def env(fake_arcpy, demo_gpkg, apply, script, sel=()):
    layers = [fa.make_layer("Nodes", gpkg.table_path(demo_gpkg, "wwn_nodes")),
              fa.make_layer("Gravity sewers", gpkg.table_path(demo_gpkg, "wwn_conduits"), selection=sel)]
    a = fake_arcpy(layers)
    client = FakeClient(script)
    return a, layers, client, (lambda key: client)


PROPOSE = [msg([text("Upsizing. "), tool_use("t1", "propose_edits", {
    "layer": "wwn_conduits", "reason": "capacity",
    "edits": [{"id": "P01", "field": "dn", "value": 225}]})], "tool_use"),
    msg([text("Done: proposed DN225 for P01.")])]


def test_end_to_end_apply_false(fake_arcpy, demo_gpkg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    a, layers, client, factory = env(fake_arcpy, demo_gpkg, False, PROPOSE)
    res = tools_impl.ask_assistant(a, Messages(a), "upsize P01", model="claude-sonnet-5-5",
                                   effort="low", apply=False, client_factory=factory,
                                   layers_fn=lambda: layers)
    assert a.writes == []
    assert rows(demo_gpkg, "select dn from wwn_conduits where conduit_id='P01'") == [(150,)]
    text_log = "\n".join(t for _, t in a.log)
    assert "PROPOSED EDITS (1) - NOT applied" in text_log and "Apply proposed edits" in text_log
    assert "P01" in text_log and "225" in text_log
    assert "Done: proposed DN225" in text_log                      # streamed text
    assert res.stop_reason == "end_turn" and res.tool_calls == 1
    # the tool result sent back to the model says nothing was applied
    tr = client.calls[1]["messages"][-1]["content"][0]
    assert '"applied": false' in tr["content"] and "re-run" in tr["content"]
    # request used the ArcGIS-adapted tool text + system prompt
    c0 = client.calls[0]
    qf = next(t for t in c0["tools"] if t["name"] == "query_features")
    assert "SQL WHERE" in qf["description"] and "QGIS" not in qf["description"]
    assert "ArcGIS Pro" in c0["system"][0]["text"] and "embedded in QGIS" not in c0["system"][0]["text"]
    assert c0["model"] == "claude-sonnet-5-5"


def test_end_to_end_apply_true(fake_arcpy, demo_gpkg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    a, layers, client, factory = env(fake_arcpy, demo_gpkg, True, PROPOSE)
    res = tools_impl.ask_assistant(a, Messages(a), "upsize P01", apply=True, client_factory=factory,
                                   layers_fn=lambda: layers)
    assert rows(demo_gpkg, "select dn from wwn_conduits where conduit_id='P01'") == [(225,)]
    assert len(a.writes) == 1
    assert "1 of 1 proposed edit(s) applied" in "\n".join(t for _, t in a.log)
    assert res.proposals[0].applied


def test_selection_context_and_history(fake_arcpy, demo_gpkg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    a, layers, client, factory = env(fake_arcpy, demo_gpkg, False,
                                     [msg([text("first answer")]), msg([text("second answer")])], sel=[1])
    kw = dict(client_factory=factory, layers_fn=lambda: layers)
    tools_impl.ask_assistant(a, Messages(a), "q1", "Gravity sewers", True, **kw)
    first_user = client.calls[0]["messages"][0]["content"]
    assert "1 feature(s) are selected" in first_user and "['P01']" in first_user and first_user.endswith("q1")
    key = tools_impl.map_key(a)
    assert settings.history_path(key).is_file()
    tools_impl.ask_assistant(a, Messages(a), "q2", **kw)
    sent = client.calls[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[1]["content"] == "first answer"
    # reset clears it
    client.script.append(msg([text("third")]))
    tools_impl.ask_assistant(a, Messages(a), "q3", reset=True, **kw)
    assert len(client.calls[2]["messages"]) == 1


def test_missing_key_and_unknown_model(fake_arcpy, demo_gpkg):
    a, layers, client, factory = env(fake_arcpy, demo_gpkg, False, [])
    with pytest.raises(RuntimeError, match="Set Assistant API Key"):
        tools_impl.ask_assistant(a, Messages(a), "hi", client_factory=factory, layers_fn=lambda: layers)
    with pytest.raises(ValueError, match="Unknown model"):
        assistant_run.run("x", api_key="k", model="gpt", effort="low",
                          tools=arcpy_tools.ArcpyTools(a), msgs=Messages(a), history_key="k")


def test_install_instructions_when_anthropic_missing(monkeypatch):
    client = shared.assistant("client")
    monkeypatch.setattr(client, "anthropic_available", lambda: False)
    with pytest.raises(RuntimeError) as ei:
        assistant_run.check_prereqs()
    m = str(ei.value)
    for needle in ("Clone Default", "pip install anthropic", "wwnet_native", "Package Manager",
                   "api.anthropic.com"):
        assert needle in m


def test_chunked_writer():
    out = []
    w = ChunkedWriter(out.append, chunk=20)
    w.write("hello world, this is a long streamed sentence\nnext")
    w.flush()
    assert out[0].startswith("hello world")
    assert all(len(x) <= 20 for x in out[:-2]) and out[-1] == "next"
    assert " ".join(out).replace("  ", " ").count("sentence") == 1
