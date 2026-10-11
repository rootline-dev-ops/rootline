"""Exercise the real `anthropic` SDK against a local stub of the Messages API.

No network and no API key: this verifies that the requests Rootline builds (tools with
cache_control, system blocks, output_config.format, tool_result round-trips including
the SDK's own content-block objects) serialise correctly through the official SDK.
It does NOT validate model behaviour.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic
import pytest

from rootline.agent import AgentConfig, analyze
from rootline.context import LocalContext
from rootline.prefilter import prefilter
from rootline.redact import Redactor
from rootline.tools import Toolbox

from .test_agent import FINAL

USAGE = {
    "input_tokens": 1200,
    "output_tokens": 80,
    "cache_creation_input_tokens": 1100,
    "cache_read_input_tokens": 0,
}


def msg(content, stop_reason, usage=USAGE):
    return {
        "id": "msg_stub",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }


SCRIPT = [
    msg(
        [
            {"type": "thinking", "thinking": "Check the log.", "signature": "sig"},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "search_log",
                "input": {"pattern": "FAILED", "context": 0},
            },
        ],
        "tool_use",
    ),
    msg(
        [{"type": "text", "text": json.dumps(FINAL)}],
        "end_turn",
        {**USAGE, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1100},
    ),
]


@pytest.fixture
def stub_server():
    received = []
    script = list(SCRIPT)

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            received.append({"path": self.path, "body": body, "headers": dict(self.headers)})
            payload = json.dumps(script.pop(0)).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}", received
    srv.shutdown()


def test_real_sdk_request_shape(stub_server, fixture_text):
    base_url, received = stub_server
    client = anthropic.Anthropic(api_key="stub-key", base_url=base_url, max_retries=0)
    pre = prefilter(fixture_text("gha_pytest.log"))
    tb = Toolbox(log_lines=pre.lines, context=LocalContext(log_path="ci.log"), redactor=Redactor())
    out = analyze(client, AgentConfig(effort="medium"), tb, {"mode": "local"}, pre.excerpt)

    assert out.analysis.root_cause == FINAL["root_cause"]
    assert out.usage["cache_read_input_tokens"] == 1100
    assert len(received) == 2
    first, second = received[0]["body"], received[1]["body"]
    assert received[0]["path"] == "/v1/messages"
    assert first["model"] == "claude-sonnet-5-5"
    assert first["output_config"]["format"]["type"] == "json_schema"
    assert first["output_config"]["effort"] == "medium"
    assert first["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    # assistant turn (incl. thinking block + signature) is replayed verbatim
    assistant = second["messages"][1]
    assert assistant["role"] == "assistant"
    assert (
        assistant["content"][0]["type"] == "thinking"
        and assistant["content"][0]["signature"] == "sig"
    )
    tool_result = second["messages"][2]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "toolu_1"
    assert "FAILED tests/test_format.py" in tool_result["content"]
