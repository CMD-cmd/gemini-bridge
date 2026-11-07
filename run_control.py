"""In-process admission for same-session ACP prompt steering; never replays.

Adapter contract: ``send(text, *, send_guard)`` must wrap ONLY its actual
transport write + flush in ``with send_guard():``. Waiting for the provider's
response belongs outside that context. The context serializes write with close;
``on_sent`` alone cannot provide that guarantee. No provider acknowledgement or
requirement execution is implied by a successful transport write or response.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import threading


class SteeringClosed(ValueError):
    """The active turn ended before the native write began."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _identifier(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', value):
        raise ValueError(f'invalid {name}')
    return value


class _Channel:
    def __init__(self, job_id, sid, send, directory, records):
        self.job_id, self.sid, self.send = job_id, sid, send
        self.path = Path(directory) / 'steering.json'
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.inflight = set()
        self.active = send is not None
        self.read_only = send is None
        self.records = records


class RunControlRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._channels = {}

    def register(self, job_id, sid, send_callable, job_dir):
        _identifier(job_id, 'job_id')
        if not isinstance(sid, str) or not sid.strip() or len(sid) > 500:
            raise ValueError('invalid session_id')
        if not callable(send_callable):
            raise ValueError('send_callable is required')
        with self._lock:
            if job_id in self._channels:
                raise ValueError('job already registered or restored')
            channel = self._load(job_id, job_dir, sid, send_callable)
            self._channels[job_id] = channel
            with channel.lock:
                self._persist(channel)

    def restore(self, job_id, job_dir):
        """Load a historical journal as read-only; pending entries never resend."""
        _identifier(job_id, 'job_id')
        with self._lock:
            if job_id not in self._channels:
                self._channels[job_id] = self._load(job_id, job_dir)
        return self.records(job_id)

    def _load(self, job_id, directory, sid=None, send=None):
        path = Path(directory) / 'steering.json'
        records = {}
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('job_id') != job_id or data.get('schema_version') != 1:
                raise ValueError('steering journal does not match job')
            for record in data['records']:
                key = _identifier(record['steering_id'], 'steering_id')
                if key in records:
                    raise ValueError('duplicate journal steering_id')
                records[key] = deepcopy(record)
            # A previous process cannot establish that its turn is still active.
            if send is not None:
                raise ValueError('historical steering journal is read-only; do not reactivate')
            sid = sid or data.get('session_id')
            for record in records.values():
                previous = record.get('status')
                if previous in ('queued', 'sent'):
                    status = 'rejected' if previous == 'queued' else 'unknown'
                    record['status'] = status
                    record['reason'] = 'process_restarted'
                    record['recovered_from_status'] = previous
                    record['updated_at'] = _now()
                    record['events'].append({'status': status, 'at': record['updated_at'],
                                             'reason': 'process_restarted'})
        return _Channel(job_id, sid, send, directory, records)

    @staticmethod
    def _compact(record):
        return {key: deepcopy(value) for key, value in record.items() if key not in ('text', 'events')}

    def _get(self, job_id):
        _identifier(job_id, 'job_id')
        with self._lock:
            return self._channels.get(job_id)

    def submit(self, job_id, steering_id, text):
        _identifier(steering_id, 'steering_id')
        if not isinstance(text, str) or not text.strip() or len(text) > 16000 or '\x00' in text:
            raise ValueError('text must contain 1 to 16000 characters without NUL')
        channel = self._get(job_id)
        if channel is None:
            raise ValueError('job is not registered')
        with channel.lock:
            old = channel.records.get(steering_id)
            if old is not None:
                if old['text'] != text:
                    raise ValueError('steering_id conflicts with existing text')
                return self._compact(old)
            record = {'job_id': job_id, 'steering_id': steering_id,
                      'session_id': channel.sid, 'text': text, 'text_length': len(text),
                      'created_at': _now(), 'events': []}
            channel.records[steering_id] = record
            self._transition(channel, record, 'queued' if channel.active else 'rejected',
                             None if channel.active else 'turn_closed')
            result = self._compact(record)
            if channel.active:
                worker = threading.Thread(target=self._send, args=(channel, record),
                                          name=f'steering-{steering_id}', daemon=True)
                worker.start()
            return result

    def _transition(self, channel, record, status, reason=None):
        record['status'] = status
        record['updated_at'] = _now()
        record[status + '_at'] = record['updated_at']
        if reason:
            record['reason'] = reason
        else:
            record.pop('reason', None)
        record['events'].append({'status': status, 'at': record['updated_at'],
                                 **({'reason': reason} if reason else {})})
        self._persist(channel)

    @contextmanager
    def _send_guard(self, channel, record):
        with channel.lock:
            if not channel.active or record['status'] != 'queued':
                if record['status'] == 'queued':
                    self._transition(channel, record, 'rejected', 'turn_closed')
                raise SteeringClosed('turn closed before steering write')
            channel.inflight.add(record['steering_id'])
            # Persist uncertainty BEFORE touching transport, for crash recovery.
            self._transition(channel, record, 'unknown', 'write_in_progress')
            try:
                yield
            except BaseException:
                self._transition(channel, record, 'unknown', 'transport_write_failed')
                raise
            else:
                self._transition(channel, record, 'sent')

    def _send(self, channel, record):
        try:
            response = channel.send(record['text'], send_guard=lambda: self._send_guard(channel, record))
            with channel.lock:
                if record['status'] in ('sent', 'unknown') and 'sent_at' in record:
                    # Keep only the protocol stop reason, never response text/tokens.
                    if isinstance(response, dict) and isinstance(response.get('stopReason'), str):
                        record['stop_reason'] = response['stopReason'][:200]
                    self._transition(channel, record, 'responded')
                elif record['status'] == 'queued':
                    self._transition(channel, record, 'unknown', 'adapter_did_not_use_send_guard')
        except SteeringClosed:
            pass
        except Exception:
            # Exception strings may contain provider payloads or credentials.
            with channel.lock:
                if record['status'] != 'rejected':
                    self._transition(channel, record, 'unknown', 'send_or_response_failed')
        finally:
            with channel.condition:
                channel.inflight.discard(record['steering_id'])
                channel.condition.notify_all()

    def wait_settled(self, job_id, timeout=0):
        """Wait only for RPCs whose write started; admission close is separate."""
        channel = self._get(job_id)
        if channel is None:
            return True
        with channel.condition:
            return channel.condition.wait_for(lambda: not channel.inflight, timeout)

    def terminal_evidence(self, job_id):
        channel = self._get(job_id)
        if channel is None:
            return {'settled': True, 'all_sent_terminal': True, 'pending_count': 0}
        terminal = {'end_turn', 'cancelled', 'max_tokens', 'max_turn_requests', 'refusal'}
        with channel.lock:
            return {'settled': not channel.inflight, 'pending_count': len(channel.inflight),
                    'all_sent_terminal': not channel.inflight and all(
                        row['status'] == 'rejected' or
                        row['status'] == 'responded' and row.get('stop_reason') in terminal
                        for row in channel.records.values())}

    def close(self, job_id):
        channel = self._get(job_id)
        if channel is None:
            return []
        with channel.lock:
            channel.active = False
            for record in channel.records.values():
                if record['status'] == 'queued':
                    self._transition(channel, record, 'rejected', 'turn_closed')
                # A live sent RPC remains sent while its response is pending.
                # Admission close is not evidence of transport loss; the worker
                # records unknown only if the write/response actually fails.
            return [self._compact(record) for record in channel.records.values()]

    def records(self, job_id):
        channel = self._get(job_id)
        if channel is None:
            return []
        with channel.lock:
            return [self._compact(record) for record in channel.records.values()]

    @staticmethod
    def _persist(channel):
        if channel.read_only:
            return
        channel.path.parent.mkdir(parents=True, exist_ok=True)
        value = {'schema_version': 1, 'job_id': channel.job_id, 'session_id': channel.sid,
                 'records': list(channel.records.values())}
        fd, temporary = tempfile.mkstemp(prefix='.steering-', dir=channel.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, channel.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


registry = RunControlRegistry()
