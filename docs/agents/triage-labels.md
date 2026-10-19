# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

## Category roles

| Role in mattpocock/skills | Label in our tracker | Notes |
| ------------------------- | -------------------- | ----- |
| `bug`                     | `bug`                | Already in use. |
| `enhancement`             | `enhancement`        | Use for `architecture` refactors too: keep the `architecture` label and add `enhancement`. |

`security` is a topic label, not a category. It sits alongside `bug` or `enhancement` and never replaces them. A `security` issue goes to `ready-for-human` unless the fix is fully specified and touches no key handling, crypto or auth-gate code.

Edit the right-hand columns to match whatever vocabulary you actually use.
