---
status: accepted (recorded retroactively on 2026-10-19; the rationale is inferred from the code, so correct it if it is wrong)
---

# File contents never leave the machine

Sortify extracts, classifies, clusters and names files entirely on the user's machine. The only outbound traffic is fetching model weights from Hugging Face (checked against `app/core/hashes_registry.py`) and the security-advisory lookup; sandboxed worker threads have `socket.connect` patched to block anything else (`app/core/shared_registry.py`), and the offline bundle lets the ML features run with no network at all. We chose this over hosted OCR/LLM APIs because users sort documents (clinical trial files, finances, PHI) that they cannot send to a third party.

## Consequences

- A new feature that needs a model ships the model in the offline bundle or downloads it with a pinned hash. A feature that calls a hosted API with file content contradicts this ADR.
- `network_rules.json` only feeds generated docs; it is not the enforcement point.
