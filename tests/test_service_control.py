"""Service ownership and PID reuse checks without signaling real user processes."""

import os

from scripts import service_control as control


def process(pid, command="sleep 30", *, parent=1, group=None, started="Fri Oct 2 12:00:00 2026", uid=501, status="S"):
    return control.Process(pid, parent, group or pid, uid, started, command, status)


def select(processes, *, state=None, directories=None, protected=()):
    directories = directories or {}
    return control.select_targets(
        {item.pid: item for item in processes}, state or {},
        directory=lambda pid: directories.get(pid, control.PROJECT_DIR),
        uid=501, protected=protected,
    )


def test_manual_services_include_children_but_not_other_projects_or_users():
    processes = [
        process(10, "python -m uvicorn backend.main:app --port 8000"),
        process(11, "python -c multiprocessing.spawn", parent=10),
        process(20, "npm run dev --host 127.0.0.1"),
        process(21, "node frontend/node_modules/vite/bin/vite.js", parent=20),
        process(30, "python -m uvicorn backend.main:app --port 8000"),
        process(40, "node node_modules/vite/bin/vite.js"),
        process(50, "python -m uvicorn backend.main:app", uid=502),
        process(60, "python -m http.server 8000"),
    ]
    selected = select(processes, directories={30: control.PROJECT_DIR.parent / "other", 40: control.PROJECT_DIR.parent / "other"})
    assert set(selected) == {10, 11, 20, 21}


def test_orphaned_reload_child_is_recovered_from_recorded_group():
    state = {"processes": [{"role": "backend", "pid": 10, "pgid": 10, "started": "old"}]}
    processes = [process(11, "python -c multiprocessing.spawn", group=10),
                 process(12, "python -c multiprocessing.resource_tracker", group=10),
                 process(13, group=10, uid=502), process(14)]
    assert set(select(processes, state=state)) == {11, 12}


def test_reused_pid_does_not_authorize_stale_process_group():
    state = {"processes": [{"role": "backend", "pid": 10, "pgid": 10, "started": "old"}]}
    processes = [process(10, group=10, started="new"), process(11, group=10)]
    assert select(processes, state=state) == {}


def test_protected_calling_shell_and_zombies_are_never_targets():
    processes = [process(10, "bash scripts/dev.sh"),
                 process(11, "python -m uvicorn backend.main:app", status="Z"),
                 process(12, "node node_modules/vite/bin/vite.js")]
    assert set(select(processes, protected={10, 12})) == set()


def test_remaining_targets_rechecks_identity_before_signaling():
    original = process(10, started="old")
    assert control.remaining_targets({10: original}, {10: process(10, started="new")}) == {}
    assert control.remaining_targets({10: original}, {10: process(10, started="old", status="Z")}) == {}


def test_old_launcher_cannot_remove_new_launch_state(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "STATE_FILE", tmp_path / "services.json")
    control.STATE_FILE.write_text('{"project": ' + control.json.dumps(str(control.PROJECT_DIR)) + ', "token": "new", "processes": []}')
    control.clear_state("old")
    assert control.STATE_FILE.exists()
    control.clear_state("new")
    assert not control.STATE_FILE.exists()


def test_record_is_private_and_contains_no_process_arguments(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(control, "STATE_FILE", tmp_path / ".run" / "services.json")
    monkeypatch.setattr(control, "process_snapshot", lambda: {
        pid: process(pid, command="python --private-argument") for pid in (10, 20, 30)
    })
    control.record_services(10, 20, 30)
    token = capsys.readouterr().out.strip()
    state = control.read_state()
    assert state["token"] == token and token
    assert len(state["processes"]) == 3
    assert "private-argument" not in control.STATE_FILE.read_text()
    if os.name == "posix":
        assert control.STATE_FILE.stat().st_mode & 0o777 == 0o600
