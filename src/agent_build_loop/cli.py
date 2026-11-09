"""The `abl` command line. Exit 0 = ok, 1 = refused or failed, 2 = usage or config error."""
import argparse
import os
import sys
from pathlib import Path

from agent_build_loop import approve as approvelib
from agent_build_loop import config, events, gitio, handover, mask, merge as mergelib, person, worktrees
from agent_build_loop import init as initlib
from agent_build_loop.audit import record as auditrecord
from agent_build_loop.audit import reviewer as reviewerlib
from agent_build_loop.audit import run as auditrun
from agent_build_loop.claims import PathRuleError
from agent_build_loop.state import StateError

UNEXPECTED = "abl: failed: an unexpected error stopped the command; nothing more is shown"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="abl", allow_abbrev=False,
                                description="Run several Claude Code workers on one repository, with a person who keeps the final say.")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", allow_abbrev=False, help="set up the main checkout (hooks, settings, trust record)")
    i.add_argument("--dry-run", action="store_true")
    i.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="change a person setting (the person, at a terminal)")
    i.add_argument("--rehash", action="store_true", help="record the current git state as trusted (the person, at a terminal)")
    i.add_argument("--remove-hooks", action="store_true", help="take the guard out again (the person, at a terminal)")
    n = sub.add_parser("new", allow_abbrev=False, help="open a worker: worktree, branch and claim")
    n.add_argument("topic")
    n.add_argument("--owns", nargs="+", required=True, metavar="PATH")
    n.add_argument("--goal", required=True)
    n.add_argument("--model", default="")
    n.add_argument("--effort", default="")
    c = sub.add_parser("claim", allow_abbrev=False, help="add paths to an open claim")
    c.add_argument("topic")
    c.add_argument("--add", nargs="+", required=True, metavar="PATH")
    sub.add_parser("list", help="show open claims")
    e = sub.add_parser("end", allow_abbrev=False, help="remove a worker's worktree, branch and claim")
    e.add_argument("topic")
    e.add_argument("--discard", action="store_true", help="throw away unmerged work (the person, at a terminal)")
    h = sub.add_parser("handover", help="write or show a handover")
    hs = h.add_subparsers(dest="hcmd", required=True)
    w = hs.add_parser("write")
    w.add_argument("--note", action="append", default=[])
    w.add_argument("--waiting", action="append", default=[])
    hs.add_parser("show")
    a = sub.add_parser("audit", allow_abbrev=False, help="audit a worker branch at its current head")
    a.add_argument("topic")
    a.add_argument("--reviewer", default="stub", help="stub (default) or replay:<file>")
    sub.add_parser("verdict", help="exit 0 for a current PASS, 1 BLOCK, 2 missing or stale").add_argument("topic")
    sub.add_parser("approve", help="approve a merge (the person, at a terminal)").add_argument("topic")
    sub.add_parser("merge", help="merge an audited, approved branch; roll back if main's check fails").add_argument("topic")
    hk = sub.add_parser("hook", allow_abbrev=False, help="Claude Code hook entry points (written into settings by init and new)")
    hk.add_argument("event", choices=["pre-tool-use", "session-start"])
    hk.add_argument("--as", dest="role", default=None)
    d = sub.add_parser("demo", allow_abbrev=False, help="the scripted end-to-end demo in a temp folder ($0, no model call)")
    d.add_argument("--keep", action="store_true", help="leave the demo folder")
    ev = sub.add_parser("eval", allow_abbrev=False, help="run the red-team set and the merge set in temp repositories ($0, no model call)")
    ev.add_argument("--out", default=os.path.join("evals", "results"), metavar="DIR", help="where eval.md and eval.json go (default: evals/results)")
    return p


class _Out:
    def __init__(self, masker):
        self.masker = masker

    def say(self, text: str, stream=None) -> None:
        stream = stream or sys.stdout
        for line in str(text).splitlines() or [""]:
            stream.write(self.masker.text(line) + "\n")


def main(argv=None) -> int:
    """Entry point. Any error the commands do not name ends in one fixed line (no traceback, no path) and exit 1."""
    try:
        return _main(argv)
    except Exception:  # noqa: BLE001 - last resort; SystemExit from argparse is not an Exception and still exits 2
        _Out(mask.default_masker()).say(UNEXPECTED, sys.stderr)
        return 1


def _main(argv=None) -> int:
    args = _parser().parse_args(argv)
    out = _Out(mask.default_masker())
    try:
        gitio.require_git()
    except RuntimeError as exc:
        out.say(f"abl: {exc}", sys.stderr)
        return 2
    if sys.version_info < (3, 11):  # pragma: no cover - pyproject also says >=3.11
        out.say("abl: Python 3.11 or newer is required", sys.stderr)
        return 2
    if args.cmd == "hook":
        from agent_build_loop.hooks import guard, session_start

        return (guard if args.event == "pre-tool-use" else session_start).hook_main(args.role)
    if args.cmd == "demo":
        from agent_build_loop import demo

        try:
            demo.run(keep=args.keep, say=out.say)
        except (demo.DemoError, worktrees.Refused, gitio.GitError, StateError) as exc:
            out.say(f"abl demo: failed: {exc}", sys.stderr)
            return 1
        return 0
    if args.cmd == "eval":
        from agent_build_loop import evals

        try:
            report = evals.evaluate(say=out.say)
            paths = evals.write(report, Path(args.out))
        except (evals.EvalError, worktrees.Refused, gitio.GitError, StateError, OSError) as exc:
            out.say(f"abl eval: failed: {exc}", sys.stderr)
            return 1
        for name, s in report["sets"].items():
            out.say(evals.headline(name, s))
        out.say(f"wrote {paths[0].name} and {paths[1].name} to {args.out}")
        return 0 if evals.ok(report) else 1
    person_only = args.cmd == "approve" or (args.cmd == "init" and (args.set or args.rehash or args.remove_hooks))
    if args.cmd == "init" and sum(bool(x) for x in (args.set, args.rehash, args.remove_hooks, args.dry_run)) > 1:
        out.say("abl init: use one of --dry-run, --set, --rehash, --remove-hooks at a time", sys.stderr)
        return 2
    try:
        if person_only:
            person.require_tty(f"abl {args.cmd}")  # before any state is read
        repo = worktrees.open_repo(os.getcwd())
        out.masker = mask.default_masker(str(repo.main_root))
        return _run(args, repo, out)
    except config.ConfigError as exc:
        out.say(f"abl: config error in {exc}", sys.stderr)
        return 2
    except (worktrees.Refused, PathRuleError, person.NotAPerson, StateError) as exc:
        out.say(f"abl: refused: {exc}", sys.stderr)
        return 1
    except reviewerlib.ReviewerError as exc:
        out.say(f"abl: reviewer: {exc}", sys.stderr)
        return 1
    except gitio.GitError as exc:
        out.say(f"abl: {exc}" + (f": {exc.first_line}" if exc.first_line else ""), sys.stderr)
        return 1
    except (ValueError, OSError, events.EventError) as exc:
        out.say(f"abl: failed: {type(exc).__name__}: {exc}", sys.stderr)
        return 1


def _run(args, repo, out: _Out) -> int:
    cwd = os.getcwd()
    if args.cmd == "list":
        out.say(worktrees.format_rows(worktrees.list_rows(repo)))
        return 0
    if args.cmd == "handover" and args.hcmd == "show":
        text = handover.newest(repo.store)
        out.say(text if text is not None else "no handover yet")
        return 0
    if args.cmd == "verdict":
        code, line = auditrecord.status(repo, args.topic)
        out.say(line)
        return code
    worktrees.require_main(cwd, repo)
    if args.cmd == "init":
        if args.rehash:
            out.say(initlib.rehash(repo))
        elif args.set:
            try:
                out.say(initlib.set_settings(repo, args.set))
            except ValueError as exc:
                out.say(f"abl init: {exc}", sys.stderr)
                return 2
        elif args.remove_hooks:
            removed = initlib.remove_all_hooks(repo)
            out.say("removed the abl hooks from: " + (", ".join(removed) if removed else "nowhere (none were set)"))
        else:
            done = initlib.setup(repo, dry_run=args.dry_run)
            out.say("\n".join(done) if done else "already set up; nothing changed")
        return 0
    if args.cmd == "audit":
        try:
            rev = reviewerlib.from_spec(args.reviewer)
        except ValueError as exc:
            out.say(f"abl audit: {exc}", sys.stderr)
            return 2
        rec = auditrun.audit(repo, args.topic, rev)
        out.say(f"audit {args.topic} at {rec['head'][:8]}: {rec['verdict']}")
        for cid, status in rec["checks"]:
            out.say(f"  {cid}: {status}")
        for f in rec["findings"]:
            out.say(f"  finding: {f['path']}: {f['reason']}")
        return 0 if rec["verdict"] in auditrecord.PASSING else 1
    if args.cmd == "approve":
        out.say(approvelib.approve(repo, args.topic))
        return 0
    if args.cmd == "merge":
        result = mergelib.merge(repo, args.topic)
        out.say(result.message, sys.stdout if result.code == 0 else sys.stderr)
        return result.code
    if args.cmd == "new":
        claim, hint = worktrees.new(repo, args.topic, args.owns, args.goal, args.model, args.effort)
        out.say(f"opened {claim.topic} on {claim.branch}\nowns: {', '.join(claim.owns)}\n{hint}")
    elif args.cmd == "claim":
        claim = worktrees.claim_add(repo, args.topic, args.add)
        out.say(f"{claim.topic} now owns: {', '.join(claim.owns)}")
    elif args.cmd == "end":
        if args.discard:
            person.require_person(args.topic, f"Discard the unmerged work of {args.topic}?")
        out.say(worktrees.end(repo, args.topic, discard=args.discard))
    elif args.cmd == "handover":
        path = handover.write(repo, args.note, args.waiting, masker=out.masker)
        out.say(f"wrote handover {path.name}")
    return 0
