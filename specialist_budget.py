"""Process-safe, content-free accounting for scarce specialist calls."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo


class BudgetUnavailable(ValueError):
    """The ledger cannot safely authorize a specialist call."""


DEFAULT = {
    'per_task': 1, 'per_day': 1, 'rolling_days': 7, 'max_calls': 3,
    'max_high_calls': 1, 'max_units': 6,
    'effort_units': {'low': 1, 'medium': 2, 'high': 3},
}
_SHANGHAI = ZoneInfo('Asia/Shanghai')


def _config(config):
    result = {**DEFAULT, **(config or {})}
    if type(result.get('limits_enabled', True)) is not bool:
        raise ValueError('Invalid specialist limits_enabled')
    result['effort_units'] = {**DEFAULT['effort_units'], **result['effort_units']}
    for key in ('per_task', 'per_day', 'rolling_days', 'max_calls', 'max_high_calls', 'max_units'):
        if type(result[key]) is not int or result[key] < (1 if key == 'rolling_days' else 0):
            raise ValueError('Invalid specialist budget ' + key)
    if any(type(v) is not int or v < 1 for v in result['effort_units'].values()):
        raise ValueError('Invalid specialist effort units')
    return result


def _timestamp(now):
    if now is None:
        return datetime.now(timezone.utc).timestamp()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            raise ValueError('Budget time must have a timezone')
        now = now.timestamp()
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
        raise ValueError('Invalid budget timestamp')
    try:
        datetime.fromtimestamp(now, timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError('Invalid budget timestamp') from exc
    return float(now)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate ledger key')
        result[key] = value
    return result


def _day(timestamp):
    return datetime.fromtimestamp(timestamp, _SHANGHAI).date()


class SpecialistBudget:
    def __init__(self, root):
        self.directory = Path(root) / '.runtime'
        self.path = self.directory / 'specialist-budget.json'
        self.lock_path = self.directory / 'specialist-budget.lock'

    @contextmanager
    def _locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield self._read()
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self):
        try:
            data = json.loads(self.path.read_text(), object_pairs_hook=_unique_object)
        except FileNotFoundError:
            return {'version': 1, 'jobs': {}}
        except (OSError, ValueError) as exc:
            raise BudgetUnavailable('Specialist budget ledger unreadable') from exc
        try:
            if not isinstance(data, dict) or set(data) != {'version', 'jobs'} or type(data['version']) is not int or data['version'] != 1 or not isinstance(data['jobs'], dict):
                raise ValueError('Invalid ledger schema')
            for job_id, row in data['jobs'].items():
                if not isinstance(job_id, str) or not job_id or not isinstance(row, dict) or set(row) != {'task_id', 'effort', 'units', 'reserved_at', 'started_at'}:
                    raise ValueError('Invalid reservation schema')
                if not isinstance(row['task_id'], str) or not row['task_id'] or not isinstance(row['effort'], str) or not row['effort'] or type(row['units']) is not int or row['units'] < 1:
                    raise ValueError('Invalid reservation identity')
                _timestamp(row['reserved_at'])
                if row['started_at'] is not None:
                    _timestamp(row['started_at'])
        except (TypeError, ValueError, OverflowError) as exc:
            raise BudgetUnavailable('Specialist budget ledger corrupt') from exc
        return data

    def _write(self, data):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=self.directory, prefix='.specialist-budget-', delete=False) as stream:
                temporary = stream.name
                json.dump(data, stream, sort_keys=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary is not None:
                os.unlink(temporary)

    @staticmethod
    def _status(data, config, task_id, effort, now, exclude=None):
        if effort is not None and effort not in config['effort_units']:
            raise ValueError('Unsupported specialist effort')
        rows = [row for key, row in data['jobs'].items() if key != exclude]
        # Pending work keeps its place across day boundaries and restarts.
        rolling = [r for r in rows if r['started_at'] is None or r['started_at'] > now - config['rolling_days'] * 86400]
        daily = [r for r in rows if r['started_at'] is None or _day(r['started_at']) == _day(now)]
        counters = {
            'task_calls': sum(r['task_id'] == task_id for r in rows) if task_id is not None else 0,
            'day_calls': len(daily), 'rolling_calls': len(rolling),
            'rolling_high_calls': sum(r['effort'] == 'high' for r in rolling),
            'rolling_units': sum(r['units'] for r in rolling),
            'pending_calls': sum(r['started_at'] is None for r in rows),
        }
        units = config['effort_units'][effort] if effort else min(config['effort_units'].values())
        limits = [
            (task_id is not None and counters['task_calls'] >= config['per_task'], 'per_task'),
            (counters['day_calls'] >= config['per_day'], 'per_day'),
            (counters['rolling_calls'] >= config['max_calls'], 'max_calls'),
            (effort == 'high' and counters['rolling_high_calls'] >= config['max_high_calls'], 'max_high_calls'),
            (counters['rolling_units'] + units > config['max_units'], 'max_units'),
        ]
        reason = next((name for exceeded, name in limits if exceeded), None) if config.get('limits_enabled', True) else None
        return {'allowed': reason is None, 'reason': reason or ('available' if config.get('limits_enabled', True) else 'unlimited'), 'limits_enabled': config.get('limits_enabled', True), 'counters': counters}

    def status(self, config, task_id=None, effort=None, now=None):
        config, now = _config(config), _timestamp(now)
        with self._locked() as data:
            return self._status(data, config, task_id, effort, now)

    def reserve(self, job_id, task_id, effort, config, now=None):
        if not isinstance(job_id, str) or not job_id or not isinstance(task_id, str) or not task_id:
            raise ValueError('Specialist reservation requires job and task IDs')
        config, now = _config(config), _timestamp(now)
        if effort not in config['effort_units']:
            raise ValueError('Unsupported specialist effort')
        with self._locked() as data:
            existing = data['jobs'].get(job_id)
            if existing:
                result = self._status(data, config, task_id, effort, now, exclude=job_id)
                same = existing['task_id'] == task_id and existing['effort'] == effort
                return {**result, 'allowed': same, 'reason': 'already_reserved' if same else 'reservation_identity_mismatch'}
            result = self._status(data, config, task_id, effort, now)
            if result['allowed']:
                data['jobs'][job_id] = {'task_id': task_id, 'effort': effort, 'units': config['effort_units'][effort], 'reserved_at': now, 'started_at': None}
                self._write(data)
            return result

    def start(self, job_id, config, now=None):
        config, now = _config(config), _timestamp(now)
        with self._locked() as data:
            row = data['jobs'].get(job_id)
            if row is None:
                return {'allowed': False, 'reason': 'reservation_missing', 'counters': {}}
            result = self._status(data, config, row['task_id'], row['effort'], now, exclude=job_id)
            if row['started_at'] is not None:
                return {**result, 'allowed': True, 'reason': 'already_started', 'start_token': row['started_at']}
            # Preserve reserved units if policy becomes less costly, charge more if it rises.
            charged_units = max(row['units'], config['effort_units'][row['effort']])
            if config.get('limits_enabled', True) and result['allowed'] and result['counters']['rolling_units'] + charged_units > config['max_units']:
                result = {**result, 'allowed': False, 'reason': 'max_units'}
            if result['allowed']:
                row['units'], row['started_at'] = charged_units, now
                self._write(data)
                result = {**result, 'start_token': row['started_at']}
            return result

    def rollback_start(self, job_id, start_token, *, process_never_started=False):
        """Undo a charge only with caller proof no provider process ever started."""
        if process_never_started is not True:
            return False
        with self._locked() as data:
            row = data['jobs'].get(job_id)
            if row is None or row['started_at'] is None or type(start_token) not in (int, float) or start_token != row['started_at']:
                return False
            del data['jobs'][job_id]
            self._write(data)
            return True

    def release(self, job_id):
        with self._locked() as data:
            row = data['jobs'].get(job_id)
            if row is None or row['started_at'] is not None:
                return False
            del data['jobs'][job_id]
            self._write(data)
            return True

    def reconcile(self, active_job_ids=None):
        """Remove unstarted reservations absent from an authoritative live-job set."""
        with self._locked() as data:
            if active_job_ids is None:
                return []
            active = set(active_job_ids)
            removed = [key for key, row in data['jobs'].items() if row['started_at'] is None and key not in active]
            for key in removed:
                del data['jobs'][key]
            if removed:
                self._write(data)
            return removed
