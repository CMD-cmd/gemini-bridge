"""start/ensure/status/stop/headers for the independent HTTP MCP process.

The headers action is a credential helper: consume its stdout privately.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / ".runtime"
STATE = RUNTIME / "http-mcp.json"
PYTHON = ROOT / ".venv-http" / "bin" / "python"
SERVER = ROOT / "http_mcp.py"
PORT = 50252
URL = f"http://127.0.0.1:{PORT}/mcp"


def read_state():
    if not STATE.exists():
        return None
    if STATE.is_symlink() or STATE.stat().st_mode & 0o077:
        raise RuntimeError("HTTP discovery file must be a private regular file (0600)")
    return json.loads(STATE.read_text())


def save_state(value):
    temporary = RUNTIME / (".http-mcp-" + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(value, output)
    os.replace(temporary, STATE)


def owned_process(state):
    if not state or not isinstance(state.get("pid"), int) or state["pid"] <= 1:
        return False
    try:
        command = subprocess.check_output(["ps", "-p", str(state["pid"]), "-o", "command="], text=True).strip()
        born = subprocess.check_output(["ps", "-p", str(state["pid"]), "-o", "lstart="], text=True).strip()
    except subprocess.CalledProcessError:
        return False
    # ps renders argv without quoting embedded spaces on macOS.
    expected = " ".join([str(PYTHON), str(SERVER), "--serve"])
    return command == expected and born == state.get("process_start")


def healthy(state):
    if not owned_process(state):
        return False
    try:
        with build_opener(ProxyHandler({})).open(URL.replace("/mcp", "/health"), timeout=1) as response:
            value = json.load(response)
        return value == {"status": "ok", "transport": "streamable-http", "instance": state["instance"]}
    except (OSError, ValueError):
        return False


def status(state):
    return {"running": healthy(state), "url": URL,
            "pid": state.get("pid") if state and owned_process(state) else None}


def start(state):
    if healthy(state):
        return status(state)
    if owned_process(state):
        raise RuntimeError("Recorded HTTP process is not healthy; refusing duplicate start")
    # Never replace the credentials of an unrelated listener.
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", PORT))
        except OSError as error:
            raise RuntimeError(f"HTTP MCP port {PORT} is occupied; existing listener was not changed") from error
    # Keep the private identity stable across a reboot: Codex stores this token.
    # Reject incomplete discovery records rather than reusing arbitrary values.
    if (isinstance(state, dict) and state.get('url') == URL
            and re.fullmatch(r'[A-Za-z0-9_-]{64}', str(state.get('token', '')))
            and re.fullmatch(r'[a-f0-9]{48}', str(state.get('instance', '')))):
        state = {key: state[key] for key in ('token', 'instance', 'url')}
    else:
        state = {"token": secrets.token_urlsafe(48), "instance": secrets.token_hex(24), "url": URL}
    save_state(state)
    log_fd = os.open(RUNTIME / "http-mcp.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(log_fd, "a") as log:
        process = subprocess.Popen([str(PYTHON), str(SERVER), "--serve"], cwd=ROOT,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True, close_fds=True)
    state["pid"] = process.pid
    state["process_start"] = subprocess.check_output(["ps", "-p", str(process.pid), "-o", "lstart="], text=True).strip()
    save_state(state)
    for _ in range(100):
        if healthy(state):
            return status(state)
        if process.poll() is not None:
            raise RuntimeError("HTTP MCP exited during startup; inspect .runtime/http-mcp.log")
        time.sleep(.1)
    # This Popen object is our child, not a PID obtained from discovery.
    process.terminate()
    raise RuntimeError("HTTP MCP did not become healthy; its new process was stopped")


def stop(state):
    if not state or not owned_process(state):
        return {"running": False, "stopped": False}
    if not healthy(state):
        raise RuntimeError("HTTP identity could not be verified; refusing to signal discovery PID")
    os.kill(state["pid"], signal.SIGTERM)
    for _ in range(450):
        if not owned_process(state):
            return {"running": False, "stopped": True}
        time.sleep(.1)
    raise RuntimeError("HTTP MCP is still draining; no force kill was sent")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "ensure", "status", "stop", "headers"])
    action = parser.parse_args().action
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    os.chmod(RUNTIME, 0o700)
    fd = os.open(RUNTIME / "http-mcp.lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state()
        if action in ("start", "ensure"):
            result = start(state)
        elif action == "stop":
            result = stop(state)
        elif action == "headers":
            if not healthy(state):
                start(state)
                state = read_state()
            result = {"Authorization": "Bearer " + state["token"]}
        else:
            result = status(state)
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
