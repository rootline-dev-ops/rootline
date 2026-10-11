"""End-to-end orchestration: fetch -> prefilter/redact -> (Claude agent) -> report."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent import AgentConfig, AgentError, analyze
from .context import ContextProvider, GitHubContext, LocalContext
from .github import GitHub
from .prefilter import PrefilterResult, prefilter
from .redact import Redactor
from .result import RunReport
from .tools import Toolbox

FAILED_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}


class NothingToDo(Exception):
    pass


@dataclass
class Options:
    budget_tokens: int = 12000
    use_llm: bool = True
    agent: AgentConfig | None = None


def make_client() -> Any:
    import anthropic  # imported lazily so prefilter-only use needs no API key

    return anthropic.Anthropic()


def _build_report(
    pre: PrefilterResult,
    ctx: ContextProvider,
    redactor: Redactor,
    opts: Options,
    client: Any | None,
) -> RunReport:
    context = ctx.describe()
    report = RunReport(
        context=context,
        prefilter=pre.stats(),
        failures=[f.to_dict() for f in pre.failures],
        failing_step=pre.failing_step,
    )
    if not opts.use_llm:
        return report
    if client is None and not os.environ.get("ANTHROPIC_API_KEY"):
        report.error = "ANTHROPIC_API_KEY is not set"
        return report
    cfg = opts.agent or AgentConfig.from_env()
    toolbox = Toolbox(log_lines=pre.lines, context=ctx, redactor=redactor)
    try:
        outcome = analyze(client or make_client(), cfg, toolbox, context, pre.excerpt)
    except AgentError as e:
        report.error = str(e)
        report.tool_calls = toolbox.calls
        return report
    except Exception as e:  # API/network errors: degrade to deterministic report
        report.error = f"{type(e).__name__}: {str(e)[:200]}"
        report.tool_calls = toolbox.calls
        return report
    report.analysis = outcome.analysis
    report.model = outcome.model
    report.models_tried = outcome.models_tried
    report.usage = outcome.usage
    report.tool_calls = toolbox.calls
    # Final belt-and-braces redaction of everything the model wrote.
    a = report.analysis
    a.summary, a.root_cause, a.suggested_fix, a.patch = (
        redactor.redact(a.summary),
        redactor.redact(a.root_cause),
        redactor.redact(a.suggested_fix),
        redactor.redact(a.patch),
    )
    for e in a.evidence:
        e.excerpt, e.reference = redactor.redact(e.excerpt), redactor.redact(e.reference)
    return report


def run_local(
    log_file: str,
    *,
    repo_dir: str | None = None,
    base_ref: str | None = None,
    workflow_file: str | None = None,
    opts: Options | None = None,
    client: Any | None = None,
) -> RunReport:
    opts = opts or Options()
    redactor = Redactor.from_env()
    raw = Path(log_file).read_text(encoding="utf-8", errors="replace")
    pre = prefilter(raw, budget_tokens=opts.budget_tokens, redactor=redactor)
    ctx = LocalContext(
        log_path=log_file,
        repo_dir=Path(repo_dir) if repo_dir else None,
        base_ref=base_ref,
        workflow_file=workflow_file,
    )
    return _build_report(pre, ctx, redactor, opts, client)


def run_github(
    gh: GitHub,
    run_id: int,
    *,
    job_id: int | None = None,
    pr_number: int | None = None,
    force: bool = False,
    opts: Options | None = None,
    client: Any | None = None,
) -> tuple[RunReport, GitHubContext]:
    opts = opts or Options()
    redactor = Redactor.from_env()
    run = gh.get_run(run_id)
    if not force and run.get("conclusion") not in FAILED_CONCLUSIONS:
        raise NothingToDo(f"run {run_id} concluded {run.get('conclusion')!r}; nothing to analyse")
    jobs = gh.list_jobs(run_id)
    if job_id:
        job = next((j for j in jobs if j["id"] == job_id), None)
        if job is None:
            raise NothingToDo(f"job {job_id} not found in run {run_id}")
    else:
        failed = [j for j in jobs if j.get("conclusion") in FAILED_CONCLUSIONS]
        if not failed:
            raise NothingToDo(f"run {run_id} has no failed jobs")
        failed.sort(key=lambda j: j.get("completed_at") or "")
        job = failed[0]
    failed_step = next(
        (
            s.get("name")
            for s in job.get("steps") or []
            if s.get("conclusion") in FAILED_CONCLUSIONS
        ),
        None,
    )
    raw = gh.get_job_log(job["id"])
    pre = prefilter(
        raw, budget_tokens=opts.budget_tokens, failing_step_hint=failed_step, redactor=redactor
    )
    if pr_number is None:
        pr_number = gh.find_pr_for_run(run)
    ctx = GitHubContext(gh=gh, run=run, job=job, pr_number=pr_number)
    report = _build_report(pre, ctx, redactor, opts, client)
    other = [
        j.get("name")
        for j in jobs
        if j.get("conclusion") in FAILED_CONCLUSIONS and j["id"] != job["id"]
    ]
    if other:
        report.context["other_failed_jobs"] = other
    if failed_step:
        report.context["failed_step_from_api"] = failed_step
    return report, ctx
