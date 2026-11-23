"""Glue between the shared assistant engine and the ArcGIS tools (no arcpy import here)."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

from . import settings, shared, vendor
from .messages import ChunkedWriter

MAX_TURNS_KEPT = 20

INSTALL_HELP = """\
The WWNet assistant needs the Python package 'anthropic', which is not installed in this
ArcGIS Pro Python environment.
Easiest: use the no-install bundle (CI artifact wwnet-arcgis-bundle-win-py311; it carries 'anthropic' and
wwnet_native in its vendor folder) and run the 'Verify Setup' tool. Alternative: Pro's default environment
(arcgispro-py3) is read-only, so:
  1. In ArcGIS Pro: Settings > Package Manager > Environment Manager > Clone Default
     (give it a name such as arcgispro-py3-wwnet) and activate the clone. Restart Pro.
  2. Open 'Python Command Prompt' from the ArcGIS start-menu folder (it uses the active environment) and run:
         pip install anthropic path\\to\\wwnet_native-<version>-cp39-abi3-win_amd64.whl
  3. Restart ArcGIS Pro and run the tool again.
If your IT policy blocks pip, ask IT to install 'anthropic' into the cloned environment and to allow
outbound HTTPS to api.anthropic.com."""

HOST_NOTES = """\

# Host: ArcGIS Pro
- This session runs in ArcGIS Pro (not QGIS). Layers come from the active map's GeoPackage tables.
- query_features `expression` is a SQL WHERE clause (SQLite dialect), not a QGIS expression, e.g.
  dn = 225 AND status = 'proposed'. Pass an empty string to list the first rows.
- propose_edits records proposals. Whether they are written depends on the engineer ticking
  'Apply proposed edits' when running the tool; the tool result says which happened. Never claim data
  changed unless the result says "applied": true. If edits were only proposed, tell the engineer they can
  apply exactly these proposals with the tool's 'Apply the proposals from the last run' option (no new
  question needed).
"""

_TOOL_OVERRIDES = {
    "list_layers": "List the layers and tables of the active ArcGIS Pro map: name, WWNet table, "
                   "feature count, selected count, fields and the business id field.",
    "get_selected_features": "Return the attributes (no geometry) of the features currently selected "
                             "on the map in a layer (max 100). Layer may be a WWNet table name "
                             "(wwn_conduits) or a layer name.",
    "query_features": "Return attributes of features matching a SQL WHERE clause (SQLite dialect), e.g. "
                      "dn = 225 AND status = 'proposed'. Use limit to cap rows (default 50, max 200).",
    "propose_edits": "Propose attribute edits. Each edit: id = business id value (e.g. conduit_id "
                     "'C12'), field, value. The tool records proposals; they are written only if the "
                     "engineer ran the tool with 'Apply proposed edits' ticked, or later applies them with "
                     "'Apply the proposals from the last run'. The result states whether edits were applied.",
}


def tool_definitions() -> list:
    defs = copy.deepcopy(shared.assistant("tools").tool_definitions())
    for d in defs:
        if d["name"] in _TOOL_OVERRIDES:
            d["description"] = _TOOL_OVERRIDES[d["name"]]
    return defs


def system_blocks() -> list:
    text = shared.assistant("prompt").build_system_text()
    text = text.replace("embedded in QGIS", "embedded in ArcGIS Pro") + HOST_NOTES
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def check_prereqs():
    """Raises RuntimeError with install instructions if `anthropic` is missing."""
    vendor.activate(shared.TOOLBOX_ROOT)
    if not shared.assistant("client").anthropic_available():
        raise RuntimeError(INSTALL_HELP)


# ------------------------------------------------------------------- history

def load_history(key: str) -> list:
    """Condensed history: alternating plain-text user/assistant messages."""
    try:
        data = json.loads(settings.history_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    msgs = []
    for t in (data.get("turns") or [])[-MAX_TURNS_KEPT:]:
        if isinstance(t, dict) and t.get("user") and t.get("assistant"):
            msgs += [{"role": "user", "content": t["user"]},
                     {"role": "assistant", "content": t["assistant"]}]
    return msgs


def save_history(key: str, messages: list, user: str, assistant: str):
    turns = [{"user": messages[i]["content"], "assistant": messages[i + 1]["content"]}
             for i in range(0, len(messages) - 1, 2)]
    if user and assistant:
        turns.append({"user": user, "assistant": assistant})
    settings._atomic_write(settings.history_path(key),
                           json.dumps({"version": 1, "turns": turns[-MAX_TURNS_KEPT:]},
                                      ensure_ascii=False, indent=1).encode("utf-8"))


def reset_history(key: str):
    for p in (settings.history_path(key), settings.proposals_path(key)):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def save_proposals(key: str, proposals: list):
    """Remember the not-yet-applied proposals of the last run (replaces earlier ones; [] clears)."""
    p = settings.proposals_path(key)
    if not proposals:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        return
    settings._atomic_write(p, json.dumps({"version": 1, "proposals": proposals}, ensure_ascii=False,
                                         indent=1, default=str).encode("utf-8"))


def load_proposals(key: str) -> list:
    try:
        data = json.loads(settings.proposals_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [d for d in data.get("proposals") or [] if isinstance(d, dict)]


@dataclass
class RunResult:
    text: str = ""
    stop_reason: str = ""
    error: str | None = None
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    proposals: list = field(default_factory=list)


def run(question: str, *, api_key: str, model: str, effort: str, tools, msgs,
        history_key: str, reset: bool = False, context: str = "", client_factory=None,
        persist: bool = True) -> RunResult:
    """Run one assistant turn. `tools` is an ArcpyTools; `msgs` a Messages sink."""
    client_mod = shared.assistant("client")
    engine_mod = shared.assistant("engine")
    if model not in dict(client_mod.MODELS):
        raise ValueError(f"Unknown model {model!r}; choose one of {[m for m, _ in client_mod.MODELS]}")
    if effort not in client_mod.EFFORTS:
        raise ValueError(f"Unknown effort {effort!r}; choose one of {list(client_mod.EFFORTS)}")
    if reset:
        reset_history(history_key)
        msgs.info("Conversation history reset.")
    prior = load_history(history_key)
    if client_factory is None:           # real client only: trust the Windows store (assistant run only)
        note = vendor.inject_truststore()
        if note:
            msgs.info(note)
    client = (client_factory or client_mod.make_client)(api_key)

    handlers = {**shared.assistant("tools").pure_handlers(), **tools.handlers()}
    out = RunResult()
    writer = ChunkedWriter(msgs.info)

    def on_event(e):
        t = e.get("type")
        if t == "tool_call":
            writer.flush()
            args = json.dumps(e.get("input"), default=str)
            msgs.info(f"[tool] {e['name']} {args[:160]}")
        elif t == "tool_result" and e.get("is_error"):
            msgs.warn(f"[tool error] {e['name']}: {e.get('preview', '')[:200]}")
        elif t == "notice":
            writer.flush()
            {"error": msgs.error, "warning": msgs.warn}.get(e.get("level"), msgs.info)(e["text"])
        elif t == "usage":
            out.tokens_in += e.get("input_tokens") or 0
            out.tokens_out += e.get("output_tokens") or 0

    eng = engine_mod.Engine(client, model, system_blocks(), tool_definitions(), handlers,
                            effort=effort, on_text=writer.write, on_event=on_event)
    eng.messages = list(prior)
    user_text = f"{context}\n\n{question}" if context else question
    res = eng.send(user_text)
    writer.flush()
    out.text, out.stop_reason, out.error = res.text, res.stop_reason, res.error
    out.tool_calls, out.proposals = res.tool_calls, list(tools.proposals)
    if persist and res.stop_reason in ("end_turn", "stop_sequence") and res.text:
        save_history(history_key, prior, question, res.text)
    return out
