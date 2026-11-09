# Usage

Needs Python 3.11 or newer and git 2.30 or newer.

## Install and demo
Bash (Linux, macOS, Git Bash; on Windows use `.venv/Scripts/` instead of `.venv/bin/`):
```
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.lock
.venv/bin/pip install --no-deps --no-build-isolation .
.venv/bin/abl demo
```
<details><summary>PowerShell 5.1</summary>

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install --require-hashes -r requirements-dev.lock
.venv\Scripts\python -m pip install --no-deps --no-build-isolation .
.venv\Scripts\abl demo
```
</details>

## Use it in your repository
Person-only commands (`abl approve`, `abl init --rehash`, `abl end --discard`) need a real console. Git Bash runs in mintty, a terminal that is not a real Windows console, and refuses them: use PowerShell or Windows Terminal, or `winpty abl approve <topic>` (winpty gives mintty a pseudo console).
```
cd path/to/your/repo
abl init
abl new add-summary --owns src/summary.py tests/test_summary.py --goal "add a summary"
cd .worktrees/add-summary
claude                      # the worker runs here, behind the guard hook
cd ../..
abl audit add-summary
abl approve add-summary     # the person, at a terminal (Git Bash: winpty abl approve add-summary)
abl merge add-summary
abl handover write --waiting "what the next session must settle"
```
On Windows use backslashes in paths (`src\summary.py`). Project settings live in `.abl.toml` on the main branch (the `check` command, shared paths). `abl list` shows open claims; `abl verdict <topic>` exits 0 for a current PASS.

## What the guard does
Blocks Edit and Write outside the worker's claim, into another worktree, the main checkout, the git directory, the settings files and `.abl.toml`. Paths are resolved first (`..`, case, junctions, `\\?\` prefixes, 8.3 short names such as `PROGRA~1`, NTFS alternate data streams, trailing dots). It denies git commands that move branches or push, `git -c` overrides, writes into the git directory, and person-only commands run by a Claude session. It fails closed, and it reads its configuration from the main branch plus a trust fingerprint of git config, attributes and hooks.

## Honest limits (full list)
- The guard is a **guard, not a sandbox.** Editor tools are checked by path; shell commands are parsed best effort, so a worker can still write through a script or `python -c`. The red-team set lists these as known misses.
- The audit's diff check catches out-of-claim changes committed on the worker's own branch. Shell writes into other trees are caught only when they make `main` dirty (the merge refuses) or show as dirty in `abl list`. A shell write into another worktree can ride in that worker's commit if the path is inside that worker's claim. Writes outside the repository are not caught.
- Person-only commands need a terminal and a retype; a process that fakes a terminal could still approve. A local process running as the same OS user could also forge state files outside Claude's tools. The design defends against mistakes and prompt-injected drift, not a hostile local user; hostile isolation needs containers or separate OS users.
- The command-line role check uses the current folder. It stops mistakes; it is not authorization (the hook's identity and the terminal rule are).
- MCP tools are covered only by the `mcp__.*` matcher and `deny_mcp_for_workers`; an MCP server that writes files for main is not path-checked.
- The reviewer here is a stub or a replay of synthetic transcripts. No model call is made anywhere.
- The checks run the branch's own code on your machine, as running its tests would. The merge gate is only as good as the `check` command.
- A rollback is refused (with the manual command) when a tracked file changed on `main` during the merge.
