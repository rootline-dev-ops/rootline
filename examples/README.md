# Examples

| File | What it is | Produced how |
|---|---|---|
| [`prefilter-output.txt`](prefilter-output.txt) | The exact, redacted excerpt that would be sent to Claude for `tests/fixtures/gha_pytest.log` | **Real output** of `rootline prefilter --log-file tests/fixtures/gha_pytest.log` |
| [`deterministic-comment.md`](deterministic-comment.md) | PR comment in deterministic mode (no model call) | **Real output** of `rootline analyze --log-file tests/fixtures/gha_pytest.log --no-llm` |
| [`illustrative-report.json`](illustrative-report.json) | A full report including an AI analysis | **Hand-written** analysis for illustration (deterministic fields are real) |
| [`illustrative-claude-comment.md`](illustrative-claude-comment.md) | What a comment with an AI analysis looks like | Real renderer applied to the hand-written JSON above: **illustrative, not a live Claude output** |
| [`workflows/rootline.yml`](workflows/rootline.yml) | Workflow to add Rootline to a repository | Copy into `.github/workflows/` |

The fixture logs in `tests/fixtures/` were written for this project to mimic real GitHub Actions output
(timestamps, ANSI colours, `##[group]` markers). The repositories they mention (`acme-labs/...`) are
fictional.

**Status:** as of the first commit no live run against the Anthropic API has been performed; the agent
loop is covered by tests with a mocked client and by a wire-level test that drives the official
`anthropic` SDK against a local stub server.
