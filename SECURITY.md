# Security

## Reporting a vulnerability

Use GitHub private vulnerability reporting on this repository (Security tab, "Report a vulnerability"). Please do not open a public issue for a vulnerability. Include the version, the operating system, and steps to reproduce.

This is a one-person project: expect an acknowledgement within a week.

## Scope

In scope: a way for a worker session to edit outside its claim through an editor tool, to forge or skip an approval, to change the trusted configuration, to make `abl` run planted code as the main session, or to leak machine paths or user names into `abl` output.

Out of scope: the documented limits in the README (shell writes the guard cannot parse, a hostile local user of the same OS account, MCP servers that write files, the checks running a branch's own code).

## What the package does and does not do

- Standard library only at run time; no network call; no model call; no key or secret is read or stored.
- Every git call runs with an empty hooks path and `core.fsmonitor=false`. A trust fingerprint blocks audit, approve and merge when git config, attributes, hooks or `.abl.toml` change without the person re-recording them.
- Branch text and handover text are treated as data. Unparsable reviewer output is a BLOCK.
- It is a guard, not a sandbox. See "Honest limits" in the README.

## Supported versions

Only the latest commit on the default branch.
