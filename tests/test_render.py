from rootline.render import MARKER_PREFIX, fence, marker, neutralize, render_comment
from rootline.result import Analysis, RunReport

ANALYSIS = {
    "summary": "2 pytest failures in tests/test_format.py after the formatter refactor.",
    "root_cause": "format_total() no longer passes invoice.locale to babel.numbers.format_currency, so it uses the default en_US locale.",
    "category": "Regression",
    "confidence": "high",
    "evidence": [
        {
            "source": "log",
            "reference": "log L89",
            "excerpt": "E       AssertionError: assert '€1,234.50' == '1.234,50\\xa0€'",
        },
        {
            "source": "diff",
            "reference": "diff invoicer/format.py",
            "excerpt": "-    return format_currency(total, currency, locale=invoice.locale)\n+    return format_currency(total, currency)",
        },
    ],
    "suggested_fix": "Pass `locale=invoice.locale` again in `invoicer/format.py`. cc @octocat",
    "patch": "--- a/invoicer/format.py\n+++ b/invoicer/format.py\n@@ -10,3 +10,3 @@ def format_total(invoice):\n     total = invoice.total()\n-    return format_currency(total, invoice.currency)\n+    return format_currency(total, invoice.currency, locale=invoice.locale)\n",
}


def make_report(analysis=None, **kw):
    return RunReport(
        context={
            "workflow": "CI",
            "job": "test (3.12)",
            "run_url": "https://github.com/acme/x/actions/runs/1",
        },
        prefilter={"original_lines": 2214, "kept_lines": 41, "redactions": 2},
        failures=[
            {
                "framework": "pytest",
                "name": "tests/test_format.py::test_a",
                "message": "AssertionError",
                "line": 107,
            }
        ],
        failing_step="Run pytest -q",
        analysis=Analysis.from_dict(analysis) if analysis else None,
        model="claude-sonnet-5-5" if analysis else None,
        tool_calls=[{"tool": "get_pr_diff"}, {"tool": "get_file"}] if analysis else [],
        **kw,
    )


def test_full_comment_structure():
    body = render_comment(make_report(ANALYSIS), ".github/workflows/ci.yml")
    assert body.startswith(marker(".github/workflows/ci.yml"))
    assert "### 🔴 Rootline: `CI / test (3.12)` failed" in body
    assert "**🧩 Regression** · confidence: **high**" in body  # category normalised to lowercase
    assert "#### Root cause" in body and "#### Suggested fix" in body
    assert "```diff\n--- a/invoicer/format.py" in body
    assert "<summary>Evidence</summary>" in body
    assert "2,214 log lines → 41 sent" in body
    assert "2 tool calls" in body
    assert "2 secret-like values redacted" in body
    assert "AI-generated analysis" in body


def test_mentions_are_neutralised():
    body = render_comment(make_report(ANALYSIS), "k")
    assert "@octocat" not in body
    assert "@\u200boctocat" in body


def test_marker_is_stable_and_sanitised():
    assert marker("CI / build") == marker("CI / build")
    assert marker("a b") == f"{MARKER_PREFIX} key=a_b -->"
    assert "-->" not in marker("x-->y")[:-3]


def test_fence_survives_backticks():
    text = "```js\ncode\n```"
    f = fence(text, "diff")
    assert f.startswith("````diff") and f.endswith("````")


def test_neutralize_html_comment_close():
    assert "-->" not in neutralize("evil --> <!-- rootline:report key=x -->")


def test_invalid_patch_is_dropped():
    bad = dict(ANALYSIS, patch="just change line 10 to use locale")
    a = Analysis.from_dict(bad)
    assert a.patch == ""
    body = render_comment(make_report(bad), "k")
    assert "was not a well-formed unified diff" in body
    assert "```diff" not in body


def test_deterministic_only_comment():
    body = render_comment(make_report(None, error="ANTHROPIC_API_KEY is not set"), "k")
    assert "AI analysis unavailable: ANTHROPIC_API_KEY is not set" in body
    assert "<details open><summary>Detected failures (1)</summary>" in body
    assert "AI-generated" not in body
    assert "Deterministic log parsing only" in body


def test_unknown_category_and_confidence_coerced():
    a = Analysis.from_dict(dict(ANALYSIS, category="weird", confidence="certain"))
    assert a.category == "unknown" and a.confidence == "low"


def test_comment_size_capped():
    huge = dict(ANALYSIS, root_cause="x" * 100000)
    body = render_comment(make_report(huge), "k")
    assert len(body) <= 60000
    assert "truncated" in body
