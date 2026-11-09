"""Trust fingerprint: notice files outside any branch that change what git does as the main session."""
import hashlib
import json
from pathlib import Path

from agent_build_loop import gitio
from agent_build_loop.state import Store, atomic_write

ABSENT = "absent"
PART_LABELS = {
    "config": "git config (every scope and include, as the main checkout reads it)",
    "attributes": "git attributes",
    "worktree_configs": "per-worktree git config (as each worker tree reads it)",
    "hooks": "git hooks",
    "project_config": "project config (.abl.toml on the main branch)",
}


def _sha_file(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except FileNotFoundError:
        return ABSENT


def _config_digest(*git_args: str, cwd) -> str:
    """Hash of the effective git config: `git config --list` with origin, scope and includes, through gitio.

    Covers system, global (incl. XDG), the repo config, `config.worktree` (when `extensions.worktreeConfig` is on)
    and every `include.path` / `includeIf` target that applies. Entries in the `command` scope are this package's own
    `-c` overrides (a fresh temp folder per run) and are left out so the hash is stable. Git's own order is kept:
    it is deterministic and it decides which value wins. Only the hash is stored; nothing from it is printed.
    A failing call hashes its exit code, so it never matches a good record.
    """
    r = gitio.git(*git_args, "config", "--list", "--show-origin", "--show-scope", "--includes", "-z", cwd=cwd, check=False)
    if r.code != 0:
        return f"git-config-failed:{r.code}"
    nul = chr(0)
    fields = r.out.split(nul)  # -z: scope NUL origin NUL key LF value NUL, repeated
    kept = []
    for i in range(0, len(fields) - 2, 3):
        scope, origin, entry = fields[i : i + 3]
        if scope != "command":
            kept.append(nul.join((scope, origin, entry)))
    return hashlib.sha256((nul * 2).join(kept).encode("utf-8")).hexdigest()


class TrustChanged(Exception):
    """Something outside the branches changed while a command ran; the record was left as it was."""


def worktree_entries(common_dir) -> dict[str, str]:
    """{admin folder name: hash of its `config.worktree` and effective config} for every linked worktree."""
    common = Path(common_dir)
    out = {}
    wt_root = common / "worktrees"
    if wt_root.is_dir():
        for d in sorted(wt_root.iterdir()):
            # --git-dir names the admin folder directly, so a worker cannot redirect this read by editing its `.git` file
            eff = _config_digest(f"--git-dir={d}", cwd=common) if d.is_dir() else ABSENT
            out[d.name] = f"{_sha_file(d / 'config.worktree')}:{eff}"
    return out


def _parts_and_worktrees(common_dir, main_branch: str, main_root=None) -> tuple[dict[str, str], dict[str, str]]:
    common = Path(common_dir)
    root = Path(main_root) if main_root is not None else common.parent
    wts = worktree_entries(common)
    wt_cfg = [f"{name}:{entry}" for name, entry in wts.items()]
    hooks = []
    hook_dir = common / "hooks"
    if hook_dir.is_dir():
        for h in sorted(hook_dir.iterdir()):
            if h.is_file() and not h.name.endswith(".sample"):
                data = h.read_bytes()
                hooks.append(f"{h.name}:{len(data)}:{hashlib.sha256(data).hexdigest()}")
    blob = gitio.blob_sha(f"refs/heads/{main_branch}", ".abl.toml", common) or ABSENT
    return {
        "config": _config_digest(cwd=root),
        "attributes": _sha_file(common / "info" / "attributes"),
        "worktree_configs": hashlib.sha256("\n".join(wt_cfg).encode()).hexdigest(),
        "hooks": hashlib.sha256("\n".join(hooks).encode()).hexdigest(),
        "project_config": blob,
    }, wts


def parts(common_dir, main_branch: str, main_root=None) -> dict[str, str]:
    return _parts_and_worktrees(common_dir, main_branch, main_root)[0]


def _digest(p: dict) -> str:
    return hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()


def fingerprint(common_dir, main_branch: str, main_root=None) -> tuple[str, dict[str, str]]:
    p = parts(common_dir, main_branch, main_root)
    return _digest(p), p


def _write(store: Store, p: dict, wts: dict) -> str:
    digest = _digest(p)
    atomic_write(store.path("trust.json"), json.dumps({"digest": digest, "parts": p, "worktrees": wts}, indent=2, sort_keys=True) + "\n")
    return digest


def record(store: Store, common_dir, main_branch: str, main_root=None) -> str:
    """Record the current state as trusted. Only `abl init` (first run, `--set`, or `--rehash` after the person looked) calls this."""
    p, wts = _parts_and_worktrees(common_dir, main_branch, main_root)
    return _write(store, p, wts)


def record_change(store: Store, common_dir, main_branch: str, main_root=None, *, added: tuple[str, ...] = (),
                  removed: tuple[str, ...] = ()) -> str:
    """Re-record after a command's own git work, accepting only the change that command made.

    `added` / `removed`: linked-worktree admin folders the command created or removed. Anything else that differs
    from the record (a hook, a config line, another worktree's config, main's `.abl.toml`: merge refuses a branch
    that changes it) raises TrustChanged and leaves the record as it was, so the next audit or
    merge still says `trust-changed`.
    """
    try:
        saved = json.loads(store.path("trust.json").read_text(encoding="utf-8"))
        saved_parts, saved_wts = dict(saved["parts"]), dict(saved["worktrees"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise TrustChanged(explain(list(PART_LABELS))) from exc
    now, wts = _parts_and_worktrees(common_dir, main_branch, main_root)
    expected_wts = {k: v for k, v in saved_wts.items() if k not in removed}
    for name in added:
        if name in wts and name not in saved_wts:
            expected_wts[name] = wts[name]
    bad = {k for k in PART_LABELS if k != "worktree_configs" and saved_parts.get(k) != now.get(k)}
    if wts != expected_wts:
        bad.add("worktree_configs")
    if bad:
        raise TrustChanged(explain([k for k in PART_LABELS if k in bad]))
    return _write(store, now, wts)


def changed(store: Store, common_dir, main_branch: str, main_root=None) -> list[str]:
    """Names of the parts that differ from the recorded fingerprint ([] = unchanged).

    A missing or unreadable record counts as changed in every part: nothing was ever trusted.
    """
    try:
        saved = json.loads(store.path("trust.json").read_text(encoding="utf-8"))["parts"]
    except (OSError, ValueError, KeyError, TypeError):
        return list(PART_LABELS)
    _digest, now = fingerprint(common_dir, main_branch, main_root)
    return [k for k in PART_LABELS if saved.get(k) != now.get(k)]


def explain(changed_parts: list[str]) -> str:
    names = ", ".join(PART_LABELS[k] for k in changed_parts)
    return f"trust-changed: {names} changed since it was last recorded. Look at it, then the person runs: abl init --rehash"
