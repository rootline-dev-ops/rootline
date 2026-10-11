"""Command-line interface: `rootline analyze | prefilter | render`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .agent import AgentConfig
from .github import GitHub, GitHubError
from .pipeline import NothingToDo, Options, run_github, run_local
from .prefilter import prefilter
from .redact import Redactor
from .render import marker, render_comment
from .result import Analysis, RunReport


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--budget-tokens",
        type=int,
        default=int(os.environ.get("ROOTLINE_LOG_BUDGET_TOKENS") or 12000),
        help="approximate token budget for the log excerpt (default 12000)",
    )


def _write_github_outputs(report: RunReport, comment_url: str | None) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    a = report.analysis
    with open(out, "a", encoding="utf-8") as f:
        f.write(f"category={a.category if a else ''}\n")
        f.write(f"confidence={a.confidence if a else ''}\n")
        f.write(f"comment-url={comment_url or ''}\n")


def _write_step_summary(body: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(body + "\n")


def cmd_analyze(args: argparse.Namespace) -> int:
    agent = AgentConfig.from_env()
    if args.model:
        agent.model = args.model
    if args.max_turns:
        agent.max_turns = args.max_turns
    opts = Options(budget_tokens=args.budget_tokens, use_llm=not args.no_llm, agent=agent)

    if args.log_file:
        report = run_local(
            args.log_file,
            repo_dir=args.repo_dir,
            base_ref=args.base,
            workflow_file=args.workflow_file,
            opts=opts,
        )
        key = args.comment_key or "local"
        body = render_comment(report, key)
        if args.json_out:
            Path(args.json_out).write_text(
                json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
            )
        print(body)
        if report.error and not args.no_llm:
            print(f"rootline: note: {report.error}", file=sys.stderr)
        return 0

    repo = args.repo or os.environ.get("GITHUB_REPOSITORY")
    run_id = args.run_id or os.environ.get("ROOTLINE_RUN_ID")
    if not repo or not run_id:
        print(
            "rootline: need --log-file, or --repo and --run-id (or GITHUB_REPOSITORY / ROOTLINE_RUN_ID)",
            file=sys.stderr,
        )
        return 2
    gh = GitHub.from_env(repo)
    try:
        report, ctx = run_github(
            gh, int(run_id), job_id=args.job_id, pr_number=args.pr, force=args.force, opts=opts
        )
    except NothingToDo as e:
        print(f"rootline: {e}")
        return 0
    except GitHubError as e:
        print(f"rootline: {e}", file=sys.stderr)
        return 1

    key = args.comment_key or (ctx.run.get("path") or ctx.run.get("name") or "ci")
    body = render_comment(report, key)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
    _write_step_summary(body)

    comment_url = None
    if args.dry_run:
        print(body)
        print(f"rootline: dry run, PR #{ctx.pr_number or '-'} not commented", file=sys.stderr)
    elif ctx.pr_number:
        try:
            action, comment = gh.upsert_comment(ctx.pr_number, body, marker(key))
        except GitHubError as e:
            print(f"rootline: could not comment on PR #{ctx.pr_number}: {e}", file=sys.stderr)
            return 1
        comment_url = comment.get("html_url")
        print(f"rootline: {action} comment on PR #{ctx.pr_number}: {comment_url}")
    else:
        print("rootline: run is not associated with an open PR; wrote job summary only")
        print(body)
    if report.error:
        print(f"rootline: note: {report.error}", file=sys.stderr)
    _write_github_outputs(report, comment_url)
    return 0


def cmd_prefilter(args: argparse.Namespace) -> int:
    raw = Path(args.log_file).read_text(encoding="utf-8", errors="replace")
    res = prefilter(raw, budget_tokens=args.budget_tokens, redactor=Redactor.from_env())
    if args.json:
        print(
            json.dumps(
                {
                    **res.stats(),
                    "failing_step": res.failing_step,
                    "frameworks": res.frameworks,
                    "failures": [f.to_dict() for f in res.failures],
                    "excerpt": res.excerpt,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(res.excerpt)
        print(f"\n# stats: {json.dumps(res.stats())}", file=sys.stderr)
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    d = json.loads(Path(args.report).read_text(encoding="utf-8"))
    analysis = Analysis.from_dict(d["analysis"]) if d.get("analysis") else None
    report = RunReport(
        context=d.get("context", {}),
        prefilter=d.get("prefilter", {}),
        failures=d.get("failures", []),
        failing_step=d.get("failing_step"),
        analysis=analysis,
        model=d.get("model"),
        models_tried=d.get("models_tried", []),
        usage=d.get("usage", {}),
        tool_calls=d.get("tool_calls", []),
        error=d.get("error"),
    )
    print(render_comment(report, args.comment_key))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rootline", description="Root-cause analysis for failed CI runs, powered by Claude."
    )
    p.add_argument("--version", action="version", version=f"rootline {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="analyse a failed GitHub Actions run or a local log file")
    src = a.add_argument_group("source (pick one)")
    src.add_argument("--log-file", help="analyse a local log file instead of a GitHub run")
    src.add_argument("--repo", help="owner/name (default: $GITHUB_REPOSITORY)")
    src.add_argument("--run-id", type=int, help="workflow run id (default: $ROOTLINE_RUN_ID)")
    a.add_argument("--job-id", type=int, help="analyse this job instead of the first failed one")
    a.add_argument("--pr", type=int, help="override the PR number to comment on")
    a.add_argument("--repo-dir", help="local mode: repository checkout for get_file / git diff")
    a.add_argument("--base", help="local mode: diff base for get_pr_diff, e.g. origin/main")
    a.add_argument("--workflow-file", help="local mode: path to the workflow YAML")
    a.add_argument(
        "--model", help="Claude model id (default: $ROOTLINE_MODEL or claude-sonnet-5-5)"
    )
    a.add_argument("--max-turns", type=int, help="max agent turns (default 8)")
    a.add_argument(
        "--no-llm", action="store_true", help="deterministic findings only; no Anthropic API call"
    )
    a.add_argument("--dry-run", action="store_true", help="print the comment instead of posting it")
    a.add_argument("--force", action="store_true", help="analyse even if the run did not fail")
    a.add_argument(
        "--comment-key", help="key for the idempotent comment marker (default: workflow path)"
    )
    a.add_argument("--json-out", help="also write the full report as JSON to this path")
    _add_common(a)
    a.set_defaults(func=cmd_analyze)

    pf = sub.add_parser(
        "prefilter", help="show the redacted, pre-filtered excerpt that would be sent"
    )
    pf.add_argument("--log-file", required=True)
    pf.add_argument("--json", action="store_true")
    _add_common(pf)
    pf.set_defaults(func=cmd_prefilter)

    r = sub.add_parser("render", help="render a saved JSON report as a Markdown comment")
    r.add_argument("--report", required=True)
    r.add_argument("--comment-key", default="local")
    r.set_defaults(func=cmd_render)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
