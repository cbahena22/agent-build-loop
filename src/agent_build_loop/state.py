"""The state store: one folder in the git common dir, shared by every worktree, never committed."""
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from agent_build_loop.claims import Claim
from agent_build_loop.paths import PathRuleError, validate_topic

STALE_LOCK_S = 600
LOCK_WAIT_S = 5.0
DEFAULT_SETTINGS = {"main_branch": "main", "require_approval": True}
STATE_DIRNAME = "agent-build-loop"


class StateError(Exception):
    """The state store is unreadable, locked or refused a write (exit 1 or 2 in the CLI)."""


class LockBusy(StateError):
    pass


class LockStale(StateError):
    """A lock is older than ten minutes and its process is gone. It is reported, never broken automatically."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _replace(src: str, dst: Path, tries: int = 10) -> None:
    """`os.replace`, retried briefly on Windows, where a reader holding `dst` open makes it fail with a sharing error."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if sys.platform != "win32" or i == tries - 1:
                raise
            time.sleep(0.05 * (i + 1))


def atomic_write(path: Path, text: str) -> None:
    """Write `text` to a temp file in the same folder, then replace. A crash leaves the old file intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Store:
    def __init__(self, common_dir):
        self.root = Path(common_dir) / STATE_DIRNAME

    # -- paths
    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def ensure(self) -> None:
        for sub in ("claims", "audits", "approvals", "handovers"):
            self.path(sub).mkdir(parents=True, exist_ok=True)

    # -- locks
    def lock_command(self, name: str) -> str:
        rel = f".git/{STATE_DIRNAME}/{name}.lock"
        return f"delete the file {rel} in the main checkout (PowerShell: Remove-Item .git\\{STATE_DIRNAME}\\{name}.lock)"

    @contextmanager
    def lock(self, name: str, wait_s: float = LOCK_WAIT_S):
        """Exclusive lock file (`O_CREAT | O_EXCL`) holding pid and UTC time."""
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.path(f"{name}.lock")
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                self._check_stale(name, lock_path)
                if time.monotonic() >= deadline:
                    raise LockBusy(f"{name} is held by another abl command; try again in a moment") from None
                time.sleep(0.02)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"pid": os.getpid(), "utc": utc_now()}))
            yield
        finally:
            try:
                os.unlink(lock_path)
            except OSError:
                pass

    def _check_stale(self, name: str, lock_path: Path) -> None:
        try:
            info = json.loads(lock_path.read_text(encoding="utf-8"))
            pid = int(info["pid"])
            age = time.time() - lock_path.stat().st_mtime
        except (ValueError, KeyError, TypeError):
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                return
            if age > STALE_LOCK_S:  # empty or garbled: its writer died before finishing
                raise LockStale(f"{name} lock is unreadable and older than 10 minutes. If you are sure nothing is running, {self.lock_command(name)}") from None
            return
        except OSError:
            return  # removed meanwhile: the caller retries
        if age > STALE_LOCK_S and not pid_alive(pid):
            raise LockStale(f"{name} lock is stale (older than 10 minutes, its process is gone). If you are sure nothing is running, {self.lock_command(name)}")

    # -- person settings
    def load_settings(self) -> dict:
        p = self.path("settings.json")
        if not p.exists():
            return dict(DEFAULT_SETTINGS)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StateError("settings.json in the state store is unreadable") from exc
        if not isinstance(data, dict) or set(data) - set(DEFAULT_SETTINGS):
            raise StateError("settings.json has unknown keys")
        merged = {**DEFAULT_SETTINGS, **data}
        branch = merged["main_branch"]
        if not isinstance(branch, str) or not branch or branch.startswith("-") or not branch.replace("/", "").replace("-", "").replace("_", "").replace(".", "").isalnum():
            raise StateError("settings.json main_branch is not a plain branch name")
        if not isinstance(merged["require_approval"], bool):
            raise StateError("settings.json require_approval must be true or false")
        return merged

    def save_settings(self, settings: dict) -> None:
        atomic_write(self.path("settings.json"), json.dumps({**DEFAULT_SETTINGS, **settings}, indent=2, sort_keys=True) + "\n")

    # -- claims (callers hold claims.lock when they change them)
    def claim_path(self, topic: str) -> Path:
        validate_topic(topic)
        return self.path("claims", f"{topic}.json")

    def load_claims(self) -> list[Claim]:
        folder = self.path("claims")
        if not folder.is_dir():
            return []
        out = []
        for p in sorted(folder.glob("*.json")):
            try:
                out.append(Claim.from_json(json.loads(p.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError, PathRuleError) as exc:
                raise StateError(f"claim file {p.name} is unreadable: {exc}") from exc
        return out

    def load_claim(self, topic: str) -> Claim | None:
        p = self.claim_path(topic)
        if not p.exists():
            return None
        return next((c for c in self.load_claims() if c.topic == topic), None)

    def save_claim(self, claim: Claim) -> None:
        atomic_write(self.claim_path(claim.topic), json.dumps(claim.to_json(), indent=2, sort_keys=True) + "\n")

    def remove_claim(self, topic: str) -> None:
        try:
            self.claim_path(topic).unlink()
        except FileNotFoundError:
            pass

    # -- records written by other commands, read here for `list` and handovers
    def read_record(self, kind: str, topic: str) -> dict | None:
        if kind not in ("audits", "approvals"):
            raise ValueError(kind)
        p = self.path(kind, f"{validate_topic(topic)}.json")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None
