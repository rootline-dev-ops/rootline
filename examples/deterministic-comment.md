<!-- Real output: `rootline analyze --log-file tests/fixtures/gha_pytest.log --no-llm` (deterministic mode, no API call) -->
<!-- rootline:report key=local -->
### 🔴 Rootline: `gha_pytest.log` failed

Failing step: `Run pytest -q --maxfail=5`

_Deterministic findings only (AI analysis not run)._

<details open><summary>Detected failures (2)</summary>

- `pytest` `tests/test_format.py::test_format_total_de_locale` — `AssertionError: assert '€1,234.50' == '1.234,50\xa0€'` (log L107)
- `pytest` `tests/test_format.py::test_format_total_tr_locale` — `AssertionError: assert 'TRY99.90' == '₺99,90'` (log L108)

</details>

<sub>Rootline v0.1.0 · 124 log lines → 26 sent · 2 secret-like values redacted</sub>

<sub>Deterministic log parsing only. Rootline never pushes changes.</sub>

