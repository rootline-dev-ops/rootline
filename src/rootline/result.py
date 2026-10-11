"""The structured analysis result and its JSON schema (used for Claude structured output)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

CATEGORIES = ["regression", "flaky", "config_env", "dependency", "infra", "unknown"]
CONFIDENCE = ["high", "medium", "low"]
EVIDENCE_SOURCES = ["log", "diff", "file", "workflow", "history"]

ANALYSIS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "One short sentence: what failed."},
        "root_cause": {
            "type": "string",
            "description": "The underlying cause (not the symptom), 1-4 sentences, specific to files/lines/versions.",
        },
        "category": {"type": "string", "enum": CATEGORIES},
        "confidence": {"type": "string", "enum": CONFIDENCE},
        "evidence": {
            "type": "array",
            "description": "Concrete evidence supporting the root cause. Quote exact lines.",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "enum": EVIDENCE_SOURCES},
                    "reference": {
                        "type": "string",
                        "description": "Where: 'log L123', 'src/x.py:42', 'diff src/x.py', 'run 123456'.",
                    },
                    "excerpt": {
                        "type": "string",
                        "description": "Exact quoted text, one or a few lines.",
                    },
                },
                "required": ["source", "reference", "excerpt"],
                "additionalProperties": False,
            },
        },
        "suggested_fix": {
            "type": "string",
            "description": "Minimal, concrete fix in prose (Markdown allowed).",
        },
        "patch": {
            "type": "string",
            "description": "Unified diff (git format, a/ b/ paths) implementing the fix, or empty string if not confident.",
        },
    },
    "required": [
        "summary",
        "root_cause",
        "category",
        "confidence",
        "evidence",
        "suggested_fix",
        "patch",
    ],
    "additionalProperties": False,
}


@dataclass
class Evidence:
    source: str
    reference: str
    excerpt: str


@dataclass
class Analysis:
    summary: str
    root_cause: str
    category: str
    confidence: str
    evidence: list[Evidence] = field(default_factory=list)
    suggested_fix: str = ""
    patch: str = ""
    patch_note: str = ""  # set by validation, e.g. why a patch was dropped

    @classmethod
    def from_dict(cls, d: dict) -> Analysis:
        cat = str(d.get("category", "unknown")).strip().lower()
        conf = str(d.get("confidence", "low")).strip().lower()
        a = cls(
            summary=str(d.get("summary", "")).strip(),
            root_cause=str(d.get("root_cause", "")).strip(),
            category=cat if cat in CATEGORIES else "unknown",
            confidence=conf if conf in CONFIDENCE else "low",
            evidence=[
                Evidence(
                    source=str(e.get("source", "log")).lower()
                    if str(e.get("source", "")).lower() in EVIDENCE_SOURCES
                    else "log",
                    reference=str(e.get("reference", "")),
                    excerpt=str(e.get("excerpt", "")),
                )
                for e in (d.get("evidence") or [])
                if isinstance(e, dict)
            ][:12],
            suggested_fix=str(d.get("suggested_fix", "")).strip(),
            patch=str(d.get("patch", "") or "").strip("\n"),
        )
        a.validate_patch()
        return a

    def validate_patch(self) -> None:
        """Keep a patch only if it is structurally a unified diff."""
        if not self.patch.strip():
            self.patch = ""
            return
        p = self.patch
        has_headers = re.search(r"(?m)^--- (?:a/|/dev/null)", p) and re.search(
            r"(?m)^\+\+\+ (?:b/|/dev/null)", p
        )
        has_hunk = re.search(r"(?m)^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", p)
        if not (has_headers and has_hunk):
            self.patch_note = (
                "A patch was proposed but was not a well-formed unified diff, so it was omitted."
            )
            self.patch = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunReport:
    """Everything Rootline knows after a run: deterministic facts + (optional) analysis."""

    context: dict
    prefilter: dict
    failures: list[dict]
    failing_step: str | None
    analysis: Analysis | None = None
    model: str | None = None
    models_tried: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    tool_calls: list[dict] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["analysis"] = self.analysis.to_dict() if self.analysis else None
        return d
