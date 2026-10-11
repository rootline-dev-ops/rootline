"""Framework-specific failure detectors.

Each detector scans the normalised log lines and returns:
  * structured ``Failure`` records (what failed, where, with what message), and
  * "anchors": line indices worth keeping, with a priority (higher = more decisive).

Detectors are deliberately simple, regex-based and deterministic, so their behaviour is
easy to test and reason about. They never call the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Failure:
    framework: str
    name: str
    message: str = ""
    line: int | None = None  # 1-based line number in the normalised log

    def to_dict(self) -> dict:
        return {
            "framework": self.framework,
            "name": self.name,
            "message": self.message,
            "line": self.line,
        }


@dataclass
class Detection:
    failures: list[Failure] = field(default_factory=list)
    anchors: dict[int, int] = field(default_factory=dict)  # index -> priority
    frameworks: set[str] = field(default_factory=set)
    # how many lines after an anchor are worth keeping (e.g. a traceback)
    spans: dict[int, int] = field(default_factory=dict)

    def anchor(self, idx: int, priority: int, after: int | None = None) -> None:
        if priority > self.anchors.get(idx, 0):
            self.anchors[idx] = priority
        if after is not None and after > self.spans.get(idx, 0):
            self.spans[idx] = after

    def add(self, failure: Failure) -> None:
        key = (failure.framework, failure.name)
        if any((f.framework, f.name) == key for f in self.failures):
            return
        self.failures.append(failure)


def _next_nonblank(lines: list[str], i: int, limit: int = 4) -> str:
    for j in range(i + 1, min(len(lines), i + 1 + limit)):
        s = lines[j].strip()
        if s:
            return s
    return ""


# ---------------------------------------------------------------- pytest
_PY_SUMMARY = re.compile(r"^=+ .*\b(\d+ failed|\d+ errors?)\b.* in [\d.]+s.*=+$")
_PY_FAILED = re.compile(r"^(FAILED|ERROR) (\S+?\.py(?:::\S+)?)(?: - (.*))?$")
_PY_SECTION = re.compile(r"^_{3,} (\S.*?) _{3,}$")
_PY_E = re.compile(r"^E {2,}\S")
_PY_HEADER = re.compile(r"^=+ (FAILURES|ERRORS|short test summary info) =+$")


def detect_pytest(lines: list[str], d: Detection) -> None:
    for i, line in enumerate(lines):
        if _PY_HEADER.match(line) or _PY_SUMMARY.match(line):
            d.frameworks.add("pytest")
            d.anchor(i, 3, after=25 if "short test summary" in line else 3)
        elif m := _PY_FAILED.match(line):
            d.frameworks.add("pytest")
            d.anchor(i, 3)
            d.add(Failure("pytest", m.group(2), (m.group(3) or "").strip(), i + 1))
        elif (m := _PY_SECTION.match(line)) and (
            "test" in m.group(1).lower() or "::" in m.group(1) or m.group(1).startswith("ERROR")
        ):
            d.frameworks.add("pytest")
            d.anchor(i, 2, after=6)
        elif _PY_E.match(line):
            d.anchor(i, 3)


# ---------------------------------------------------------------- jest / vitest
_JEST_FAIL = re.compile(r"^\s*FAIL\s+(\S+\.(?:[cm]?[jt]sx?))\b")
_JEST_CASE = re.compile(r"^\s*●\s+(.+?)\s*$")
_JEST_SUMMARY = re.compile(r"^\s*Tests:\s+.*\b\d+ failed\b")


def _jest_message(lines: list[str], i: int) -> str:
    expected = received = ""
    for j in range(i + 1, min(len(lines), i + 12)):
        s = lines[j].strip()
        if s.startswith("Expected:") and not expected:
            expected = s
        elif s.startswith("Received:") and not received:
            received = s
        elif _JEST_CASE.match(lines[j]):
            break
    if expected and received:
        return f"{expected}; {received}"
    return _next_nonblank(lines, i)


def detect_jest(lines: list[str], d: Detection) -> None:
    current_file = ""
    for i, line in enumerate(lines):
        if m := _JEST_FAIL.match(line):
            d.frameworks.add("jest")
            current_file = m.group(1)
            d.anchor(i, 3, after=2)
        elif (m := _JEST_CASE.match(line)) and current_file:
            name = m.group(1)
            if name.startswith("Console") or name.startswith("Test suite failed to run"):
                if name.startswith("Test suite failed to run"):
                    d.add(Failure("jest", current_file, _next_nonblank(lines, i), i + 1))
                    d.anchor(i, 3, after=15)
                continue
            d.add(Failure("jest", f"{current_file} › {name}", _jest_message(lines, i), i + 1))
            d.anchor(i, 3, after=18)
        elif _JEST_SUMMARY.match(line):
            d.frameworks.add("jest")
            d.anchor(i, 2, after=3)


# ---------------------------------------------------------------- go
_GO_FAIL = re.compile(r"^\s*--- FAIL: (\S+) \(([\d.]+)s\)")
_GO_PKG_FAIL = re.compile(r"^FAIL\s+(\S+)\s+[\d.]+s$")
_GO_MSG = re.compile(r"^\s+(\S+_test\.go:\d+: .+)$")
_GO_BUILD = re.compile(r"^(?:\./)?(\S+\.go):(\d+):(\d+): (.+)$")
_GO_PANIC = re.compile(r"^panic: (.+)")


def detect_go(lines: list[str], d: Detection) -> None:
    for i, line in enumerate(lines):
        if m := _GO_FAIL.match(line):
            d.frameworks.add("go test")
            msg = ""
            for j in range(i + 1, min(len(lines), i + 6)):
                if mm := _GO_MSG.match(lines[j]):
                    msg = mm.group(1)
                    break
            if not msg:  # -v output prints t.Errorf lines before the --- FAIL line
                for j in range(i - 1, max(-1, i - 8), -1):
                    if mm := _GO_MSG.match(lines[j]):
                        msg = mm.group(1)
                        d.anchor(j, 3)
                        break
            d.add(Failure("go test", m.group(1), msg, i + 1))
            d.anchor(i, 3, after=6)
        elif m := _GO_PKG_FAIL.match(line):
            d.frameworks.add("go test")
            d.anchor(i, 2)
        elif m := _GO_PANIC.match(line):
            d.frameworks.add("go test")
            d.add(Failure("go test", "panic", m.group(1), i + 1))
            d.anchor(i, 3, after=20)
        elif m := _GO_BUILD.match(line):
            d.frameworks.add("go build")
            d.add(Failure("go build", f"{m.group(1)}:{m.group(2)}", m.group(4), i + 1))
            d.anchor(i, 3)


# ---------------------------------------------------------------- maven
_MVN_TESTS = re.compile(r"^\[ERROR\] Tests run: \d+, Failures: \d+, Errors: \d+")
_MVN_CASE = re.compile(r"^\[ERROR\]\s{2,}([\w.$]+)\.(\w+):(\d+)\s+(.*)$")
_MVN_CASE2 = re.compile(r"^\[ERROR\] (\w[\w.$]*)\s+Time elapsed: .*<<< (FAILURE|ERROR)!")
_MVN_CASE3 = re.compile(r"^\[ERROR\] (\w+)\((\w[\w.$]*)\)\s+Time elapsed: .*<<< (FAILURE|ERROR)!")
_MVN_GOAL = re.compile(r"^\[ERROR\] Failed to execute goal (\S+) .*?: (.*)$")
_MVN_COMPILE = re.compile(r"^\[ERROR\] (\S+\.(?:java|kt)):\[(\d+),(\d+)\] (.+)$")


def detect_maven(lines: list[str], d: Detection) -> None:
    for i, line in enumerate(lines):
        if not (line.startswith("[ERROR]") or "BUILD FAILURE" in line):
            continue
        d.frameworks.add("maven")
        if "BUILD FAILURE" in line or _MVN_TESTS.match(line):
            d.anchor(i, 2)
        elif m := _MVN_COMPILE.match(line):
            d.add(
                Failure("maven", f"{m.group(1).rsplit('/', 1)[-1]}:{m.group(2)}", m.group(4), i + 1)
            )
            d.anchor(i, 3)
        elif m := _MVN_CASE.match(line):
            d.add(Failure("maven", f"{m.group(1)}.{m.group(2)}", m.group(4), i + 1))
            d.anchor(i, 3)
        elif m := _MVN_CASE3.match(line):
            d.add(Failure("maven", f"{m.group(2)}.{m.group(1)}", _next_nonblank(lines, i), i + 1))
            d.anchor(i, 3, after=8)
        elif m := _MVN_CASE2.match(line):
            d.anchor(i, 3, after=8)
        elif m := _MVN_GOAL.match(line):
            d.add(Failure("maven", m.group(1), m.group(2)[:300], i + 1))
            d.anchor(i, 3)


# ---------------------------------------------------------------- gradle
_GRADLE_CASE = re.compile(r"^(\w[\w.$]*) > (.+) FAILED$")
_GRADLE_TASK = re.compile(r"^> Task (\S+) FAILED$")
_GRADLE_WRONG = re.compile(r"^\* What went wrong:")
_GRADLE_KT = re.compile(r"^e: (?:file://)?(\S+\.kts?):(\d+):(\d+):? (.+)$")


def detect_gradle(lines: list[str], d: Detection) -> None:
    for i, line in enumerate(lines):
        if m := _GRADLE_CASE.match(line):
            d.frameworks.add("gradle")
            d.add(
                Failure("gradle", f"{m.group(1)} > {m.group(2)}", _next_nonblank(lines, i), i + 1)
            )
            d.anchor(i, 3, after=6)
        elif m := _GRADLE_TASK.match(line):
            d.frameworks.add("gradle")
            d.anchor(i, 2)
        elif _GRADLE_WRONG.match(line):
            d.frameworks.add("gradle")
            d.anchor(i, 3, after=10)
        elif line.startswith("FAILURE: Build failed") or line.startswith("BUILD FAILED"):
            d.frameworks.add("gradle")
            d.anchor(i, 2)
        elif m := _GRADLE_KT.match(line):
            d.frameworks.add("gradle")
            d.add(
                Failure(
                    "kotlinc", f"{m.group(1).rsplit('/', 1)[-1]}:{m.group(2)}", m.group(4), i + 1
                )
            )
            d.anchor(i, 3)


# ---------------------------------------------------------------- npm / tsc
_NPM_CODE = re.compile(r"^npm (?:ERR!|error) code (\S+)")
_NPM_ERR = re.compile(r"^npm (?:ERR!|error) ")
_TSC = re.compile(r"^(\S+\.tsx?)(?:\((\d+),(\d+)\)|:(\d+):(\d+)) ?[:-] ?error (TS\d+): (.+)$")


def detect_npm(lines: list[str], d: Detection) -> None:
    for i, line in enumerate(lines):
        if m := _NPM_CODE.match(line):
            d.frameworks.add("npm")
            msg = ""
            for j in range(i + 1, min(len(lines), i + 4)):
                text = _NPM_ERR.sub("", lines[j]).strip()
                if text and not text.startswith("errno") and not text.startswith("code"):
                    msg = text
                    break
            d.add(Failure("npm", m.group(1), msg, i + 1))
            d.anchor(i, 3, after=25)
        elif _NPM_ERR.match(line):
            d.frameworks.add("npm")
            d.anchor(i, 2)
        elif m := _TSC.match(line):
            d.frameworks.add("tsc")
            ln = m.group(2) or m.group(4)
            d.add(Failure("tsc", f"{m.group(1)}:{ln}", f"{m.group(6)}: {m.group(7)}", i + 1))
            d.anchor(i, 3)


# ---------------------------------------------------------------- docker build
_DOCKER_STEP_ERR = re.compile(r"^#\d+ ERROR: (.+)$")
_DOCKER_SOLVE = re.compile(r"^ERROR: failed to (?:solve|build)(?::| with)? ?(.+)$")
_DOCKER_DF = re.compile(r"^Dockerfile(?:\.\w+)?:(\d+)$")
_DOCKER_STAGE = re.compile(r"^ > \[([^\]]+)\] (.+):$")


def detect_docker(lines: list[str], d: Detection) -> None:
    for i, line in enumerate(lines):
        if m := _DOCKER_STEP_ERR.match(line):
            d.frameworks.add("docker build")
            d.anchor(i, 3)
        elif m := _DOCKER_SOLVE.match(line):
            d.frameworks.add("docker build")
            d.add(Failure("docker build", "failed to solve", m.group(1)[:400], i + 1))
            d.anchor(i, 3)
        elif _DOCKER_DF.match(line):
            d.anchor(i, 2, after=12)
        elif _DOCKER_STAGE.match(line):
            d.anchor(i, 2, after=15)


# ---------------------------------------------------------------- python tracebacks & generic
_TRACEBACK = re.compile(r"^\s*Traceback \(most recent call last\):")
_PY_EXC = re.compile(r"^(?:[\w.]+\.)?(\w*(?:Error|Exception|Exit|Interrupt))(?:: (.+))?$")
_GH_ERROR = re.compile(r"^##\[error\](.*)$")
_GENERIC = [
    (re.compile(r"(?i)\b(?:fatal|panic)\b[:!]"), 2),
    (
        re.compile(
            r"(?i)\b(?:no space left on device|out of memory|oomkilled|killed signal|segmentation fault|ENOSPC)\b"
        ),
        3,
    ),
    (
        re.compile(
            r"(?i)\b(?:ECONNRESET|ETIMEDOUT|ECONNREFUSED|EAI_AGAIN|could not resolve host|rate limit|HTTP 429|503 Service Unavailable|TLS handshake timeout)\b"
        ),
        2,
    ),
    (re.compile(r"(?i)(?:^|\s)(?:error|err)(?:\[\w+\])?[:\]]\s"), 1),
    (re.compile(r"(?i)\b(?:timed out|timeout exceeded|deadline exceeded)\b"), 1),
    (re.compile(r"(?i)\bexit(?:ed)? (?:with )?(?:code|status) [1-9]\d*"), 1),
    (re.compile(r"(?i)\bcommand not found\b|\bpermission denied\b|\bnot found in registry\b"), 2),
]


def detect_generic(lines: list[str], d: Detection) -> None:
    for i, line in enumerate(lines):
        if _TRACEBACK.match(line):
            # keep the whole traceback up to the exception line
            end = i + 1
            while end < len(lines) and end - i < 80:
                if not lines[end].startswith((" ", "\t")) and lines[end].strip():
                    break
                end += 1
            d.anchor(i, 3, after=end - i)
            if end < len(lines) and (m := _PY_EXC.match(lines[end].strip())):
                d.add(Failure("python", m.group(1), (m.group(2) or "").strip(), end + 1))
                d.anchor(end, 3)
            continue
        if m := _GH_ERROR.match(line):
            d.anchor(i, 2)
            continue
        for k, (pattern, prio) in enumerate(_GENERIC):
            if m := pattern.search(line):
                d.anchor(i, prio)
                if k in (1, 2):  # resource exhaustion / network: strong infra signals
                    d.add(Failure("infra-signal", m.group(0), line.strip()[:300], i + 1))
                break


DETECTORS = [
    detect_pytest,
    detect_jest,
    detect_go,
    detect_maven,
    detect_gradle,
    detect_npm,
    detect_docker,
    detect_generic,
]


def detect(lines: list[str]) -> Detection:
    d = Detection()
    for fn in DETECTORS:
        fn(lines, d)
    return d
