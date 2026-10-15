# Terminal-Native Architecture Report Format

The architectural review is rendered as a structured Markdown file (`ARCHITECTURE-REPORT.md` or printed to terminal stdout) formatted for standard terminal viewports (80–120 columns). All visual diagrams use ASCII/Unicode box-drawing characters and plain-text layouts. No HTML rendering, external browser openers, or CDN asset scripts are permitted.

## Scaffold

```markdown
# Architecture Review: <repo_name>

**Date:** YYYY-MM-DD
**Legend:** `┌──┐` Module · `┊  ┊` Seam · `──►` Call · `==►` Leak/Friction · `█` Deep Module

---

## Candidates

<candidate_sections>

---

## Top Recommendation

<top_recommendation_card>
```

## Header

Repo name, date, and a compact legend:
- `┌──┐` Module
- `┊  ┊` Seam
- `──►` Normal call path
- `==►` Leakage / coupling friction
- `█` Deep module

No introduction paragraph. Straight into the candidates.

## Candidate Card Format

The ASCII/Unicode diagrams carry the weight. Prose is sparse, plain, and uses vocabulary terms from the `/codebase-design` skill without ceremony.

Each candidate is structured as a Markdown section:

### 1. <Candidate Name>

**Recommendation:** `[Strong]` | `[Worth exploring]` | `[Speculative]`  
**Category:** `[in-process]` | `[local-substitutable]` | `[ports & adapters]` | `[mock]`  
**Files:** `path/to/module_a.py`, `path/to/module_b.py`, `path/to/module_c.py`

#### Before / After Visualizations

```
BEFORE (Shallow Wrappers & Leakage)          AFTER (Deepened Module)
┌──────────────────┐                         ┌──────────────────────────────┐
│  OrderHandler    │                         │  Order Intake Module         │
└────────┬─────────┘                         │  (Deep Interface)            │
         │                                   │ ┌──────────────────────────┐ │
┌────────▼─────────┐                         │ │ Internal Validation      │ │
│  OrderValidator  │                         │ ├──────────────────────────┤ │
└────────┬─────────┘                         │ │ Internal Persistence     │ │
         │                                   │ └──────────────────────────┘ │
┌────────▼─────────┐   ==► Leak          │                              │
│  OrderRepo       ├══════════════► [Pricing]└──────────────┬───────────────┘
└──────────────────┘                                        │
                                                   ┌────────▼────────┐
                                                   │ Pricing Adapter │
                                                   └─────────────────┘
```

- **Problem:** One-sentence description of what hurts or leaks.
- **Solution:** One-sentence description of the proposed architectural deepening.
- **Wins:**
  - `locality`: Bugs concentrate in one module
  - `leverage`: Single interface handles N call sites
  - `depth`: Interface shrinks while implementation absorbs shallow wrappers
- **ADR Callout** *(if applicable)*:
  > ⚠️ **ADR Conflict:** Contradicts `ADR-0007`, but worth reopening because pricing leakage hurts testability.

## ASCII/Unicode Diagram Patterns

Choose the diagram pattern that best fits the candidate. Maintain 80-120 column terminal formatting.

### 1. Dependency & Call Flow (Before vs After)

Use box-and-arrow ASCII/Unicode diagrams for call graphs and dependency chains.

```
BEFORE:
┌──────────┐     ┌──────────┐     ┌──────────┐
│ Client A ├────►│ Service  ├────►│ Client B │
└──────────┘     └────┬─────┘     └──────────┘
                      │ (Leak)
                      ▼
               ┌────────────┐
               │ Storage DB │
               └────────────┘

AFTER:
┌──────────┐     ┌───────────────────────┐
│ Client A ├────►│ Deep Core Engine      │
└──────────┘     │ ┌───────────────────┐ │
                 │ │ Internal State    │ │
                 │ └───────────────────┘ │
                 └───────────┬───────────┘
                             │
                      ┌──────▼──────┐
                      │ Storage DB  │
                      └─────────────┘
```

### 2. Mass / Depth Diagram (Shallow vs Deep Surface)

Illustrate surface area relative to implementation complexity.

```
BEFORE (Shallow: Wide surface, thin implementation):
┌────────────────────────────────────────────────────────┐
│ Interface: do_a(), do_b(), do_c(), do_d(), do_e()     │
├────────────────────────────────────────────────────────┤
│ Implementation: 1-line forwarding wrappers             │
└────────────────────────────────────────────────────────┘

AFTER (Deep: Small surface, thick implementation):
┌────────────────────────────────┐
│ Interface: process()           │
├────────────────────────────────┤
│ Implementation:                │
│  - Internal validation         │
│  - Orchestration & retry       │
│  - State persistence           │
└────────────────────────────────┘
```

### 3. Layer Cross-Section

Show multi-layer call passing collapsed into a single boundary.

```
BEFORE: [Caller] ──► [Controller] ──► [Service] ──► [Manager] ──► [Dao]
AFTER:  [Caller] ──► [ Deep Domain Module ]
```

## Style Guidance

- **Terminal Columns:** Keep code blocks and box diagrams under 100 characters wide to prevent wrapping on standard terminals (80–120 chars).
- **Clean Alignment:** Align Unicode box borders (`│`, `─`, `┌`, `┐`, `└`, `┘`) precisely.
- **No HTML/CDN Assets:** Do not embed HTML tags, CSS stylesheets, or external JS scripts.
- **Plain Text Badges:** Use square brackets for badges: `[Strong]`, `[Worth exploring]`, `[Speculative]`.

## Top Recommendation Section

Include a summary section at the end of the report:

```markdown
## Top Recommendation

### 1. Collapse Order Intake Pipeline
- **Reason:** Highest testability gain and removes widespread pricing leakage.
- **Next Step:** Begin grilling session on the Order Intake interface boundary.
```

## Tone & Terminology

Use exact terms from `/codebase-design`:
- **Use:** `module`, `interface`, `implementation`, `depth`, `deep`, `shallow`, `seam`, `adapter`, `leverage`, `locality`.
- **Avoid:** `component`, `service`, `unit`, `API`, `signature`, `boundary`, `layer`, `wrapper`.
