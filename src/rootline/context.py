"""Context providers: where the agent's tools get repository data from.

* ``GitHubContext``: a real workflow run, read through the GitHub REST API (read-only).
* ``LocalContext``: a local log file plus an optional local checkout (for the CLI and
  for trying Rootline without a GitHub token).

Both expose the same small interface, so the agent and its tools do not care which one
is in use. Nothing here executes repository code.
"""

from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

from .github import GitHub, GitHubError

# Files the agent is never allowed to read, even if it asks.
DENY_GLOBS = [
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    "*secret*",
    "*credential*",
    ".git/*",
]


class ContextUnavailable(Exception):
    """Raised by a provider when a piece of context does not exist in this mode."""


def check_path_allowed(path: str) -> str:
    p = PurePosixPath(path.strip().lstrip("/"))
    if not str(p) or str(p) == "." or ".." in p.parts:
        raise PermissionError(f"path not allowed: {path!r}")
    for part in [p.name, str(p)]:
        for g in DENY_GLOBS:
            if fnmatch.fnmatch(part.lower(), g):
                raise PermissionError(
                    f"reading {path!r} is blocked by Rootline's sensitive-file denylist"
                )
    return str(p)


class ContextProvider(Protocol):
    def describe(self) -> dict: ...
    def get_file(self, path: str) -> str: ...
    def get_pr_diff(self) -> str: ...
    def get_workflow_yaml(self) -> str: ...
    def list_previous_runs(self, limit: int) -> list[dict]: ...


@dataclass
class GitHubContext:
    gh: GitHub
    run: dict
    job: dict | None = None
    pr_number: int | None = None
    _diff: str | None = field(default=None, repr=False)

    def describe(self) -> dict:
        r = self.run
        d = {
            "mode": "github",
            "repository": self.gh.repo,
            "workflow": r.get("name"),
            "workflow_path": r.get("path"),
            "run_id": r.get("id"),
            "run_attempt": r.get("run_attempt"),
            "run_url": r.get("html_url"),
            "event": r.get("event"),
            "head_branch": r.get("head_branch"),
            "head_sha": r.get("head_sha"),
            "commit_message": ((r.get("head_commit") or {}).get("message") or "").split("\n")[0][
                :200
            ],
            "pull_request": self.pr_number,
        }
        if self.job:
            d["job"] = self.job.get("name")
            d["job_url"] = self.job.get("html_url")
            d["runner"] = ", ".join(self.job.get("labels") or [])
        return d

    def get_file(self, path: str) -> str:
        path = check_path_allowed(path)
        return self.gh.get_file(path, ref=self.run["head_sha"])

    def get_pr_diff(self) -> str:
        if not self.pr_number:
            raise ContextUnavailable("this run is not associated with an open pull request")
        if self._diff is None:
            self._diff = self.gh.get_pr_diff(self.pr_number)
        return self._diff

    def get_workflow_yaml(self) -> str:
        path = self.run.get("path")
        if not path:
            raise ContextUnavailable("workflow path unknown")
        # `path` can be "dynamic/..." for some run types; strip any "@ref" suffix.
        return self.gh.get_file(path.split("@")[0], ref=self.run["head_sha"])

    def list_previous_runs(self, limit: int) -> list[dict]:
        wf = self.run.get("workflow_id")
        if not wf:
            raise ContextUnavailable("workflow id unknown")
        out = []
        branches = [self.run.get("head_branch")]
        default = (self.run.get("repository") or {}).get("default_branch")
        if default and default not in branches:
            branches.append(default)
        seen = set()
        for br in branches:
            try:
                runs = self.gh.list_workflow_runs(wf, branch=br, limit=limit)
            except GitHubError as e:
                raise ContextUnavailable(str(e)) from e
            for r in runs:
                if r["id"] in seen or r["id"] == self.run.get("id"):
                    continue
                seen.add(r["id"])
                out.append(
                    {
                        "id": r["id"],
                        "branch": r.get("head_branch"),
                        "sha": (r.get("head_sha") or "")[:10],
                        "event": r.get("event"),
                        "conclusion": r.get("conclusion") or r.get("status"),
                        "attempt": r.get("run_attempt"),
                        "created_at": r.get("created_at"),
                        "title": (r.get("display_title") or "")[:120],
                    }
                )
        out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return out[: limit * 2]


@dataclass
class LocalContext:
    log_path: str
    repo_dir: Path | None = None
    base_ref: str | None = None
    workflow_file: str | None = None

    def describe(self) -> dict:
        d: dict = {"mode": "local", "log_file": Path(self.log_path).name}
        if self.repo_dir:
            d["repo_dir"] = str(self.repo_dir)
            try:
                d["head_sha"] = self._git("rev-parse", "HEAD").strip()
                d["head_branch"] = self._git("rev-parse", "--abbrev-ref", "HEAD").strip()
            except ContextUnavailable:
                pass
        if self.base_ref:
            d["diff_base"] = self.base_ref
        d["generated_at"] = datetime.now().isoformat(timespec="seconds")
        return d

    def _git(self, *args: str) -> str:
        if not self.repo_dir:
            raise ContextUnavailable("no local repository given (--repo-dir)")
        try:
            return subprocess.run(
                ["git", "-C", str(self.repo_dir), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
            raise ContextUnavailable(f"git {' '.join(args)} failed: {e}") from e

    def get_file(self, path: str) -> str:
        if not self.repo_dir:
            raise ContextUnavailable("no local repository given (--repo-dir)")
        rel = check_path_allowed(path)
        root = self.repo_dir.resolve()
        target = (root / rel).resolve()
        if root not in target.parents and target != root:
            raise PermissionError("path escapes the repository")
        if target.is_dir():
            return "[directory listing] " + ", ".join(
                sorted(p.name for p in target.iterdir())[:100]
            )
        if not target.exists():
            raise FileNotFoundError(f"{rel} does not exist")
        return target.read_text(encoding="utf-8", errors="replace")

    def get_pr_diff(self) -> str:
        if not self.base_ref:
            raise ContextUnavailable("no diff base given (--base, e.g. origin/main)")
        return self._git("diff", f"{self.base_ref}...HEAD")

    def get_workflow_yaml(self) -> str:
        if self.workflow_file:
            return Path(self.workflow_file).read_text(encoding="utf-8", errors="replace")
        if self.repo_dir and (self.repo_dir / ".github" / "workflows").is_dir():
            files = sorted((self.repo_dir / ".github" / "workflows").glob("*.y*ml"))
            if len(files) == 1:
                return files[0].read_text(encoding="utf-8", errors="replace")
            names = ", ".join(f.name for f in files)
            raise ContextUnavailable(f"several workflows found ({names}); pass --workflow-file")
        raise ContextUnavailable("no workflow file available (--workflow-file)")

    def list_previous_runs(self, limit: int) -> list[dict]:
        raise ContextUnavailable("run history is only available in GitHub mode")
