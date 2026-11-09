# Brief: Kestrel Print Co. (fictional)

Kestrel Print Co. is a made-up print shop with five developers and one lead. They want three or four Claude Code workers on their `print-quotes` library at the same time. This brief, the client and the project are invented for this repository.

## What went wrong in the trial

1. A worker "fixed" a file another worker was building, and the two changes collided.
2. Two branches that each passed their own tests broke `main` when merged together.

## What the lead needs

- Each worker has its own folder and a written list of paths it may change.
- An edit outside that list is blocked when it happens, and logged.
- Every branch gets a fresh-context audit tied to its exact commit. A later commit makes the audit stale.
- Nothing reaches `main` without an audit PASS and an approval typed by the lead at a terminal.
- A merge that breaks `main` is undone automatically, and the lead is told how.
- The next main session can pick up from a short handover, without trusting its text as instructions.

## Success

- Editor-tool edits outside a claim are denied (red-team set, `abl eval`).
- The merge gate refuses a conflict, a failing branch check, a stale audit, a missing approval and a BLOCK verdict, and rolls back a failing `main` (merge set, `abl eval`).
- `abl demo` shows the whole path on the `print-quotes` project in about a minute, with no key and no model call.

## Out of scope

- Sandboxing a hostile local user.
- Running the Claude sessions for the person.
- Any live model reviewer (the reviewer here is a stub or a replay of synthetic transcripts).
