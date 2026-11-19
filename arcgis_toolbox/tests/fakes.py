"""Scripted fake Anthropic client (same streaming shape the shared engine consumes)."""
from types import SimpleNamespace as NS


def text(t):
    return NS(type="text", text=t)


def tool_use(i, name, inp):
    return NS(type="tool_use", id=i, name=name, input=inp)


def msg(content, stop="end_turn"):
    return NS(content=content, stop_reason=stop,
              usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                       cache_creation_input_tokens=0))


class _Stream:
    def __init__(self, m):
        self.m = m

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        for b in self.m.content:
            if b.type == "text":
                yield from (b.text[:5], b.text[5:])

    def get_final_message(self):
        return self.m


class FakeClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        api = NS(stream=self._stream)
        self.messages = api
        self.beta = NS(messages=api)

    def _stream(self, **kw):
        self.calls.append({**kw, "messages": [dict(m) for m in kw["messages"]]})
        return _Stream(self.script.pop(0))
