import json
import os
import threading
import time

import pytest

from agent_build_loop import state, worktrees
from agent_build_loop.claims import Claim
from agent_build_loop.state import LockBusy, LockStale, Store, StateError


def test_atomic_write_replaces_and_leaves_no_temp(tmp_path):
    p = tmp_path / "s" / "f.json"
    state.atomic_write(p, "one")
    state.atomic_write(p, "two")
    assert p.read_text(encoding="utf-8") == "two"
    assert [x.name for x in p.parent.iterdir()] == ["f.json"]


def test_crash_between_temp_write_and_replace_keeps_the_old_file(tmp_path, monkeypatch):
    p = tmp_path / "f.json"
    state.atomic_write(p, "old")

    def boom(src, dst):
        raise OSError("simulated crash")

    monkeypatch.setattr(state.os, "replace", boom)
    with pytest.raises(OSError):
        state.atomic_write(p, "new")
    monkeypatch.undo()
    assert p.read_text(encoding="utf-8") == "old"
    assert [x.name for x in tmp_path.iterdir()] == ["f.json"]


def test_lock_excludes_and_releases(tmp_path):
    store = Store(tmp_path)
    with store.lock("claims"):
        assert json.loads(store.path("claims.lock").read_text(encoding="utf-8"))["pid"] == os.getpid()
        with pytest.raises(LockBusy):
            with store.lock("claims", wait_s=0.1):
                pass
    assert not store.path("claims.lock").exists()
    with store.lock("claims"):
        pass


def test_lock_released_on_error(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(RuntimeError):
        with store.lock("merge"):
            raise RuntimeError("x")
    assert not store.path("merge.lock").exists()


def test_stale_lock_with_dead_pid_is_reported_not_broken(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.root.mkdir(parents=True)
    lock = store.path("merge.lock")
    lock.write_text(json.dumps({"pid": 2_000_000_000, "utc": "2020-01-01T00:00:00Z"}), encoding="utf-8")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    monkeypatch.setattr(state, "pid_alive", lambda pid: False)
    with pytest.raises(LockStale) as exc:
        with store.lock("merge", wait_s=0.1):
            pass
    assert "merge.lock" in str(exc.value) and "Remove-Item" in str(exc.value)
    assert lock.exists()


def test_unreadable_old_lock_is_reported_stale(tmp_path):
    store = Store(tmp_path)
    store.root.mkdir(parents=True)
    lock = store.path("claims.lock")
    lock.write_text("", encoding="utf-8")
    with pytest.raises(LockBusy):  # young and empty: its writer may still be working
        with store.lock("claims", wait_s=0.1):
            pass
    old = time.time() - 3600
    os.utime(lock, (old, old))
    with pytest.raises(LockStale, match="unreadable"):
        with store.lock("claims", wait_s=0.1):
            pass


def test_old_lock_of_a_live_process_is_just_busy(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.root.mkdir(parents=True)
    lock = store.path("claims.lock")
    lock.write_text(json.dumps({"pid": os.getpid(), "utc": "2020-01-01T00:00:00Z"}), encoding="utf-8")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    with pytest.raises(LockBusy):
        with store.lock("claims", wait_s=0.1):
            pass


def test_pid_alive_for_self_and_nonsense():
    assert state.pid_alive(os.getpid())
    assert not state.pid_alive(0)
    assert not state.pid_alive(-5)


def test_settings_defaults_roundtrip_and_validation(tmp_path):
    store = Store(tmp_path)
    assert store.load_settings() == {"main_branch": "main", "require_approval": True}
    store.save_settings({"main_branch": "trunk", "require_approval": False})
    assert store.load_settings() == {"main_branch": "trunk", "require_approval": False}
    for bad in ('{"main_branch": "--evil"}', '{"require_approval": "no"}', '{"other": 1}', "not json", '{"main_branch": ""}', '{"main_branch": "a b"}'):
        store.path("settings.json").write_text(bad, encoding="utf-8")
        with pytest.raises(StateError):
            store.load_settings()


def test_claim_files_and_unreadable_claim(tmp_path):
    store = Store(tmp_path)
    c = Claim("add-summary", "w/add-summary", ".worktrees/add-summary", ["a.py"], "g", "", "", "2026-01-01T00:00:00Z", "b" * 40)
    store.save_claim(c)
    assert store.load_claims() == [c] and store.load_claim("add-summary") == c and store.load_claim("nope-x") is None
    store.claim_path("add-summary").write_text("{}", encoding="utf-8")
    with pytest.raises(StateError):
        store.load_claims()
    store.remove_claim("add-summary")
    store.remove_claim("add-summary")  # idempotent
    assert store.load_claims() == []


def test_two_concurrent_new_never_both_succeed(repo_dir, monkeypatch):
    results = []
    barrier = threading.Barrier(2)

    def go(topic):
        r = worktrees.open_repo(repo_dir)
        barrier.wait()
        try:
            worktrees.new(r, topic, ["print_quotes/"], "work")
            results.append((topic, "ok"))
        except worktrees.Refused as exc:
            results.append((topic, "refused"))
            assert "overlaps" in str(exc)

    threads = [threading.Thread(target=go, args=(t,)) for t in ("first-one", "second-one")]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert sorted(r[1] for r in results) == ["ok", "refused"]
    assert len(worktrees.open_repo(repo_dir).store.load_claims()) == 1
