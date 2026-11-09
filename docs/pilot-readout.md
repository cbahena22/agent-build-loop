# Pilot readout (one page)

Pilot on the fictional Kestrel Print Co. `print-quotes` project. Everything below comes from `abl demo` and `abl eval`; no real team or model review is involved.

## The ask

Three or four workers on one repository, with the lead keeping the say on every merge (see [brief.md](brief.md)).

## Results

- Red-team set: 60 / 60 scored cases matched (n = 65, 5 known misses). Merge set: 10 / 10. Exact lines are in [`evals/results/eval.md`](../evals/results/eval.md).
- The demo opens two workers, denies one out-of-claim edit, merges one branch cleanly and rolls the other back.
- No model call, $0.

## Three cases

1. **The shell-write gap the audit caught.** In the demo, `rename-total` runs a `python -c` one-liner that edits `pyproject.toml`. The guard does not see shell writes of this kind (a known miss, `gap-python-c-writes-a-file`). The change is committed on the worker's branch, so the audit's claim check sees a file outside the claim and returns BLOCK; `abl merge` is refused (`no-pass`). The guard is not the only layer.
2. **The integration break the rollback caught.** `add-summary` merges cleanly. After a second pass, `rename-total` gets PASS-WITH-NOTES, the person approves, and the merge goes ahead; but a caller outside its claim still uses the old name, so the check fails on `main`. The merge gate resets with `git reset --keep` to the commit before the merge, and the handover lists what to do next.
3. **A stale audit.** A commit made after an audit changes the head. `abl merge` compares the audited commit with the branch head and refuses with `stale-audit`. The merge set carries this case.

## Limits seen

Shell writes into other worktrees or outside the repository are not stopped by the guard (see the README's honest limits). A rollback is refused when a tracked file changed on `main` during the merge.

## Next steps

- A live reviewer adapter, behind the same protocol, with recorded transcripts.
- Containers or separate OS users for hostile isolation.
- Tests on macOS.
