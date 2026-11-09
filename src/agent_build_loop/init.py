"""`abl init`: set up the main checkout (person settings, main's hook, the exclude lines, the trust record).

The plain form is idempotent and never re-records trust over a change: when the record exists and something
changed, it refuses and names the person-only `--rehash`. `--set`, `--rehash` and `--remove-hooks` are
person-only (the CLI checks the terminal before reading any state, then asks for a retype).
"""
from pathlib import Path

from agent_build_loop import events, person, settings, trust, worktrees
from agent_build_loop.state import DEFAULT_SETTINGS

SETTINGS_KEYS = tuple(DEFAULT_SETTINGS)


def _exclude_missing(repo) -> list[str]:
    path = repo.common_dir / "info" / "exclude"
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [line for line in worktrees.EXCLUDE_LINES if line not in existing]


def setup(repo: worktrees.Repo, dry_run: bool = False) -> list[str]:
    """Bring the main checkout to the set-up state. Returns the changes made (or, with `dry_run`, the ones it would make)."""
    store = repo.store
    trusted = store.path("trust.json").exists()
    if trusted:
        diff = trust.changed(store, repo.common_dir, repo.main_branch, repo.main_root)
        if diff:
            raise worktrees.Refused(trust.explain(diff))
    todo = []
    if not store.path("settings.json").exists():
        todo.append(("settings", "record the person settings (defaults: main_branch = main, require_approval = true)"))
    if not settings.hooks_installed(repo.main_root, "main"):
        todo.append(("hooks", f"write the main session's hook entries to {settings.SETTINGS_RELPATH} (--as main)"))
    if _exclude_missing(repo):
        todo.append(("exclude", "add .worktrees/ and the hook settings file to .git/info/exclude"))
    if not trusted:
        todo.append(("trust", "record the trust fingerprint (git config, attributes, hooks, .abl.toml on main)"))
    if dry_run:
        return [f"would {what}" for _k, what in todo]
    store.ensure()
    for key, _what in todo:
        if key == "settings":
            store.save_settings(dict(DEFAULT_SETTINGS))
        elif key == "hooks":
            settings.write_hooks(repo.main_root, "main")
        elif key == "exclude":
            worktrees.ensure_exclude(repo.common_dir)
        elif key == "trust":
            trust.record(store, repo.common_dir, repo.main_branch, repo.main_root)
    if todo:
        events.append(store, "main", "init", None, {"changed": [k for k, _w in todo]})
    return [what for _k, what in todo]


def rehash(repo: worktrees.Repo, confirm=person.require_person) -> str:
    diff = trust.changed(repo.store, repo.common_dir, repo.main_branch, repo.main_root)
    names = ", ".join(trust.PART_LABELS[k] for k in diff) if diff else "nothing"
    confirm("rehash", f"Changed since the last record: {names}. Record the current state as trusted?")
    trust.record(repo.store, repo.common_dir, repo.main_branch, repo.main_root)
    events.append(repo.store, "person", "trust-rehash", None, {"changed": diff})
    return f"trust recorded (changed: {names})"


def parse_set(pairs: list[str]) -> dict:
    out = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or key not in SETTINGS_KEYS:
            raise ValueError(f"--set takes KEY=VALUE with KEY one of {', '.join(SETTINGS_KEYS)}")
        if key == "require_approval":
            if value.lower() not in ("true", "false"):
                raise ValueError("require_approval must be true or false")
            out[key] = value.lower() == "true"
        else:
            out[key] = value
    return out


def set_settings(repo: worktrees.Repo, pairs: list[str], confirm=person.require_person) -> str:
    changes = parse_set(pairs)
    diff = trust.changed(repo.store, repo.common_dir, repo.main_branch, repo.main_root)
    if diff:
        raise worktrees.Refused(trust.explain(diff))
    keys = " ".join(sorted(changes))
    confirm(keys, f"Change the person settings: {', '.join(f'{k} = {v}' for k, v in sorted(changes.items()))}?")
    merged = {**repo.store.load_settings(), **changes}
    repo.store.save_settings(merged)
    try:
        repo.store.load_settings()  # the same validation every reader applies
    except Exception:
        repo.store.save_settings(repo.settings)
        raise
    # the project config part follows main_branch; the person has just looked at an unchanged record
    trust.record(repo.store, repo.common_dir, merged["main_branch"], repo.main_root)
    events.append(repo.store, "person", "settings-set", None, {"keys": sorted(changes)})
    return f"settings changed: {keys}"


def remove_all_hooks(repo: worktrees.Repo, confirm=person.require_person) -> list[str]:
    """The person's recovery step: take the abl hook entries out of the main checkout and every worktree."""
    confirm("remove-hooks", "Remove the abl guard from the main checkout and every worktree?")
    dirs = [repo.main_root]
    wt_root = repo.main_root / ".worktrees"
    if wt_root.is_dir():
        dirs += sorted(p for p in wt_root.iterdir() if p.is_dir())
    removed = []
    for d in dirs:
        if settings.remove_hooks(d):
            removed.append("." if d == repo.main_root else Path(d).relative_to(repo.main_root).as_posix())
    events.append(repo.store, "person", "hooks-removed", None, {"where": removed})
    return removed
