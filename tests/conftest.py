from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def fixture_text():
    return load


# ----------------------------------------------------------------- fake Anthropic client
def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_use(id_: str, name: str, input_: dict):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def response(content, stop_reason, usage=None):
    usage = usage or SimpleNamespace(
        input_tokens=100, output_tokens=50, cache_creation_input_tokens=0, cache_read_input_tokens=0
    )
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage)


class FakeMessages:
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        # Snapshot what was sent (messages are mutated later by the loop).
        import copy

        self.calls.append(copy.deepcopy(kwargs))
        if not self.scripted:
            raise AssertionError("FakeMessages: no more scripted responses")
        nxt = self.scripted.pop(0)
        return nxt(kwargs) if callable(nxt) else nxt


class FakeClient:
    def __init__(self, scripted):
        self.messages = FakeMessages(scripted)


# ----------------------------------------------------------------- fake GitHub HTTP
class FakeResp:
    def __init__(self, status=200, json_data=None, text=""):
        self.status_code = status
        self._json = json_data
        self.text = text if text else ("" if json_data is None else str(json_data))
        self.encoding = "utf-8"

    def json(self):
        return self._json


class FakeSession:
    """Routes (METHOD, path) -> response. Records requests."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list[tuple[str, str, dict]] = []

    def request(self, method, url, headers=None, timeout=None, **kw):
        path = url.replace("https://api.github.com", "")
        self.requests.append((method, path, kw))
        key = (method, path)
        if key not in self.routes:
            return FakeResp(404, {"message": "Not Found"}, text='{"message":"Not Found"}')
        r = self.routes[key]
        return r(kw) if callable(r) else r
