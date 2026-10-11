> **Illustrative example, not a live model output.** The analysis JSON in
> [`illustrative-report.json`](illustrative-report.json) was written by hand for the fictional
> repository in `tests/fixtures/gha_pytest.log` to show the comment format. It was rendered with the
> real renderer (`rootline render --report examples/illustrative-report.json`). No live Claude run has
> been performed for this example.

---

<!-- rootline:report key=.github/workflows/ci.yml -->
### 🔴 Rootline: `CI / test (3.12)` failed

**🧩 Regression** · confidence: **high** · failing step: `Run pytest -q --maxfail=5`

> Two locale-formatting tests in tests/test_format.py fail after format_total() was refactored in this PR.

#### Root cause
The PR replaced `format_currency(total, invoice.currency, locale=invoice.locale)` with `format_currency(total, invoice.currency)` in `invoicer/format.py`. Babel then falls back to the process default locale (en_US on the runner), so de_DE and tr_TR invoices are formatted as `€1,234.50` / `TRY99.90` instead of `1.234,50 €` / `₺99,90`.

#### Suggested fix
Restore the `locale=invoice.locale` argument in `format_total()` (`invoicer/format.py`). If the default was removed on purpose, pass the locale explicitly from the callers instead.

<details><summary>Proposed patch (review before applying)</summary>

```diff
--- a/invoicer/format.py
+++ b/invoicer/format.py
@@ -14,4 +14,4 @@ def format_total(invoice: Invoice) -> str:
     total = sum(line.amount for line in invoice.lines)
-    return format_currency(total, invoice.currency)
+    return format_currency(total, invoice.currency, locale=invoice.locale)
```

</details>

<details><summary>Evidence</summary>

- **log** · log L89

  ```text
  E       AssertionError: assert '€1,234.50' == '1.234,50\xa0€'
  ```
- **diff** · diff invoicer/format.py

  ```text
  -    return format_currency(total, invoice.currency, locale=invoice.locale)
  +    return format_currency(total, invoice.currency)
  ```
- **history** · previous runs on main

  ```text
  Latest runs of CI on main concluded success; this is the first failure on the PR branch.
  ```

</details>

<details><summary>Detected failures (2)</summary>

- `pytest` `tests/test_format.py::test_format_total_de_locale` — `AssertionError: assert '€1,234.50' == '1.234,50\xa0€'` (log L107)
- `pytest` `tests/test_format.py::test_format_total_tr_locale` — `AssertionError: assert 'TRY99.90' == '₺99,90'` (log L108)

</details>

<sub>Rootline v0.1.0 · model `claude-sonnet-5-5` · 124 log lines → 26 sent · 3 tool calls · 2 secret-like values redacted · [run](https://github.com/acme-labs/invoicer/actions/runs/0)</sub>

<sub>AI-generated analysis: verify before acting. Rootline never pushes changes.</sub>

