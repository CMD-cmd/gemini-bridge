"""Standard-library ACP NDJSON transport; no model or session policy.

Callbacks receive ``(method, params)`` and run on daemon threads, never the
reader. Notifications execute in arrival order on one dedicated worker; call
drain_notifications() after a prompt response to wait for preceding updates. ``on_request`` returns the JSON-RPC result or raises ACPError. Without
that callback all server requests are refused. Callback code should be bounded;
close() cannot forcibly stop arbitrary Python callback code.
"""
from __future__ import annotations

import json
import os
import queue
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence


class ACPError(Exception):
    """Transport/protocol failure or remote JSON-RPC error (no automatic retry)."""

    def __init__(self, message: str, *, code: int | None = None,
                 data: Any = None, kind: str = "rpc") -> None:
        super().__init__(message)
        self.code, self.data, self.kind = code, data, kind


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: ACPError | None = None
    observer: Callable | None = None


@dataclass
class _ResponseObservation:
    callback: Callable
    value: Any


class ACPClient:
    """One owned subprocess, with concurrent JSON-RPC requests.

    ``env=None`` inherits the environment; an explicit mapping replaces it
    completely, so previously removed credentials cannot be reintroduced.
    stderr is append-only in a
    mode-0600 file (a private temporary file if stderr_path is omitted). Protocol
    stdout is never logged. start() is explicit; instances cannot be restarted.
    Use notify('session/cancel', {'sessionId': ...}) for protocol cancellation.
    """

    def __init__(self, argv: Sequence[str], *, cwd: str | os.PathLike | None = None,
                 env: Mapping[str, str] | None = None,
                 stderr_path: str | os.PathLike | None = None,
                 on_notification: Callable[[str, Any], None] | None = None,
                 on_request: Callable[[str, Any], Any] | None = None) -> None:
        if not argv or isinstance(argv, (str, bytes)):
            raise ValueError("argv must be a nonempty sequence of arguments")
        self.argv, self.cwd = list(argv), cwd
        self.env = None if env is None else dict(env)
        self.stderr_path = Path(stderr_path) if stderr_path is not None else None
        self.on_notification, self.on_request = on_notification, on_request
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._next_id = 0
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._notification_worker: threading.Thread | None = None
        self._notifications: queue.Queue = queue.Queue()
        self._notification_condition = threading.Condition()
        self._notifications_submitted = 0
        self._notifications_completed = 0
        self._started = False
        self._closed = False
        self._failure: ACPError | None = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def start(self) -> "ACPClient":
        with self._lock:
            if self._closed:
                raise ACPError("Client is closed", kind="closed")
            if self._started:
                raise ACPError("Client was already started", kind="state")
            self._started = True
            fd = None
            try:
                if self.stderr_path is None:
                    fd, path = tempfile.mkstemp(prefix="acp-stderr-", suffix=".log")
                    self.stderr_path = Path(path)
                else:
                    fd = os.open(self.stderr_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND
                                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.fchmod(fd, 0o600)
                stderr = os.fdopen(fd, "ab", buffering=0)
                fd = None  # The file object now owns the descriptor.
                with stderr:
                    self._process = subprocess.Popen(
                        self.argv, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=stderr, start_new_session=True)
            except Exception as exc:
                self._failure = ACPError(f"Unable to start ACP process: {exc}", kind="start")
                raise self._failure from exc
            finally:
                if fd is not None:
                    os.close(fd)
            self._notification_worker = threading.Thread(
                target=self._notification_loop, name="acp-notifications", daemon=True)
            self._notification_worker.start()
            self._reader = threading.Thread(target=self._read_loop, name="acp-reader", daemon=True)
            self._reader.start()
        return self

    def _check(self) -> None:
        if self._failure:
            raise self._failure
        if self._closed:
            raise ACPError("Client is closed", kind="closed")
        if self._process is None:
            raise ACPError("Client has not started", kind="state")

    def _send(self, message: dict, *, send_guard=None, on_sent=None) -> None:
        try:
            encoded = (json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        except (ValueError, TypeError) as exc:
            raise ACPError(f"Message is not JSON serializable: {exc}", kind="protocol") from exc
        with self._write_lock:
            with self._lock:
                self._check()
                process = self._process
            try:
                with send_guard() if send_guard else nullcontext():
                    process.stdin.write(encoded)
                    process.stdin.flush()
                    if on_sent:on_sent()
            except (OSError, ValueError) as exc:
                error = ACPError(f"ACP write failed: {exc}", kind="transport")
                self._fail(error)
                raise error from exc

    def request(self, method: str, params: Any = None, *, timeout: float | None = 60.0,
                send_guard=None, on_sent=None, on_response=None) -> Any:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be nonnegative or None")
        pending = _Pending(observer=on_response)
        with self._lock:
            self._check()
            self._next_id += 1
            request_id = self._next_id
            self._pending[request_id] = pending
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method,
                        "params": {} if params is None else params},send_guard=send_guard,on_sent=on_sent)
            if not pending.event.wait(timeout):
                with self._lock:
                    if not pending.event.is_set():
                        self._pending.pop(request_id, None)
                        raise ACPError(f"ACP request {method!r} timed out; it was not resent",
                                       kind="timeout", data={"id": request_id, "method": method})
            if pending.error:
                raise pending.error
            return pending.result
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: Any = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method,
                    "params": {} if params is None else params})

    def _fail(self, error: ACPError) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = error
            for pending in self._pending.values():
                pending.error = self._failure
                pending.event.set()
            self._pending.clear()

    def _read_loop(self) -> None:
        try:
            for line in self._process.stdout:
                try:
                    message = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                    self._dispatch(message)
                except (ValueError, TypeError, KeyError) as exc:
                    raise ACPError(f"Malformed ACP JSON-RPC message: {exc}", kind="protocol") from exc
            raise ACPError("ACP process closed stdout (EOF)", kind="eof")
        except Exception as exc:
            self._fail(exc if isinstance(exc, ACPError) else
                       ACPError(f"ACP reader failed: {exc}", kind="transport"))

    def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ValueError("expected a JSON-RPC 2.0 object")
        if "method" in message:
            if not isinstance(message["method"], str):
                raise ValueError("method must be a string")
            if "id" in message and (isinstance(message["id"], bool) or
                                    not isinstance(message["id"], (int, str))):
                raise ValueError("invalid server request id")
            if "id" not in message:
                with self._notification_condition:
                    self._notifications_submitted += 1
                self._notifications.put(message)
            else:
                threading.Thread(target=self._handle_callback, args=(message,),
                                 name="acp-callback", daemon=True).start()
            return
        response_id = message.get("id")
        if isinstance(response_id, bool) or not isinstance(response_id, (int, str)):
            raise ValueError("invalid response id")
        if ("result" in message) == ("error" in message):
            raise ValueError("response must contain exactly one of result or error")
        error = None
        if "error" in message:
            raw = message["error"]
            if not isinstance(raw, dict) or not isinstance(raw.get("message"), str) or \
                    isinstance(raw.get("code"), bool) or not isinstance(raw.get("code"), int):
                raise ValueError("invalid response error")
            error = ACPError(raw["message"], code=raw["code"], data=raw.get("data"))
        with self._lock:
            pending = self._pending.pop(response_id, None)
            if pending is not None:  # Late responses to expired requests are ignored.
                pending.result, pending.error = message.get("result"), error
                if error is None and pending.observer is not None:
                    # Observe receipts in wire order with preceding/following
                    # notifications, even when their callback worker is delayed.
                    with self._notification_condition:
                        self._notifications_submitted += 1
                    self._notifications.put(_ResponseObservation(pending.observer,pending.result))
                pending.event.set()

    def drain_notifications(self, timeout: float | None = 60.0) -> None:
        """Wait for notifications received before this call to finish in order.

        Reader progress and nested requests remain independent. Call this after
        session/prompt returns; it includes all updates preceding its response.
        A callback exception counts as finished. Raises ACPError on timeout.
        """
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be nonnegative or None")
        if threading.current_thread() is self._notification_worker:
            raise ACPError("Cannot drain notifications from their callback", kind="state")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._notification_condition:
            target = self._notifications_submitted
            while self._notifications_completed < target:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise ACPError("Timed out draining ACP notifications", kind="timeout")
                self._notification_condition.wait(remaining)

    def _notification_loop(self) -> None:
        while True:
            message = self._notifications.get()
            try:
                if message is None:
                    return
                try:
                    if isinstance(message,_ResponseObservation):
                        try:message.callback(message.value)
                        except Exception:pass
                    else:
                        self._handle_callback(message)
                finally:
                    with self._notification_condition:
                        self._notifications_completed += 1
                        self._notification_condition.notify_all()
            finally:
                self._notifications.task_done()

    def _handle_callback(self, message: dict) -> None:
        if "id" not in message:
            if self.on_notification is not None:
                try:
                    self.on_notification(message["method"], message.get("params", {}))
                except Exception:
                    pass  # Application notification failures do not poison transport.
            return
        response = {"jsonrpc": "2.0", "id": message["id"]}
        try:
            if self.on_request is None:
                raise ACPError("Server requests are disabled", code=-32601)
            response["result"] = self.on_request(message["method"], message.get("params", {}))
        except Exception as exc:
            response["error"] = {"code": exc.code if isinstance(exc, ACPError) and exc.code is not None else -32603,
                                 "message": str(exc)}
            if isinstance(exc, ACPError) and exc.data is not None:
                response["error"]["data"] = exc.data
        try:
            self._send(response)
        except ACPError:
            pass

    def close(self) -> None:
        """Fail waiters and terminate this process group, including descendants."""
        with self._close_lock:
            with self._lock:
                if self._closed:
                    return
                self._closed = True
                process = self._process
            self._fail(ACPError("ACP client closed", kind="closed"))
            if process is None:
                return
            # Reap an exited leader first: macOS can return EPERM for its zombie
            # group. Descendants keep the group alive after the leader is reaped.
            process.poll()
            # The group may outlive its leader; signal it even if poll() is done.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    break
                except PermissionError:
                    # A leader may exit between poll() and killpg(). Reap that
                    # zombie and retry; do not hide genuine permission failures.
                    if process.poll() is None:
                        raise
                    try:
                        os.killpg(process.pid, sig)
                    except ProcessLookupError:
                        break
                    except PermissionError:
                        # macOS may retain a zombie-only group after SIGTERM.
                        # Do not report failed cleanup when no executable member
                        # remains; never hide an error for a live or unknown group.
                        snapshot=subprocess.run(['ps','-axo','pgid=,stat='],capture_output=True,text=True,timeout=3)
                        if snapshot.returncode!=0:raise
                        members=[line.split()[1] for line in snapshot.stdout.splitlines()
                                 if len(line.split())==2 and line.split()[0]==str(process.pid)]
                        if any(not state.startswith('Z') for state in members):raise
                        break
                if sig == signal.SIGTERM:
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        pass
            process.wait()
            # Killing the group unblocks any concurrent pipe write/read.
            with self._write_lock:
                process.stdin.close()
            if self._reader and self._reader is not threading.current_thread():
                self._reader.join(timeout=1)
            process.stdout.close()
            self._notifications.put(None)
            if self._notification_worker is not threading.current_thread():
                self._notification_worker.join(timeout=1)

    def __enter__(self) -> "ACPClient":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()
