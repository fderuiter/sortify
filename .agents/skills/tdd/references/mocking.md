# When to Mock

Mock at **system boundaries** only:

- External APIs (Hugging Face downloads, the security-advisory lookup, LLM backends)
- Heavy optional libraries behind the `[ml]` extra (torch, easyocr, transformers)
- Databases (sometimes; prefer a real temporary SQLCipher DB)
- Time/randomness
- File system (rarely; `tmp_path` is cheap and real)

Don't mock:

- Your own classes/modules
- Internal collaborators
- Anything you control

## Designing for Mockability

At system boundaries, design interfaces that are easy to mock:

**1. Use dependency injection**

Pass external dependencies in rather than creating them internally:

```python
# Easy to mock
def download_model(name: str, client: HubClient) -> Path:
    return client.fetch(name)


# Hard to mock
def download_model(name: str) -> Path:
    client = HubClient(token=os.environ["HF_TOKEN"])
    return client.fetch(name)
```

**2. Prefer SDK-style interfaces over generic fetchers**

Create specific functions for each external operation instead of one generic function with conditional logic:

```python
# GOOD: Each function is independently mockable
class AdvisoryClient:
    def get_advisories(self, model_id: str) -> list[Advisory]: ...
    def get_model_hash(self, model_id: str) -> str: ...


# BAD: Mocking requires conditional logic inside the mock
class AdvisoryClient:
    def request(self, path: str, **params) -> dict: ...
```

The SDK approach means:
- Each mock returns one specific shape
- No conditional logic in test setup
- Easier to see which endpoints a test exercises
- Type safety per endpoint

## In this repo

- Fake missing ML packages by inserting synthetic modules into `sys.modules` (see `monkeypatch.setitem(sys.modules, "torch", fake_torch)`), not by patching deep attribute paths inside `app/`.
- Never add a test-only branch to production code (`PYTEST_CURRENT_TEST`, `"unittest.mock" in sys.modules`). Inject the dependency instead, so the test runs the same path production runs.
