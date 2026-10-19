---
status: accepted (recorded retroactively on 2026-10-19; the rationale is inferred from the code, so correct it if it is wrong)
---

# Clustering runs in a spawned subprocess on an encrypted payload

The `default` strategy (`RecursiveKMeansStrategy` in `app/core/analyzer_strategies.py`) vectorises text with TF-IDF, or ONNX sentence embeddings when a model is present, and clusters with `MiniBatchKMeans`, recursing up to five levels. It runs in a `multiprocessing` "spawn" child that receives the documents as an encrypted payload. We chose MiniBatchKMeans over topic models such as NMF or LDA because it clusters large folders quickly with bounded memory and needs no topic count tuning beyond `max_folders`, and we isolate it in a spawned process so a native-library crash or memory blow-up in scikit-learn/ONNX cannot take down the TUI or the daemon, and so plaintext never crosses the process boundary.

## Considered options

- **NMF topic modelling.** The README still describes it; the code no longer uses it.
- **Running in a thread.** Rejected: a crash or memory spike in native code would kill the host process.
