"""Track and stop this checkout's services using only the Python standard library."""

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
from uuid import uuid4

PROJECT_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_DIR / ".run" / "services.json"


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    pgid: int
    uid: int
    started: str
    command: str
    status: str = "S"

    @property
    def identity(self):
        return self.pid, self.started


def process_snapshot():
    result = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,pgid=,uid=,stat=,lstart=,command="],
        check=True, capture_output=True, text=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    processes = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 10)
        if len(fields) != 11:
            continue
        pid, ppid, pgid, uid = map(int, fields[:4])
        processes[pid] = Process(pid, ppid, pgid, uid, " ".join(fields[5:10]), fields[10], fields[4])
    return processes


def process_directory(pid):
    """Linux exposes cwd in /proc; macOS provides it through its bundled lsof."""
    if sys.platform.startswith("linux"):
        try:
            return Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
        except OSError:
            return None
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=3,
        )
        for line in result.stdout.splitlines():
            if line.startswith("n/"):
                return Path(line[1:]).resolve()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def read_state():
    try:
        state = json.loads(STATE_FILE.read_text())
        if state.get("project") == str(PROJECT_DIR) and isinstance(state.get("processes"), list):
            return state
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def clear_state(token):
    if token and read_state().get("token") == token:
        STATE_FILE.unlink(missing_ok=True)


def record_services(launcher_pid, api_pid, ui_pid):
    snapshot = process_snapshot()
    entries = []
    for role, pid in (("launcher", launcher_pid), ("backend", api_pid), ("frontend", ui_pid)):
        process = snapshot.get(pid)
        if process is None:
            raise RuntimeError(f"El proceso de {role} terminó durante el arranque.")
        # Save identity and group, never command arguments or environment variables.
        entries.append({"role": role, "pid": pid, "pgid": process.pgid, "started": process.started})
    token = uuid4().hex
    STATE_FILE.parent.mkdir(mode=0o700, exist_ok=True)
    temporary = STATE_FILE.with_name(f"services-{token}.tmp")
    with open(temporary, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
        json.dump({"project": str(PROJECT_DIR), "token": token, "processes": entries}, output)
    temporary.replace(STATE_FILE)
    print(token)


def service_command(process, directory):
    try:
        parts = shlex.split(process.command)
    except ValueError:
        return False
    names = {Path(part).name for part in parts}
    if "uvicorn" in names and "backend.main:app" in parts:
        return True
    if any(part.endswith("node_modules/vite/bin/vite.js") for part in parts):
        return True
    if "npm" in names and "run" in parts and "dev" in parts:
        # npm rewrites its process title and can omit --prefix frontend.
        return directory in {PROJECT_DIR, PROJECT_DIR / "frontend"}
    return any(part in {"scripts/dev.sh", "start.sh", str(PROJECT_DIR / "scripts/dev.sh"), str(PROJECT_DIR / "start.sh")} for part in parts) and "bash" in names


def select_targets(processes, state, directory=process_directory, uid=None, protected=()):
    """Identify recorded services, orphaned group members and older/manual starts."""
    uid = os.getuid() if uid is None else uid
    selected = set()
    directories = {}

    def owned(process):
        if process.uid != uid or process.status.startswith("Z") or process.pid in protected:
            return False
        if process.pid not in directories:
            directories[process.pid] = directory(process.pid)
        return directories[process.pid] in {PROJECT_DIR, PROJECT_DIR / "frontend"}

    for entry in state.get("processes", []):
        if not isinstance(entry, dict):
            continue
        leader = processes.get(entry.get("pid"))
        # A reused PID must never authorize terminating its new process group.
        if leader is not None and leader.started != entry.get("started"):
            continue
        if leader is not None and owned(leader):
            selected.add(leader.pid)
        if entry.get("role") in {"backend", "frontend"}:
            for process in processes.values():
                if process.pgid == entry.get("pgid") and owned(process):
                    selected.add(process.pid)

    for process in processes.values():
        # Inspect cwd only for possible app commands, rather than all user processes.
        if service_command(process, PROJECT_DIR / "frontend") and owned(process):
            if service_command(process, directories[process.pid]):
                selected.add(process.pid)

    while True:
        children = {process.pid for process in processes.values()
                    if process.ppid in selected and process.uid == uid
                    and not process.status.startswith("Z") and process.pid not in protected}
        if children <= selected:
            break
        selected.update(children)
    return {pid: processes[pid] for pid in selected}


def protected_ancestors(processes):
    protected = set()
    pid = os.getpid()
    while pid in processes and pid not in protected:
        protected.add(pid)
        pid = processes[pid].ppid
    return protected


def remaining_targets(targets, processes):
    return {pid: process for pid, process in targets.items()
            if pid in processes and processes[pid].identity == process.identity
            and not processes[pid].status.startswith("Z")}


def signal_targets(targets, signum):
    for pid in remaining_targets(targets, process_snapshot()):
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass


def stop_services():
    state = read_state()
    snapshot = process_snapshot()
    targets = select_targets(snapshot, state, protected=protected_ancestors(snapshot))
    if not targets:
        clear_state(state.get("token"))
        print("No hay servicios de Emergency Analyzer abiertos en este repositorio.")
        return 0
    print(f"Cerrando Emergency Analyzer ({len(targets)} procesos)…", flush=True)
    signal_targets(targets, signal.SIGTERM)
    # A suspended background job must resume to handle its termination signal.
    signal_targets(targets, signal.SIGCONT)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        remaining = remaining_targets(targets, process_snapshot())
        if not remaining:
            break
        time.sleep(0.2)
    else:
        print("Algunos procesos no respondieron; forzando su cierre…", flush=True)
        signal_targets(remaining, signal.SIGKILL)
        time.sleep(0.2)
    remaining = remaining_targets(targets, process_snapshot())
    if remaining:
        print("No se pudieron cerrar los procesos: " + ", ".join(map(str, remaining)), file=sys.stderr)
        return 1
    clear_state(state.get("token"))
    print("Backend y frontend detenidos.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record")
    record.add_argument("pids", nargs=3, type=int)
    clear = commands.add_parser("clear")
    clear.add_argument("token")
    commands.add_parser("stop")
    args = parser.parse_args()
    try:
        if args.command == "record":
            record_services(*args.pids)
        elif args.command == "clear":
            clear_state(args.token)
        else:
            return stop_services()
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        print(f"No se pudieron gestionar los servicios: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
