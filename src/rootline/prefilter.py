"""Deterministic log pre-filter.

Turns a raw CI job log (often thousands of lines) into a compact, redacted excerpt that
fits a token budget, before anything is sent to the model:

1. normalise: strip ANSI escapes and GitHub timestamps, resolve carriage-return progress
   bars, redact secrets;
2. segment by GitHub Actions step (``##[group]Run ...``) and find the failing step;
3. drop known-noise lines (package downloads, docker layer progress, ...) and collapse
   repeated lines;
4. run framework detectors (pytest, jest, go test, maven, gradle, npm/tsc, docker build,
   Python tracebacks, generic errors) to find decisive lines and structured failures;
5. build error windows around those lines, compress long stack traces, rank windows and
   pack them into the budget, always keeping line numbers so the model (and humans) can
   cite evidence and ask for more context.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .detectors import Detection, Failure, detect
from .redact import Redactor

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
TS_RE = re.compile(r"^\ufeff?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")
STEP_RE = re.compile(r"^##\[group\](Run .+|Post .+|Set up job|Complete job|Build container.+)$")

NOISE_RES = [
    re.compile(p)
    for p in [
        r"^\s*(?:Collecting|Downloading|Using cached|Requirement already satisfied|Obtaining|Building wheel|Created wheel|Stored in directory)\b",
        r"^\s*(?:Get|Hit|Ign):\d+ https?://",
        r"^(?:Selecting previously unselected|Preparing to unpack|Unpacking|Setting up|Processing triggers for)\b",
        r"^\(Reading database \.\.\.",
        r"^remote: (?:Enumerating|Counting|Compressing|Total)\b",
        r"^(?:Receiving|Resolving) (?:objects|deltas):",
        r"^#\d+ (?:sha256:[0-9a-f]+ [\d.]+[kMG]?B / |extracting sha256:|DONE [\d.]+s$|CACHED$|resolve \S+ [\d.]+s done$)",
        r"^\s*[\w./@-]+: (?:Pulling fs layer|Waiting|Downloading|Verifying Checksum|Download complete|Extracting|Pull complete)",
        r"^\[INFO\] Download(?:ing|ed) from ",
        r"^(?:Download|Progress) \(\d+\)|^Progress \(\d+\): ",
        r"^npm (?:WARN|warn) deprecated ",
        r"^go: downloading ",
        r"^\s*\|?\s*[█▉▊▋▌▍▎▏ ]{10,}\|? *\d+%",
        r"^\s*\d+%\s*\|[#=\- ]+\|",
        r"^\s*$",
    ]
]
STACK_FRAME_RE = re.compile(r"^\s+at [\w$.<>\[\]/:\-@ ()]+")
INTERNAL_FRAME_RE = re.compile(
    r"node_modules/|node:internal|internal/(?:process|modules)|java\.base/|jdk\.internal|"
    r"org\.junit\.(?:platform|jupiter\.engine)|org\.apache\.maven\.surefire|sun\.reflect|"
    r"java\.lang\.reflect|org\.gradle\.(?:api\.internal|internal)"
)


def estimate_tokens(text: str) -> int:
    """Cheap, deterministic token estimate (~4 chars/token for English and log text).

    Used only for budgeting the excerpt; real usage is read from the API response.
    """
    return math.ceil(len(text) / 4)


@dataclass
class Step:
    name: str
    start: int  # index into lines
    end: int  # exclusive


@dataclass
class PrefilterResult:
    excerpt: str
    lines: list[str]  # full normalised + redacted log (used by the search_log tool)
    failing_step: str | None
    failures: list[Failure]
    frameworks: list[str]
    gh_errors: list[str]
    original_lines: int
    kept_lines: int
    est_tokens: int
    truncated: bool
    redactions: int
    steps: list[str] = field(default_factory=list)

    def stats(self) -> dict:
        return {
            "original_lines": self.original_lines,
            "kept_lines": self.kept_lines,
            "est_tokens": self.est_tokens,
            "truncated": self.truncated,
            "redactions": self.redactions,
        }


def normalize(raw: str, redactor: Redactor | None = None) -> list[str]:
    """Strip ANSI + timestamps, resolve progress bars (\\r), redact secrets."""
    redactor = redactor or Redactor()
    text = raw.replace("\r\n", "\n")
    text = redactor.redact(text)  # multi-line secrets (PEM blocks) first
    out = []
    for line in text.split("\n"):
        if "\r" in line:  # progress bar: keep what the terminal finally showed
            line = [seg for seg in line.split("\r") if seg.strip()][-1:] or [""]
            line = line[0]
        line = ANSI_RE.sub("", line)
        line = TS_RE.sub("", line)
        out.append(line.rstrip())
    while out and not out[-1]:
        out.pop()
    return out


def split_steps(lines: list[str]) -> list[Step]:
    steps: list[Step] = []
    for i, line in enumerate(lines):
        m = STEP_RE.match(line)
        if m:
            if steps:
                steps[-1].end = i
            steps.append(Step(m.group(1), i, len(lines)))
    return steps


def find_failing_step(lines: list[str], steps: list[Step], hint: str | None) -> Step | None:
    if hint:
        for s in steps:
            if s.name == hint or s.name == f"Run {hint}" or hint in s.name:
                return s
    for i, line in enumerate(lines):
        if line.startswith("##[error]"):
            for s in steps:
                if s.start <= i < s.end and not s.name.startswith("Post "):
                    return s
    return None


def _is_noise(line: str) -> bool:
    return any(r.search(line) for r in NOISE_RES)


def _step_setup_noise(lines: list[str], steps: list[Step]) -> set[int]:
    """Lines inside the first ##[group] of a step after the command (shell:, env: ...)."""
    drop: set[int] = set()
    for s in steps:
        i = s.start + 1
        in_env = False
        while i < s.end and lines[i] != "##[endgroup]":
            line = lines[i]
            if line.startswith(("shell:", "env:")):
                in_env = line.startswith("env:")
                drop.add(i)
            elif in_env and line.startswith(" "):
                drop.add(i)
            else:
                in_env = False
            i += 1
        if i < s.end:
            drop.add(i)
    for i, line in enumerate(lines):
        if line in ("##[endgroup]",) or (line.startswith("##[group]") and not STEP_RE.match(line)):
            drop.add(i)
    return drop


def _compress_frames(idxs: list[int], lines: list[str], keep: int = 6) -> list[int | str]:
    """Collapse runs of stack frames: keep the first `keep` non-internal frames."""
    out: list[int | str] = []
    run: list[int] = []

    def flush() -> None:
        if not run:
            return
        kept = [i for i in run if not INTERNAL_FRAME_RE.search(lines[i])][:keep]
        if not kept:
            kept = run[:2]
        out.extend(kept)
        hidden = len(run) - len(kept)
        if hidden > 0:
            out.append(f"      … {hidden} more stack frames (framework/internal) omitted")
        run.clear()

    for i in idxs:
        if STACK_FRAME_RE.match(lines[i]):
            run.append(i)
        else:
            flush()
            out.append(i)
    flush()
    return out


def _render(
    items: list[int | str], lines: list[str], protect: frozenset[int] = frozenset()
) -> list[str]:
    """Number lines and collapse runs of identical/near-identical lines.

    Near-identical (equal after masking digits) lines are collapsed unless they are
    protected (decisive anchors such as distinct compiler errors or test failures).
    The last line of a collapsed run is shown, so the final state is never hidden.
    """
    rendered: list[str] = []
    prev_exact: str | None = None
    prev_norm: str | None = None
    run: list[int] = []

    def flush() -> None:
        if not run:
            return
        if len(run) > 1:
            rendered.append(
                f"      … {len(run) - 1} similar line{'s' if len(run) > 2 else ''} collapsed, last one:"
            )
        rendered.append(f"{run[-1] + 1:>6}| {lines[run[-1]]}")
        run.clear()

    for it in items:
        if isinstance(it, str):
            flush()
            rendered.append(it)
            prev_exact = prev_norm = None
            continue
        text = lines[it]
        norm = re.sub(r"\d+", "#", text)
        same = prev_exact is not None and (
            text == prev_exact or (norm == prev_norm and it not in protect)
        )
        if same:
            run.append(it)
            continue
        flush()
        rendered.append(f"{it + 1:>6}| {text}")
        prev_exact, prev_norm = text, norm
    flush()
    return rendered


def prefilter(
    raw: str,
    *,
    budget_tokens: int = 12000,
    failing_step_hint: str | None = None,
    redactor: Redactor | None = None,
    context_before: int = 6,
    context_after: int = 10,
) -> PrefilterResult:
    redactor = redactor or Redactor()
    lines = normalize(raw, redactor)
    steps = split_steps(lines)
    failing = find_failing_step(lines, steps, failing_step_hint)
    det: Detection = detect(lines)

    drop = _step_setup_noise(lines, steps)
    keepable = [i for i, line in enumerate(lines) if i not in drop and not _is_noise(line)]
    keepable_set = set(keepable)

    # Restrict to the failing step when we know it (plus nothing from Post steps).
    lo, hi = (failing.start, failing.end) if failing else (0, len(lines))

    def in_scope(i: int) -> bool:
        return lo <= i < hi

    anchors = {i: p for i, p in det.anchors.items() if in_scope(i)}
    if not anchors and failing:  # detectors found nothing inside the step: widen
        lo, hi = 0, len(lines)
        anchors = dict(det.anchors)

    protect = frozenset(i for i, p in anchors.items() if p >= 2)
    gh_errors = [ln[len("##[error]") :] for ln in lines if ln.startswith("##[error]")]

    # Build windows around anchors.
    windows: list[tuple[int, int, int]] = []  # (start, end_exclusive, score)
    for i, prio in anchors.items():
        after = max(context_after if prio >= 2 else 3, det.spans.get(i, 0))
        before = context_before if prio >= 2 else 2
        windows.append((max(lo, i - before), min(hi, i + after + 1), prio))
    windows.sort()
    merged: list[list[int]] = []
    for s, e, p in windows:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
            merged[-1][2] = max(merged[-1][2], p)
            merged[-1][3] += p
        else:
            merged.append([s, e, p, p])

    # Header (always included).
    header: list[str] = []
    header.append(f"Failing step: {failing.name}" if failing else "Failing step: (not identified)")
    if det.frameworks:
        header.append("Detected tooling: " + ", ".join(sorted(det.frameworks)))
    failures = [f for f in det.failures if f.line is None or lo <= f.line - 1 < hi] or det.failures
    if failures:
        header.append(f"Structured failures ({len(failures)}):")
        for f in failures[:25]:
            msg = f" — {f.message[:200]}" if f.message else ""
            header.append(f"  - [{f.framework}] {f.name}{msg} (line {f.line})")
        if len(failures) > 25:
            header.append(f"  - … {len(failures) - 25} more")
    if gh_errors:
        header.append("GitHub error annotations:")
        for e in gh_errors[:10]:
            header.append(f"  - {e[:300]}")
    header.append("")
    header.append("Log excerpt (line numbers refer to the full normalised log):")

    budget_chars = budget_tokens * 4
    used = sum(len(h) + 1 for h in header)

    # Rank windows: max priority, then total anchor weight, then recency (later wins).
    ranked = sorted(
        range(len(merged)), key=lambda k: (merged[k][2], merged[k][3], merged[k][0]), reverse=True
    )
    chosen: dict[int, list[str]] = {}
    truncated = False
    for k in ranked:
        s, e, _, _ = merged[k]
        idxs = [i for i in range(s, e) if i in keepable_set or i in anchors]
        block = _render(_compress_frames(idxs, lines), lines, protect)
        size = sum(len(b) + 1 for b in block)
        if used + size > budget_chars:
            truncated = True
            remaining = budget_chars - used
            if remaining > 400 and not chosen:  # nothing fits: keep the head of the best window
                cut, acc = [], 0
                for b in block:
                    if acc + len(b) + 1 > remaining:
                        break
                    cut.append(b)
                    acc += len(b) + 1
                chosen[k] = [*cut, "      … window truncated to fit budget"]
                used += acc
            continue
        chosen[k] = block
        used += size

    # Tail of the failing scope: often contains the final error / exit code.
    tail_idxs = [i for i in keepable if lo <= i < hi][-15:]
    covered = {i for k in chosen for i in range(merged[k][0], merged[k][1])}
    tail_idxs = [i for i in tail_idxs if i not in covered]
    tail_block = _render(tail_idxs, lines, protect) if tail_idxs else []
    tail_size = sum(len(b) + 1 for b in tail_block)
    if tail_block and used + tail_size > budget_chars:
        tail_block, truncated = [], True

    body: list[str] = []
    last_end = None
    for k in sorted(chosen, key=lambda k: merged[k][0]):
        s = merged[k][0]
        if last_end is not None and s > last_end:
            body.append(f"      … [{s - last_end} lines omitted]")
        elif last_end is None and s > 0:
            body.append(f"      … [{s} lines omitted]")
        body.extend(chosen[k])
        last_end = merged[k][1]
    if tail_block:
        body.append("      … [tail of failing step]")
        body.extend(tail_block)

    excerpt = "\n".join(header + body)
    kept = sum(1 for b in body if "| " in b[:8])
    return PrefilterResult(
        excerpt=excerpt,
        lines=lines,
        failing_step=failing.name if failing else None,
        failures=failures,
        frameworks=sorted(det.frameworks),
        gh_errors=gh_errors,
        original_lines=len(raw.splitlines()),
        kept_lines=kept,
        est_tokens=estimate_tokens(excerpt),
        truncated=truncated,
        redactions=redactor.count,
        steps=[s.name for s in steps],
    )
