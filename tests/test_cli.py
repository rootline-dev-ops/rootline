import json

from rootline import cli
from rootline.github import GitHub

from .conftest import FIXTURES, FakeClient, FakeResp, FakeSession, response, text_block, tool_use
from .test_agent import FINAL

REPO = "acme/invoicer"


def test_prefilter_command(capsys):
    assert cli.main(["prefilter", "--log-file", str(FIXTURES / "maven.log"), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["frameworks"] == ["maven"]
    assert data["failures"][0]["name"] == "OrderServiceTest.appliesVolumeDiscount"


def test_local_no_llm(capsys, monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out_json = tmp_path / "r.json"
    rc = cli.main(
        [
            "analyze",
            "--log-file",
            str(FIXTURES / "go_test.log"),
            "--no-llm",
            "--json-out",
            str(out_json),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "TestTokenBucket_Refill" in out and "Deterministic findings only" in out
    report = json.loads(out_json.read_text())
    assert report["analysis"] is None and report["prefilter"]["original_lines"] == 14

    # render round-trip
    assert cli.main(["render", "--report", str(out_json)]) == 0
    assert "TestTokenBucket_Burst" in capsys.readouterr().out


def _github_routes(log_text, comments=None):
    run = {
        "id": 42,
        "name": "CI",
        "path": ".github/workflows/ci.yml",
        "workflow_id": 7,
        "conclusion": "failure",
        "head_sha": "4f2a9c1e",
        "head_branch": "feature/locale",
        "event": "pull_request",
        "run_attempt": 1,
        "html_url": "https://github.com/acme/invoicer/actions/runs/42",
        "pull_requests": [{"number": 212}],
        "repository": {"default_branch": "main"},
        "head_commit": {"message": "Refactor formatting"},
    }
    jobs = {
        "jobs": [
            {"id": 1, "name": "lint", "conclusion": "success", "steps": []},
            {
                "id": 2,
                "name": "test (3.12)",
                "conclusion": "failure",
                "completed_at": "2026-10-09T13:42:20Z",
                "html_url": "https://github.com/acme/invoicer/actions/runs/42/job/2",
                "labels": ["ubuntu-latest"],
                "steps": [{"name": "Run pytest -q --maxfail=5", "conclusion": "failure"}],
            },
        ]
    }
    return {
        ("GET", f"/repos/{REPO}/actions/runs/42"): FakeResp(200, run),
        ("GET", f"/repos/{REPO}/actions/runs/42/jobs"): FakeResp(200, jobs),
        ("GET", f"/repos/{REPO}/actions/jobs/2/logs"): FakeResp(200, text=log_text),
        ("GET", f"/repos/{REPO}/pulls/212"): FakeResp(
            200,
            text="diff --git a/invoicer/format.py b/invoicer/format.py\n-    return format_currency(total, currency, locale=invoice.locale)\n+    return format_currency(total, currency)\n",
        ),
        ("GET", f"/repos/{REPO}/issues/212/comments"): FakeResp(200, comments or []),
        ("POST", f"/repos/{REPO}/issues/212/comments"): FakeResp(
            201, {"id": 99, "html_url": "https://github.com/acme/invoicer/pull/212#issuecomment-99"}
        ),
    }


def test_github_mode_end_to_end_with_mocked_claude(monkeypatch, capsys, fixture_text, tmp_path):
    session = FakeSession(_github_routes(fixture_text("gha_pytest.log")))
    monkeypatch.setattr(
        GitHub,
        "from_env",
        classmethod(lambda cls, repo, token=None: GitHub(token="t", repo=repo, session=session)),
    )
    fake = FakeClient(
        [
            response([tool_use("t1", "get_pr_diff", {})], "tool_use"),
            response([text_block(json.dumps(FINAL))], "end_turn"),
        ]
    )
    monkeypatch.setattr("rootline.pipeline.make_client", lambda: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-0000000000000000000000")
    summary, outputs = tmp_path / "summary.md", tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))

    rc = cli.main(["analyze", "--repo", REPO, "--run-id", "42"])
    assert rc == 0
    assert "created comment on PR #212" in capsys.readouterr().out
    post = next(r for r in session.requests if r[0] == "POST")
    body = post[2]["json"]["body"]
    assert body.startswith("<!-- rootline:report key=.github/workflows/ci.yml -->")
    assert "`CI / test (3.12)` failed" in body and "Regression" in body
    assert "sk-ant-test" not in json.dumps(fake.messages.calls, default=str)
    first_user = fake.messages.calls[0]["messages"][0]["content"][0]["text"]
    assert (
        '"pull_request": 212' in first_user
        and "Failing step: Run pytest -q --maxfail=5" in first_user
    )
    assert "category=regression" in outputs.read_text()
    assert "Rootline" in summary.read_text()


def test_github_mode_dry_run_does_not_post(monkeypatch, capsys, fixture_text):
    session = FakeSession(_github_routes(fixture_text("gha_pytest.log")))
    monkeypatch.setattr(
        GitHub,
        "from_env",
        classmethod(lambda cls, repo, token=None: GitHub(token="t", repo=repo, session=session)),
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    rc = cli.main(["analyze", "--repo", REPO, "--run-id", "42", "--dry-run"])
    assert rc == 0
    assert not any(r[0] in ("POST", "PATCH") for r in session.requests)
    assert "AI analysis unavailable: ANTHROPIC_API_KEY is not set" in capsys.readouterr().out


def test_successful_run_is_skipped(monkeypatch, capsys, fixture_text):
    routes = _github_routes("")
    routes[("GET", f"/repos/{REPO}/actions/runs/42")] = FakeResp(
        200, {"id": 42, "conclusion": "success"}
    )
    session = FakeSession(routes)
    monkeypatch.setattr(
        GitHub,
        "from_env",
        classmethod(lambda cls, repo, token=None: GitHub(token="t", repo=repo, session=session)),
    )
    assert cli.main(["analyze", "--repo", REPO, "--run-id", "42"]) == 0
    assert "nothing to analyse" in capsys.readouterr().out


def test_client_kwargs_workspace_header():
    from rootline.pipeline import client_kwargs

    assert client_kwargs({}) == {}
    assert client_kwargs({"ANTHROPIC_WORKSPACE_ID": "  "}) == {}
    assert client_kwargs({"ANTHROPIC_WORKSPACE_ID": "wrkspc_abc"}) == {
        "default_headers": {"anthropic-workspace-id": "wrkspc_abc"}
    }


def test_make_client_sends_workspace_header(monkeypatch):
    from rootline.pipeline import make_client

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-0000000000000000000000")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_abc")
    client = make_client()
    assert client.default_headers.get("anthropic-workspace-id") == "wrkspc_abc"
