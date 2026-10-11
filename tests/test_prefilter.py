import pytest

from rootline.prefilter import estimate_tokens, normalize, prefilter

CASES = [
    # fixture, failing step, framework, expected failure names (substring), must-include lines
    (
        "gha_pytest.log",
        "Run pytest -q --maxfail=5",
        "pytest",
        ["test_format_total_de_locale", "test_format_total_tr_locale"],
        ["E       AssertionError: assert '€1,234.50'", "2 failed, 7 passed"],
    ),
    (
        "gha_jest.log",
        "Run npm test -- --ci",
        "jest",
        ["invoice.test.ts › formatTotal › formats EUR amounts with the customer locale"],
        ['Received: "€1,234.50"', "Tests:       1 failed"],
    ),
    (
        "go_test.log",
        None,
        "go test",
        ["TestTokenBucket_Refill", "TestTokenBucket_Burst"],
        [
            "bucket_test.go:47: after 1s refill: got 4 tokens, want 5",
            "FAIL\tgithub.com/acme-labs/ratelimit/internal/bucket",
        ],
    ),
    (
        "maven.log",
        None,
        "maven",
        ["OrderServiceTest.appliesVolumeDiscount", "maven-surefire-plugin"],
        ["expected: <90.00> but was: <100.00>", "BUILD FAILURE"],
    ),
    (
        "gradle.log",
        None,
        "gradle",
        ["DateParserTest > parsesIsoWeekDates()"],
        ["DateTimeParseException", "* What went wrong:", "DateParser.kt:23"],
    ),
    (
        "npm_eresolve.log",
        None,
        "npm",
        ["ERESOLVE"],
        ['peer react@"^17.0.0 || ^18.0.0" from react-datepicker@6.9.0'],
    ),
    (
        "docker_build.log",
        None,
        "docker build",
        ["failed to solve"],
        [
            "pg_config executable not found",
            ">>> RUN pip install --no-cache-dir -r requirements.txt",
        ],
    ),
    (
        "python_traceback.log",
        None,
        None,
        ["ModuleNotFoundError"],
        ["import weasyprint", "No module named 'weasyprint'"],
    ),
    (
        "infra_disk.log",
        None,
        "gradle",
        ["No space left on device"],
        ["java.io.IOException: No space left on device"],
    ),
]


@pytest.mark.parametrize("name,step,framework,failures,must", CASES, ids=[c[0] for c in CASES])
def test_fixture_extraction(fixture_text, name, step, framework, failures, must):
    res = prefilter(fixture_text(name))
    assert res.failing_step == step
    if framework:
        assert framework in res.frameworks
    names = [f.name for f in res.failures]
    for expected in failures:
        assert any(expected in n for n in names), (expected, names)
    for line in must:
        assert line in res.excerpt, line
    assert res.est_tokens <= 12000
    assert "\x1b" not in res.excerpt


def test_ansi_and_timestamps_removed(fixture_text):
    lines = normalize(fixture_text("gha_jest.log"))
    assert not any("\x1b" in line for line in lines)
    assert not any(line.startswith("2026-10-09T") for line in lines)
    assert lines[0] == "##[group]Run actions/setup-node@v4"  # BOM + timestamp stripped


def test_noise_is_dropped_from_excerpt(fixture_text):
    res = prefilter(fixture_text("gha_pytest.log"))
    assert "Downloading pydantic" not in res.excerpt
    assert "remote: Counting objects" not in res.excerpt
    assert "Collecting babel" not in res.excerpt
    # post-failure steps are out of scope
    assert "No files were found with the provided path" not in res.excerpt
    assert res.kept_lines < res.original_lines / 3


def test_step_env_block_dropped_and_secrets_redacted(fixture_text):
    res = prefilter(fixture_text("gha_pytest.log"))
    assert "s3cr3t-ci-pass" not in "\n".join(res.lines)
    assert "sk_test_EXAMPLExxxxxxxxxxxxxxxx" not in "\n".join(res.lines)
    assert "[REDACTED:stripe_key]" in res.excerpt
    assert "INVOICER_DB_URL" not in res.excerpt  # env: block of the step is noise
    assert res.redactions >= 2


def test_line_numbers_point_into_normalised_log(fixture_text):
    res = prefilter(fixture_text("gha_pytest.log"))
    for row in res.excerpt.splitlines():
        head, sep, text = row.partition("| ")
        if sep and head.strip().isdigit():
            assert res.lines[int(head) - 1] == text


def test_failing_step_hint_from_api(fixture_text):
    res = prefilter(fixture_text("gha_pytest.log"), failing_step_hint="pytest -q --maxfail=5")
    assert res.failing_step == "Run pytest -q --maxfail=5"


def test_stack_frames_compressed(fixture_text):
    res = prefilter(fixture_text("gha_jest.log"))
    assert "node_modules/jest-circus/build/run.js:316" not in res.excerpt
    assert "more stack frames (framework/internal) omitted" in res.excerpt
    assert "src/billing/__tests__/invoice.test.ts:42:33" in res.excerpt


def test_budget_is_respected_on_huge_log():
    noise = "\n".join(f"[INFO] compiling module {i} of 50000 ... ok" for i in range(50000))
    errors = "\n".join(
        f"src/m{i}.ts(10,5): error TS2345: Argument of type 'string' is not assignable"
        for i in range(400)
    )
    raw = noise + "\n" + errors + "\nError: Process completed with exit code 2.\n"
    res = prefilter(raw, budget_tokens=2000)
    assert res.truncated
    assert res.est_tokens <= 2000 * 1.05
    assert "tsc" in res.frameworks
    assert len(res.failures) == 400
    assert "Structured failures (400)" in res.excerpt
    assert "… 375 more" in res.excerpt


def test_repeated_lines_collapsed():
    raw = (
        "\n".join([f"Error: retrying connection to db (attempt {i})" for i in range(1, 30)])
        + "\n##[error]Process completed with exit code 1."
    )
    res = prefilter(raw)
    assert "similar lines collapsed, last one:" in res.excerpt
    assert "(attempt 29)" in res.excerpt
    assert res.excerpt.count("retrying connection") < 5


def test_no_anchors_falls_back_to_tail():
    raw = "\n".join(f"step output {i}" for i in range(200))
    res = prefilter(raw)
    assert "step output 199" in res.excerpt
    assert "step output 0" not in res.excerpt


def test_progress_bar_carriage_returns():
    lines = normalize("Downloading 10%\rDownloading 50%\rDownloading 100%\nnext")
    assert lines == ["Downloading 100%", "next"]


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd" * 10) == 10
