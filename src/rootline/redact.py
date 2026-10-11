"""Best-effort secret redaction applied to everything before it is sent to the model.

This is a defence-in-depth layer, not a guarantee. GitHub already masks registered
secrets as ``***`` in Actions logs; this pass additionally catches well-known token
formats, credentials in URLs, private keys and ``key=value`` pairs with secret-looking
names that can leak through test output, debug prints, diffs or file contents.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# (name, pattern, group-to-replace or 0 for whole match)
_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        0,
    ),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,255}\b"), 0),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b"), 0),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), 0),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"), 0),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), 0),
    ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"), 0),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), 0),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"), 0),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), 0),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        0,
    ),
    (
        "auth_header",
        re.compile(
            r"(?i)\b(?:authorization|proxy-authorization)\s*[:=]\s*(?:bearer|basic|token)\s+([^\s'\"]+)"
        ),
        1,
    ),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{16,})"), 1),
    ("url_credentials", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:([^@\s/]+)@"), 1),
    (
        "secret_assignment",
        re.compile(
            r"(?i)\b[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
            r"private[_-]?key|client[_-]?secret|auth[_-]?key|credentials?)(?![A-Za-z0-9])"
            r"[\"']?\s*[:=]\s*[\"']?([^\s\"',;}{]{6,})"
        ),
        1,
    ),
]

# Values that are obviously not secrets; keeps `token: ${{ secrets.X }}` etc. readable.
_SAFE_VALUE = re.compile(
    r"^(?:\*+|\$\{\{.*|\$\{?[A-Z_][A-Z0-9_]*\}?|<[^>]*>|\[REDACTED[^\]]*\]|true|false|null|none|"
    r"required|optional|string|undefined|xxx+|changeme|example|placeholder|secrets\..*|env\..*)$",
    re.IGNORECASE,
)


@dataclass
class Redactor:
    """Redacts secrets from text. Keeps a running count of replacements."""

    extra_patterns: list[re.Pattern[str]] = field(default_factory=list)
    literal_values: list[str] = field(default_factory=list)
    count: int = 0

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Redactor:
        env = os.environ if environ is None else environ
        extra = [
            re.compile(p) for p in env.get("ROOTLINE_REDACT_PATTERNS", "").splitlines() if p.strip()
        ]
        # Literal values of secret-looking environment variables present in this process
        # (e.g. the API keys Rootline itself was given). Never send these anywhere.
        literals = []
        for name, value in env.items():
            if (
                value
                and len(value) >= 8
                and re.search(r"(?i)(token|secret|password|api_key|apikey|private_key)", name)
            ):
                literals.append(value)
        return cls(
            extra_patterns=extra, literal_values=sorted(set(literals), key=len, reverse=True)
        )

    def redact(self, text: str) -> str:
        if not text:
            return text
        for value in self.literal_values:
            if value in text:
                self.count += text.count(value)
                text = text.replace(value, "[REDACTED:env_secret]")
        for name, pattern, group in _PATTERNS:
            text = pattern.sub(lambda m, n=name, g=group: self._replace(m, n, g), text)
        for pattern in self.extra_patterns:
            text = pattern.sub(lambda m: self._replace(m, "custom", 0), text)
        return text

    def _replace(self, match: re.Match[str], name: str, group: int) -> str:
        if group == 0:
            self.count += 1
            return f"[REDACTED:{name}]"
        value = match.group(group)
        if value is None or _SAFE_VALUE.match(value) or "(" in value:
            return match.group(0)
        self.count += 1
        start, end = match.span(group)
        base = match.start(0)
        whole = match.group(0)
        return whole[: start - base] + f"[REDACTED:{name}]" + whole[end - base :]


def redact(text: str) -> str:
    """Convenience one-shot redaction with default patterns (no env literals)."""
    return Redactor().redact(text)
