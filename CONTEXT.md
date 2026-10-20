# Sortify

Sortify (shipped as "Smart AutoSorter AI Pro", command `smart-autosorter`) is a local, offline-first app that reads the files in a folder, decides where each one belongs, and moves them into a generated folder tree that the user can undo.

## Language

### Sorting

**Source folder**:
The folder the user asks Sortify to sort. Files are discovered in it, and by default moved within it.
_Avoid_: input dir, root, base path (in prose)

**Destination folder**:
Where sorted files land when the user passes `--dest-dir`; otherwise the source folder.
_Avoid_: output dir, target root

**Sort**:
One end-to-end run: discover, metadata pre-pass, extract, plan, verify, execute.
_Avoid_: organize, job, run (alone)

**Session**:
The record of one sort's moves, identified by a session id and kept as rows in the app store (ADR 0008). A session is the unit of undo.
_Avoid_: transaction, batch

**Plan**:
The full list of proposed moves (source path → target path) for a sort, produced before anything moves. A dry run or `scan` stops at the plan.
_Avoid_: sorting plan, move list, mapping

**Move**:
One file's relocation from its source path to its target path, recorded in the ledger before it happens.
_Avoid_: transfer, operation

**Preset**:
A named starting folder for a sort (`demo`, `downloads`, `documents`).
_Avoid_: profile, template

**Conflict policy**:
What a move does when its target path is already taken: `skip`, `rename` or `overwrite`. Under ADR 0005, `overwrite` moves the existing file aside so undo can restore it (#766).
_Avoid_: collision mode

### Routing and clustering

**Routing**:
Deciding a file's target folder by fixed rules, in priority order: locked path, compliance policy, sensitivity, keyword and learned rules, extension category, history. Files that no rule routes go to clustering.
_Avoid_: classification (that word belongs to sensitivity)

**Bypassed file**:
A file routed by the metadata pre-pass from its name or extension alone, so its text is never extracted.
_Avoid_: skipped file

**Locked path**:
A user-pinned target for a file that overrides every other routing step.
_Avoid_: pinned file, override

**Keyword rule**:
A user or rule-pack rule that maps filename or text keywords to a target folder.
_Avoid_: filter, Hazel rule (that is an import source)

**Learned rule**:
A filename or keyword rule kept apart from keyword rules (`LEARNED_RULES`) to hold what the user taught Sortify by correcting earlier plans.
_Avoid_: trained rule, feedback rule

**Clustering**:
Grouping the files no rule routed by text similarity (TF-IDF or ONNX embeddings, then MiniBatchKMeans), recursively, into folders.
_Avoid_: topic modelling, NMF

**Strategy**:
A pluggable clustering-and-naming algorithm chosen with `--strategy` (`default`, `generative`, `clinical_tmf`, `clinical_isf`).
_Avoid_: engine, mode

**Folder name**:
The label a strategy gives a cluster: its top terms joined with `-`, or an LLM-generated name under the `generative` strategy. Clusters too small to name go to `Miscellaneous`.
_Avoid_: category, tag

**Max folders**:
The cap on how many clusters one level of clustering may create.
_Avoid_: k, cluster count

### Safety and compliance

**Sensitivity**:
The JEV classifier's rating of how sensitive a file's content is. HIGH and CRITICAL files are not sorted normally; they go to quarantine.
_Avoid_: risk score, confidentiality

**Compliance policy**:
An ordered, user-configured rule (`POLICIES`) that routes or blocks files before sensitivity or keyword rules are consulted.
_Avoid_: policy (alone, in code reviews where "policy" also means conflict policy)

**Quarantine**:
Where Sortify holds files it must not sort normally: sensitive files during a sort (`_Compliance_Quarantine`) and incoming files the daemon has not yet cleared (`_Quarantine_Staging`). The user reviews and releases them in the Quarantine Vault.
_Avoid_: jail, holding area

**Ledger**:
The durable, append-before-act journal of every move, used to reconcile interrupted sessions and to undo.
_Avoid_: log, journal, history (each names a different store in code today; see issue #714)

**Undo**:
Restoring every file moved in a session to its original path, at the user's request.
_Avoid_: revert, restore

**Rollback**:
An undo that Sortify starts itself because a sort failed partway.
_Avoid_: auto-undo, unwind (the function name, not the concept)

**Simulation**:
A dry run of the plan against a virtual filesystem that reports collisions, broken links and unsafe paths before anything moves.
_Avoid_: preview, dry-run (as a noun)

### Runtime

**Daemon**:
The long-running watcher that sorts a folder as files arrive, controlled over local JSON-RPC.
_Avoid_: service, watcher (alone), background mode

**Drop**:
Handing specific files to Sortify to sort into an existing tree (`smart-autosorter drop`, the Drop Zone in the TUI).
_Avoid_: import, ingest

**Plugin**:
A Python package registered through `register_plugin(registry)` that adds strategies, quarantine hooks, TUI views or settings. `clinical_compliance` is the built-in one.
_Avoid_: extension, add-on

**TMF / ISF**:
Trial Master File and Investigator Site File: the clinical-trial document taxonomies that the `clinical_compliance` plugin sorts into.

**Offline bundle**:
The pre-downloaded, hash-verified set of model weights that lets the ML features run without network access.
_Avoid_: model cache
