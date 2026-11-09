# Decisions

Short records (ADRs, architecture decision records) of why `abl` is built the way it is.

## ADR-001: One git worktree per worker
Each worker gets its own folder and branch. Workers cannot trample each other's working files, and a branch is the unit that is audited and merged. Cost: disk space and a second checkout per worker.

## ADR-002: Claims are path lists and never overlap
A claim lists the paths a worker may change. `abl new` and `abl claim --add` refuse a path another open claim covers. Plain lists are easy to check, show and explain; globbing and ownership by symbol were left out.

## ADR-003: Hooks fail closed, with identity baked in
If the guard hook cannot parse its input, resolve a path or load its configuration, it denies. A broken guard that allows everything would be worse than one that blocks too much. The role (`main` or a worker topic) is written into the hook command in that worktree's settings (`--as`). It is not read from the current folder, because a worker can `cd` anywhere.

## ADR-004: Configuration comes from the main branch ref, plus a trust fingerprint
`.abl.toml` is read from the main branch, never from a worker's branch, so a branch cannot loosen its own audit. A fingerprint of git config, attributes, hooks and `.abl.toml` is recorded by the person; a change blocks audit, approve and merge until the person re-records it. `abl merge` also refuses (`project-config`) a branch that changes `.abl.toml` against any merge base, and undoes a merge that changed main's `.abl.toml` anyway (for example through a criss-cross history), so a branch cannot get its own audit settings trusted by merging.

## ADR-005: An audit is bound to one commit and a canonical brief
The verdict names the commit it saw. The reviewer gets a canonical brief that labels branch text as data. A later commit makes the audit stale. Deterministic BLOCKs win over the reviewer, and unparsable reviewer output is a BLOCK.

## ADR-006: Approval needs a terminal
`abl approve` requires an interactive terminal and a retyped commit id. Claude sessions have no terminal. This stops mistakes and prompt-injected drift; it does not stop a hostile local user (see the README).

## ADR-007: Rollback uses `git reset --keep`
When the check fails on `main` after a merge, `abl merge` resets to the commit before the merge with `--keep`, which refuses to overwrite local changes. If a tracked file changed meanwhile, the rollback is refused and the manual command is printed.

## ADR-008: State lives in the git common directory
Claims, audits, approvals, the trust record and `events.jsonl` sit in the repository's common git directory, shared by all worktrees and never committed. The editor tools are denied access to it while a claim is open.

## ADR-009: Standard library only
No runtime dependency: nothing to pin or audit at run time, and the install is `pip install --no-deps`. Development tools are hash-locked.
