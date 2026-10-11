"""Render a RunReport as a GitHub-flavoured Markdown PR comment."""

from __future__ import annotations

import re

from . import __version__
from .result import RunReport

MARKER_PREFIX = "<!-- rootline:report"
MAX_COMMENT_CHARS = 60000  # GitHub's hard limit is 65,536

CATEGORY_LABELS = {
    "regression": "🧩 Regression",
    "flaky": "🎲 Flaky",
    "config_env": "⚙️ Config / environment",
    "dependency": "📦 Dependency",
    "infra": "🏗️ Infrastructure",
    "unknown": "❔ Unknown",
}
CONFIDENCE_LABELS = {"high": "high", "medium": "medium", "low": "low"}


def marker(key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.:/-]", "_", key)[:120]
    return f"{MARKER_PREFIX} key={safe} -->"


def fence(text: str, lang: str = "") -> str:
    """Code fence that cannot be broken by backticks inside `text`."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text.rstrip()}\n{ticks}"


def neutralize(text: str) -> str:
    """Prevent model-written prose from @-mentioning users/teams or closing HTML comments."""
    text = re.sub(r"(?<![\w`])@(?=[A-Za-z0-9-])", "@\u200b", text)
    return text.replace("-->", "--\u200b>")


def inline_code(text: str) -> str:
    text = text.replace("\n", " ").strip()
    if "`" in text:
        return f"`` {text} ``"
    return f"`{text}`"


def render_comment(report: RunReport, key: str) -> str:
    ctx = report.context
    a = report.analysis
    title_target = (
        " / ".join(x for x in [ctx.get("workflow"), ctx.get("job")] if x)
        or ctx.get("log_file")
        or "CI"
    )
    out: list[str] = [marker(key)]

    if a:
        out.append(f"### 🔴 Rootline: `{title_target}` failed")
        out.append("")
        out.append(
            f"**{CATEGORY_LABELS.get(a.category, a.category)}** · confidence: **{CONFIDENCE_LABELS.get(a.confidence, a.confidence)}**"
            + (
                f" · failing step: {inline_code(report.failing_step)}"
                if report.failing_step
                else ""
            )
        )
        out.append("")
        if a.summary:
            out.append(f"> {neutralize(a.summary)}")
            out.append("")
        out.append("#### Root cause")
        out.append(neutralize(a.root_cause) or "_Not determined._")
        out.append("")
        if a.suggested_fix:
            out.append("#### Suggested fix")
            out.append(neutralize(a.suggested_fix))
            out.append("")
        if a.patch:
            out.append("<details><summary>Proposed patch (review before applying)</summary>")
            out.append("")
            out.append(fence(a.patch, "diff"))
            out.append("")
            out.append("</details>")
            out.append("")
        elif a.patch_note:
            out.append(f"_{a.patch_note}_")
            out.append("")
        if a.evidence:
            out.append("<details><summary>Evidence</summary>")
            out.append("")
            for e in a.evidence:
                out.append(f"- **{e.source}** · {neutralize(e.reference)}")
                if e.excerpt.strip():
                    out.append("")
                    out.append("  " + fence(e.excerpt, "text").replace("\n", "\n  "))
            out.append("")
            out.append("</details>")
            out.append("")
    else:
        out.append(f"### 🔴 Rootline: `{title_target}` failed")
        out.append("")
        if report.failing_step:
            out.append(f"Failing step: {inline_code(report.failing_step)}")
            out.append("")
        if report.error:
            out.append(
                f"_AI analysis unavailable: {neutralize(report.error).rstrip('.')}. Showing deterministic findings only._"
            )
        else:
            out.append("_Deterministic findings only (AI analysis not run)._")
        out.append("")

    if report.failures:
        out.append(
            f"<details{'' if a else ' open'}><summary>Detected failures ({len(report.failures)})</summary>"
        )
        out.append("")
        for f in report.failures[:20]:
            msg = f" — {inline_code(f['message'][:200])}" if f.get("message") else ""
            line = f" (log L{f['line']})" if f.get("line") else ""
            out.append(f"- `{f['framework']}` {inline_code(f['name'])}{msg}{line}")
        if len(report.failures) > 20:
            out.append(f"- … and {len(report.failures) - 20} more")
        out.append("")
        out.append("</details>")
        out.append("")

    # footer
    p = report.prefilter
    bits = [f"Rootline v{__version__}"]
    if report.model:
        bits.append(f"model `{report.model}`")
    if p:
        bits.append(f"{p.get('original_lines', 0):,} log lines → {p.get('kept_lines', 0):,} sent")
    if report.tool_calls:
        bits.append(
            f"{len(report.tool_calls)} tool call{'s' if len(report.tool_calls) != 1 else ''}"
        )
    if p and p.get("redactions"):
        bits.append(
            f"{p['redactions']} secret-like value{'s' if p['redactions'] != 1 else ''} redacted"
        )
    if ctx.get("run_url"):
        bits.append(f"[run]({ctx['run_url']})")
    out.append("<sub>" + " · ".join(bits) + "</sub>")
    out.append("")
    if a:
        out.append(
            "<sub>AI-generated analysis: verify before acting. Rootline never pushes changes.</sub>"
        )
    else:
        out.append("<sub>Deterministic log parsing only. Rootline never pushes changes.</sub>")
    body = "\n".join(out).rstrip() + "\n"
    if len(body) > MAX_COMMENT_CHARS:
        body = (
            body[: MAX_COMMENT_CHARS - 200]
            + "\n\n… _comment truncated to fit GitHub's size limit_\n"
        )
    return body
