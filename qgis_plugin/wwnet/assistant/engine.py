"""Claude agent loop. Pure Python: no Qt/QGIS imports; tools are injected callables.

Manual tool-use loop per the Anthropic SDK docs:
 - stream each turn, take `get_final_message()`
 - check stop_reason (refusal / max_tokens) BEFORE running any tool
 - append the full `response.content` as the assistant turn
 - return ALL tool_results in a single user message (is_error on failures)
 - tool_choice stays at its default (auto): forced tool choice 400s on these models
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from . import client as client_mod

MAX_TOOL_RESULT_CHARS = 30000


class ToolError(Exception):
    """Raise from a tool handler to return an is_error tool_result with this message."""


@dataclass
class ToolResult:
    content: Any
    is_error: bool = False


@dataclass
class TurnResult:
    text: str = ""
    stop_reason: str = ""          # end_turn | refusal | max_tokens | cancelled | error | max_iterations
    error: str | None = None
    tool_calls: int = 0
    iterations: int = 0


def build_request(model: str, effort: str, system, tools, messages, max_tokens: int):
    """Return (use_beta, params) for client[.beta].messages.stream(**params)."""
    params: dict = dict(
        model=model, max_tokens=max_tokens, system=system, tools=tools, messages=list(messages),
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
    )
    if model in client_mod.FALLBACK_MODELS:
        # Server-side refusal fallback (beta). Not available for Haiku 5.5.
        params["betas"] = [client_mod.FALLBACK_BETA]
        params["fallbacks"] = "default"
        return True, params
    return False, params


class Engine:
    def __init__(self, client, model: str, system, tools: list, handlers: dict[str, Callable],
                 effort: str = client_mod.DEFAULT_EFFORT, max_tokens: int = 16000,
                 max_iterations: int = 12,
                 on_text: Callable[[str], None] | None = None,
                 on_event: Callable[[dict], None] | None = None,
                 should_stop: Callable[[], bool] | None = None):
        self.client = client
        self.model = model
        self.effort = effort
        self.system = system
        self.tools = tools
        self.handlers = handlers
        self.max_tokens = max_tokens
        self.max_iterations = max_iterations
        self.on_text = on_text or (lambda s: None)
        self.on_event = on_event or (lambda e: None)
        self.should_stop = should_stop or (lambda: False)
        self.messages: list = []

    # ------------------------------------------------------------- helpers
    def reset(self):
        self.messages = []

    def _event(self, type_, **kw):
        self.on_event({"type": type_, **kw})

    def _stream_once(self):
        use_beta, params = build_request(self.model, self.effort, self.system, self.tools,
                                         self.messages, self.max_tokens)
        api = self.client.beta.messages if use_beta else self.client.messages
        self._event("turn_start")
        with api.stream(**params) as stream:
            for text in stream.text_stream:
                if self.should_stop():
                    return None
                self.on_text(text)
            msg = stream.get_final_message()
        usage = getattr(msg, "usage", None)
        if usage is not None:
            self._event("usage",
                        input_tokens=getattr(usage, "input_tokens", None),
                        output_tokens=getattr(usage, "output_tokens", None),
                        cache_read=getattr(usage, "cache_read_input_tokens", None),
                        cache_write=getattr(usage, "cache_creation_input_tokens", None))
        for b in msg.content:
            if getattr(b, "type", None) == "fallback":
                frm = getattr(getattr(b, "from_", None), "model", "?")
                to = getattr(getattr(b, "to", None), "model", "?")
                self._event("notice", level="info", text=f"{frm} declined; {to} continued.")
        return msg

    def _run_tool(self, block) -> dict:
        name = block.name
        args = block.input if isinstance(block.input, dict) else {}
        self._event("tool_call", name=name, input=args)
        handler = self.handlers.get(name)
        is_error = False
        try:
            if handler is None:
                raise ToolError(f"Unknown tool {name!r}")
            res = handler(args)
            if isinstance(res, ToolResult):
                res, is_error = res.content, res.is_error
        except ToolError as exc:
            res, is_error = str(exc), True
        except Exception as exc:  # noqa: BLE001 - reported to the model
            res, is_error = f"{type(exc).__name__}: {exc}", True
        if not isinstance(res, str):
            try:
                res = json.dumps(res, default=str, ensure_ascii=False)
            except Exception as exc:  # noqa: BLE001
                res, is_error = f"Could not serialise tool result: {exc}", True
        if len(res) > MAX_TOOL_RESULT_CHARS:
            res = res[:MAX_TOOL_RESULT_CHARS] + "\n...[truncated; narrow the query or lower limit]"
        self._event("tool_result", name=name, is_error=is_error, preview=res[:300])
        out = {"type": "tool_result", "tool_use_id": block.id, "content": res}
        if is_error:
            out["is_error"] = True
        return out

    # ---------------------------------------------------------------- loop
    def send(self, user_text: str) -> TurnResult:
        result = TurnResult()
        mark = len(self.messages)
        self.messages.append({"role": "user", "content": user_text})
        texts: list[str] = []
        try:
            for _ in range(self.max_iterations):
                result.iterations += 1
                if self.should_stop():
                    result.stop_reason = "cancelled"
                    break
                msg = self._stream_once()
                if msg is None:
                    result.stop_reason = "cancelled"
                    if len(self.messages) == mark + 1:
                        del self.messages[mark:]
                    break
                sr = msg.stop_reason
                tool_uses = [b for b in msg.content if getattr(b, "type", None) == "tool_use"]
                texts.extend(b.text for b in msg.content if getattr(b, "type", None) == "text")

                if sr == "refusal":
                    d = getattr(msg, "stop_details", None)
                    why = getattr(d, "explanation", None) or getattr(d, "category", None) or ""
                    self._event("notice", level="warning",
                                text="The model declined this request" + (f" ({why})." if why else "."))
                    result.stop_reason = "refusal"
                    if len(self.messages) == mark + 1:
                        del self.messages[mark:]   # nothing useful happened; drop the turn
                    break
                if sr == "max_tokens":
                    if tool_uses:
                        # truncated tool input: never run it
                        self._event("notice", level="warning",
                                    text="Response hit the token limit mid tool call; tool not run.")
                    else:
                        self.messages.append({"role": "assistant", "content": msg.content})
                        self._event("notice", level="warning",
                                    text="Response truncated (max_tokens). Ask me to continue.")
                    result.stop_reason = "max_tokens"
                    break

                self.messages.append({"role": "assistant", "content": msg.content})
                if sr == "pause_turn":
                    continue
                if sr != "tool_use" or not tool_uses:
                    result.stop_reason = sr or "end_turn"
                    break

                results = []
                cancelled = False
                for b in tool_uses:
                    if self.should_stop():
                        cancelled = True
                        results.append({"type": "tool_result", "tool_use_id": b.id,
                                        "content": "Cancelled by user before running.",
                                        "is_error": True})
                        continue
                    results.append(self._run_tool(b))
                    result.tool_calls += 1
                # all results in ONE user message
                self.messages.append({"role": "user", "content": results})
                if cancelled:
                    result.stop_reason = "cancelled"
                    break
            else:
                result.stop_reason = "max_iterations"
                self._event("notice", level="warning",
                            text=f"Stopped after {self.max_iterations} model calls.")
        except Exception as exc:  # noqa: BLE001
            del self.messages[mark:]
            result.stop_reason = "error"
            result.error = client_mod.friendly_error(exc)
            self._event("notice", level="error", text=result.error)
        result.text = "\n\n".join(t for t in texts if t)
        return result
