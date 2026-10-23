# Good and Bad Tests

Examples use pytest, the runner this repo uses. Function names are illustrative, not real APIs.

## Good Tests

**Integration-style**: Test through real interfaces, not mocks of internal parts.

```python
# GOOD: Tests observable behavior
def test_sort_moves_invoices_into_finance_folder(tmp_path):
    (tmp_path / "invoice_042.txt").write_text("Invoice total due: $1,200")
    result = sort_directory(tmp_path, preset="finance")
    assert (tmp_path / "Finance" / "invoice_042.txt").exists()
    assert result.moved == 1
```

Characteristics:

- Tests behavior users/callers care about
- Uses public API only
- Survives internal refactors
- Describes WHAT, not HOW
- One logical assertion per test

## Bad Tests

**Implementation-detail tests**: Coupled to internal structure.

```python
# BAD: Tests implementation details
def test_sort_calls_resilient_move(mocker, tmp_path):
    spy = mocker.patch("app.core.mover.resilient_move")
    sort_directory(tmp_path)
    spy.assert_called_once()
```

Red flags:

- Mocking internal collaborators
- Testing private methods
- Asserting on call counts/order
- Test breaks when refactoring without behavior change
- Test name describes HOW not WHAT
- Verifying through external means instead of interface

```python
# BAD: Bypasses interface to verify
def test_undo_deletes_ledger_row(db):
    session = run_sort(db)
    undo(session.id)
    assert db.execute("SELECT * FROM transaction_ledger").fetchall() == []


# GOOD: Verifies through interface
def test_undo_restores_original_paths(tmp_path):
    original = tmp_path / "report.docx"
    original.write_bytes(b"...")
    session = run_sort(tmp_path)
    undo(session.id)
    assert original.exists()
```

**Tautological tests**: Expected value restates the implementation, so the test passes by construction.

```python
# BAD: Expected value is recomputed the way the code computes it
def test_folder_name_joins_top_terms():
    terms = ["invoice", "tax"]
    assert name_cluster(terms) == "-".join(terms[:2])


# GOOD: Expected value is an independent, known literal
def test_folder_name_joins_top_terms():
    assert name_cluster(["invoice", "tax", "receipt"]) == "invoice-tax"
```
