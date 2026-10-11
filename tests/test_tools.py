import subprocess

import pytest

from rootline.context import ContextUnavailable, LocalContext, check_path_allowed
from rootline.redact import Redactor
from rootline.tools import Toolbox, split_diff_by_file


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "config/.env.production",
        "deploy/id_rsa",
        "certs/server.pem",
        "../etc/passwd",
        "a/../../b",
        ".git/config",
        "aws_credentials.json",
    ],
)
def test_denylist(path):
    with pytest.raises(PermissionError):
        check_path_allowed(path)


def test_allowed_path():
    assert check_path_allowed("/src/app.py") == "src/app.py"


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def test_local_context_diff_and_files(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "other.py").write_text("y = 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "init")
    git(tmp_path, "checkout", "-qb", "feature")
    (tmp_path / "app.py").write_text("x = 2\napi_key = 'abcdef1234567890'\n")
    (tmp_path / "other.py").write_text("y = 2\n")
    git(tmp_path, "commit", "-qam", "change")
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("on: push\n")

    ctx = LocalContext(log_path="x.log", repo_dir=tmp_path, base_ref="main")
    tb = Toolbox(log_lines=["a", "error: boom", "c"], context=ctx, redactor=Redactor())
    out, err = tb.run("get_pr_diff", {"path_filter": "app.py"})
    assert (
        not err
        and "Changed files: app.py" in out
        and "+x = 2" in out
        and "other.py" not in out.split("\n", 1)[1]
    )
    assert "abcdef1234567890" not in out and "[REDACTED:secret_assignment]" in out
    out, err = tb.run("get_file", {"path": "app.py", "start_line": 1, "end_line": 1})
    assert "    1| x = 2" in out and "api_key" not in out
    out, _ = tb.run("get_workflow_yaml", {})
    assert out == "on: push\n"
    out, err = tb.run("list_previous_runs", {})
    assert err and "only available in GitHub mode" in out
    out, err = tb.run("search_log", {"pattern": "ERROR", "context": 1})
    assert "1 matches" in out and "     2| error: boom" in out
    out, err = tb.run("get_log_lines", {"start": 3, "end": 99})
    assert out.strip() == "3| c"
    out, err = tb.run("nope", {})
    assert err and "unknown tool" in out
    out, err = tb.run("search_log", {"pattern": "("})
    assert err


def test_result_size_cap():
    class Big:
        def get_file(self, path):
            return "x\n" * 100000

    tb = Toolbox(log_lines=[], context=Big(), redactor=Redactor(), max_result_chars=1000)
    out, _ = tb.run("get_file", {"path": "big.txt", "start_line": 1, "end_line": 100000})
    assert len(out) < 1200 and "truncated" in out


def test_no_repo_dir():
    ctx = LocalContext(log_path="x.log")
    with pytest.raises(ContextUnavailable):
        ctx.get_file("a.py")


def test_split_diff_by_file():
    d = "diff --git a/x.py b/x.py\n+1\ndiff --git a/y.py b/y.py\n+2\n"
    assert [p for p, _ in split_diff_by_file(d)] == ["x.py", "y.py"]
