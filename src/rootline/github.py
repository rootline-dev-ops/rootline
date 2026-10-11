"""Minimal GitHub REST API client (only the endpoints Rootline needs)."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any

import requests

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


@dataclass
class GitHub:
    token: str | None
    repo: str  # "owner/name"
    api_url: str = API
    session: requests.Session | None = None
    timeout: float = 30.0

    @classmethod
    def from_env(cls, repo: str, token: str | None = None) -> GitHub:
        return cls(
            token=token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
            repo=repo,
            api_url=os.environ.get("GITHUB_API_URL", API).rstrip("/"),
        )

    def _s(self) -> requests.Session:
        if self.session is None:
            self.session = requests.Session()
        return self.session

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        h = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "rootline",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def request(
        self, method: str, path: str, *, accept: str | None = None, **kw: Any
    ) -> requests.Response:
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        resp = self._s().request(
            method,
            url,
            headers=self._headers(accept or "application/vnd.github+json"),
            timeout=self.timeout,
            **kw,
        )
        if resp.status_code >= 400:
            msg = resp.text[:300]
            raise GitHubError(f"GitHub API {method} {path} -> {resp.status_code}: {msg}")
        return resp

    def get_json(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw).json()

    # ------------------------------------------------------------------ runs & logs
    def get_run(self, run_id: int) -> dict:
        return self.get_json(f"/repos/{self.repo}/actions/runs/{run_id}")

    def list_jobs(self, run_id: int) -> list[dict]:
        data = self.get_json(
            f"/repos/{self.repo}/actions/runs/{run_id}/jobs",
            params={"filter": "latest", "per_page": 100},
        )
        return data.get("jobs", [])

    def get_job_log(self, job_id: int) -> str:
        # Returns a 302 to a short-lived signed URL; requests follows it and drops the
        # Authorization header on the cross-host redirect.
        resp = self.request(
            "GET", f"/repos/{self.repo}/actions/jobs/{job_id}/logs", allow_redirects=True
        )
        resp.encoding = resp.encoding or "utf-8"
        return resp.text

    def list_workflow_runs(
        self, workflow_id: int | str, *, branch: str | None = None, limit: int = 10
    ) -> list[dict]:
        params: dict[str, Any] = {"per_page": min(max(limit, 1), 50)}
        if branch:
            params["branch"] = branch
        data = self.get_json(
            f"/repos/{self.repo}/actions/workflows/{workflow_id}/runs", params=params
        )
        return data.get("workflow_runs", [])

    # ------------------------------------------------------------------ code
    def get_file(self, path: str, ref: str) -> str:
        data = self.get_json(f"/repos/{self.repo}/contents/{path.lstrip('/')}", params={"ref": ref})
        if isinstance(data, list):
            names = ", ".join(sorted(x.get("name", "") for x in data)[:100])
            return f"[directory listing] {names}"
        if data.get("encoding") == "base64" and data.get("content") is not None:
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        # large files: fall back to the raw media type
        return self.request(
            "GET",
            f"/repos/{self.repo}/contents/{path.lstrip('/')}",
            params={"ref": ref},
            accept="application/vnd.github.raw+json",
        ).text

    # ------------------------------------------------------------------ pull requests
    def find_pr_for_run(self, run: dict) -> int | None:
        prs = run.get("pull_requests") or []
        if prs:
            return int(prs[0]["number"])
        sha = run.get("head_sha")
        if sha:
            try:
                pulls = self.get_json(f"/repos/{self.repo}/commits/{sha}/pulls")
                for p in pulls:
                    if p.get("state") == "open":
                        return int(p["number"])
            except GitHubError:
                pass
        # Fork PRs: run.pull_requests is empty; match on head owner:branch.
        head_repo = (run.get("head_repository") or {}).get("owner", {}).get("login")
        branch = run.get("head_branch")
        if head_repo and branch:
            pulls = self.get_json(
                f"/repos/{self.repo}/pulls",
                params={"head": f"{head_repo}:{branch}", "state": "open"},
            )
            for p in pulls:
                if p.get("head", {}).get("sha") == sha:
                    return int(p["number"])
        return None

    def get_pr_diff(self, number: int) -> str:
        return self.request(
            "GET", f"/repos/{self.repo}/pulls/{number}", accept="application/vnd.github.diff"
        ).text

    def get_pr(self, number: int) -> dict:
        return self.get_json(f"/repos/{self.repo}/pulls/{number}")

    # ------------------------------------------------------------------ comments
    def list_issue_comments(self, number: int) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            batch = self.get_json(
                f"/repos/{self.repo}/issues/{number}/comments",
                params={"per_page": 100, "page": page},
            )
            out.extend(batch)
            if len(batch) < 100 or page >= 20:
                return out
            page += 1

    def upsert_comment(self, number: int, body: str, marker: str) -> tuple[str, dict]:
        """Create the Rootline comment, or update the existing one carrying `marker`.

        Returns ("created"|"updated", comment_json).
        """
        for c in self.list_issue_comments(number):
            # Only comments that *start* with the marker count, so quoting the marker
            # elsewhere (or in a code block) cannot hijack the update.
            if not (c.get("body") or "").startswith(marker):
                continue
            try:
                resp = self.request(
                    "PATCH", f"/repos/{self.repo}/issues/comments/{c['id']}", json={"body": body}
                )
            except GitHubError as e:
                if " -> 403" in str(e) or " -> 404" in str(e):
                    continue  # someone else's comment carrying our marker: ignore it
                raise
            return "updated", resp.json()
        resp = self.request(
            "POST", f"/repos/{self.repo}/issues/{number}/comments", json={"body": body}
        )
        return "created", resp.json()
