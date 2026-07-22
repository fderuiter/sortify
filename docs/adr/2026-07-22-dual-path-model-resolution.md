# Architecture Decision Record: Dual-Path Offline Model Resolution

## Context and Problem Statement
Our application is deployed in offline, air-gapped environments. The setup documentation advised users and administrators to extract offline model bundles to the global user configuration path (`~/.autosorter/model`), but the runtime specifically checked only the local workspace directory (`offline_bundle/model`). This resulted in the application failing to resolve the offline model for system administrators adhering to the documented steps, completely breaking the semantic sorting feature.

## Decision
We implemented a synchronous dual-path fallback strategy for resolving the generative model path. 
1. **Local Priority**: The system first attempts to locate the bundle in the local workspace directory (`offline_bundle/model`). This preserves the development workflow where developers can easily spin up test environments and isolate dependencies without touching global paths.
2. **Global Fallback**: If the local path is unpopulated, the runtime falls back to the user configuration directory (`~/.autosorter/model`).
3. **Synchronous Checking**: The path resolution executes synchronously at initialization within the UI `check_setup_wizard` and the Strategy `__init__` routines.

## Consequences
- **Positive**: Setup processes now match the documentation flawlessly. System administrators can deploy models globally, while developers maintain local overrides.
- **Positive**: The setup wizard automatically bypasses download prompts if models exist in either path, lowering setup friction.
- **Negative**: Adds minor I/O latency on initialization, but it is bounded strictly to two exact paths, preventing any directory traversal risk.
