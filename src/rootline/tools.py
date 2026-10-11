"""Read-only tools the Claude agent can call while investigating a failure.

Every tool result is redacted and size-capped before it goes back to the model.
There are deliberately no write tools: the only side effect of a Rootline run is the
PR comment, which is posted by deterministic code after the agent finishes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .context import ContextProvider, ContextUnavailable
from .github import GitHubError
from .redact import Redactor

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "search_log",
        "description": (
            "Search the FULL normalised job log (not just the excerpt you were given) with a "
            "case-insensitive regular expression. Returns up to 15 matches with surrounding "
            "context and line numbers. Use it to find setup output, earlier warnings, or the "
            "first occurrence of an error that the excerpt omitted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Python regular expression, matched case-insensitively.",
                },
                "context": {
                    "type": "integer",
                    "description": "Lines of context before/after each match (0-10).",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "get_log_lines",
        "description": "Return a range of lines (max 200) from the full normalised job log, by line number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {
                    "type": "integer",
                    "description": "First line number (1-based, inclusive).",
                },
                "end": {"type": "integer", "description": "Last line number (inclusive)."},
            },
            "required": ["start", "end"],
        },
    },
    {
        "name": "get_file",
        "description": (
            "Read a file from the repository at the exact commit that was tested. Returns numbered "
            "lines. Optionally restrict to a line range. Paths are relative to the repository root. "
            "Secret-like files (.env, keys, credentials) are blocked."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repository-relative path, e.g. src/app/main.py",
                },
                "start_line": {"type": "integer", "description": "Optional first line (1-based)."},
                "end_line": {"type": "integer", "description": "Optional last line (inclusive)."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "get_pr_diff",
        "description": (
            "Return the unified diff of the pull request that triggered this run (or the local diff "
            "against the configured base). Optionally filter to files whose path contains a substring."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path_filter": {
                    "type": "string",
                    "description": "Optional substring; only matching files are returned.",
                },
            },
        },
    },
    {
        "name": "get_workflow_yaml",
        "description": "Return the GitHub Actions workflow file that defines the failed run.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_previous_runs",
        "description": (
            "List recent runs of the same workflow on this branch and on the default branch, with "
            "conclusion, commit and timestamp. Use it to judge whether a failure is new (regression), "
            "intermittent (flaky) or pre-existing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Runs per branch (1-20, default 10)."}
            },
        },
    },
]


def _number(text: str, start: int = 1) -> str:
    lines = text.split("\n")
    return "\n".join(f"{i:>5}| {line}" for i, line in enumerate(lines, start))


def split_diff_by_file(diff: str) -> list[tuple[str, str]]:
    parts = re.split(r"(?m)^(?=diff --git )", diff)
    out = []
    for p in parts:
        if not p.strip():
            continue
        m = re.match(r"diff --git a/(\S+) b/(\S+)", p)
        out.append((m.group(2) if m else "?", p))
    return out


@dataclass
class Toolbox:
    log_lines: list[str]
    context: ContextProvider
    redactor: Redactor
    max_result_chars: int = 20000
    calls: list[dict] = field(default_factory=list)

    def definitions(self) -> list[dict]:
        return [dict(t) for t in TOOL_DEFINITIONS]

    def run(self, name: str, args: dict) -> tuple[str, bool]:
        """Execute a tool. Returns (content, is_error)."""
        args = args or {}
        try:
            handler = getattr(self, f"_t_{name}", None)
            if handler is None:
                raise ValueError(f"unknown tool {name!r}")
            out, err = handler(**args), False
        except (
            ContextUnavailable,
            PermissionError,
            FileNotFoundError,
            ValueError,
            TypeError,
            re.error,
        ) as e:
            out, err = f"{type(e).__name__}: {e}", True
        except GitHubError as e:
            out, err = f"GitHub API error: {e}", True
        out = self.redactor.redact(out)
        if len(out) > self.max_result_chars:
            out = (
                out[: self.max_result_chars]
                + f"\n… [truncated: result exceeded {self.max_result_chars} characters]"
            )
        self.calls.append({"tool": name, "input": args, "is_error": err, "chars": len(out)})
        return out, err

    # -- tools ---------------------------------------------------------------
    def _t_search_log(self, pattern: str, context: int = 2) -> str:
        context = max(0, min(int(context), 10))
        rx = re.compile(pattern, re.IGNORECASE)
        hits = [i for i, line in enumerate(self.log_lines) if rx.search(line)]
        if not hits:
            return f"no matches for /{pattern}/ in {len(self.log_lines)} lines"
        blocks, last = [], -1
        for i in hits[:15]:
            s, e = max(0, i - context, last + 1), min(len(self.log_lines), i + context + 1)
            if s >= e:
                continue
            blocks.append("\n".join(f"{j + 1:>6}| {self.log_lines[j]}" for j in range(s, e)))
            last = e - 1
        more = f"\n… {len(hits) - 15} more matches" if len(hits) > 15 else ""
        return f"{len(hits)} matches\n" + "\n  ---\n".join(blocks) + more

    def _t_get_log_lines(self, start: int, end: int) -> str:
        start, end = max(1, int(start)), int(end)
        end = min(end, start + 199, len(self.log_lines))
        if start > end:
            raise ValueError(f"empty range; log has {len(self.log_lines)} lines")
        return "\n".join(f"{j:>6}| {self.log_lines[j - 1]}" for j in range(start, end + 1))

    def _t_get_file(
        self, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> str:
        text = self.context.get_file(path)
        if text.startswith("[directory listing]"):
            return text
        lines = text.split("\n")
        s = max(1, int(start_line or 1))
        e = min(len(lines), int(end_line or len(lines)))
        if not start_line and not end_line and len(lines) > 400:
            e = 400
            note = f"\n… file has {len(lines)} lines; request a range to see more"
        else:
            note = ""
        return (
            f"{path} (lines {s}-{e} of {len(lines)})\n"
            + _number("\n".join(lines[s - 1 : e]), s)
            + note
        )

    def _t_get_pr_diff(self, path_filter: str | None = None) -> str:
        diff = self.context.get_pr_diff()
        if not diff.strip():
            return "(empty diff)"
        files = split_diff_by_file(diff)
        if path_filter:
            files = [(p, d) for p, d in files if path_filter in p]
            if not files:
                return f"no changed files match {path_filter!r}"
        index = "Changed files: " + ", ".join(p for p, _ in files)
        return index + "\n\n" + "".join(d for _, d in files)

    def _t_get_workflow_yaml(self) -> str:
        return self.context.get_workflow_yaml()

    def _t_list_previous_runs(self, limit: int = 10) -> str:
        runs = self.context.list_previous_runs(max(1, min(int(limit), 20)))
        return json.dumps(runs, indent=1) if runs else "no previous runs found"
