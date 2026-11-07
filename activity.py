"""Bounded, incremental observation of one CLI process and conversation.

Only hashes and step lifecycle state survive event handling. Provider-side queue,
rate limit and thinking states are not exposed by the native CLI.
"""
import hashlib
import json

READ_BYTES = 256 * 1024
MAX_LINE_BYTES = 1024 * 1024


def waiting_policy(profile):
    value = profile.get('waiting_policy')
    if not isinstance(value, dict) or value.get('mode') != 'activity':
        return None
    result = dict(value)
    defaults = {'timeout_seconds': profile.get('timeout_seconds', 180),
                'hard_timeout_seconds': 600, 'activity_grace_seconds': 120,
                'quiet_grace_seconds': 120, 'quiet_notice_seconds': 60,
                'hard_timeout_grace_seconds': 30}
    for key, default in defaults.items():
        number = result.get(key, default)
        if type(number) is not int or not 0 <= number <= 1800:
            raise ValueError('Invalid waiting_policy.' + key)
        result[key] = number
    if not 10 <= result['timeout_seconds'] <= result['hard_timeout_seconds'] <= 1800:
        raise ValueError('Activity soft timeout must be between 10 and hard timeout (at most 1800)')
    if result['hard_timeout_grace_seconds'] > 30:
        raise ValueError('Activity hard timeout exit grace must be at most 30 seconds')
    return result


class ActivityWait:
    def __init__(self, profile, launched_at, wall_started_at):
        self.policy = waiting_policy(profile)
        if self.policy is None:
            raise ValueError('Activity policy required')
        self.launched_at = launched_at
        self.wall_started_at = wall_started_at
        self.conversation_id = None
        self.observed_state = 'initializing'
        self.meaningful_events = 0
        self.last_activity = None
        self.last_activity_wall = None
        self.offset = 0
        self.partial = b''
        self.discard_line = False
        self.steps = {}
        self.text_hashes = set()
        self.thought_activity_sequence = 0
        self.thought_activity_events = 0

    def observe(self, event, now, wall_now):
        if not isinstance(event, dict):
            return
        kind = event.get('event')
        init = event.get('init') if isinstance(event.get('init'), dict) else {}
        step = event.get('step_update') if isinstance(event.get('step_update'), dict) else {}
        conversation = event.get('conversation_id') or step.get('conversation_id') or init.get('conversation_id')
        # Bind once; never borrow activity from a different conversation.
        if conversation is not None:
            if not isinstance(conversation, str):
                return
            if self.conversation_id is not None and conversation != self.conversation_id:
                return
            self.conversation_id = conversation
        if kind == 'init':
            if self.observed_state == 'initializing':
                self.observed_state = 'awaiting_response'
            return
        if kind == 'result':
            self.observed_state = 'finalizing'
            return
        if kind != 'step_update':
            return
        meaningful = False
        index = step.get('step_index')
        step_type = step.get('step_type')
        state = step.get('state')
        key = (index, step_type)
        valid_index = (type(index) is int and index >= 0 and isinstance(step_type, str)
                       and step_type != 'agent_thought')
        prior = self.steps.get(key) if valid_index else None
        if valid_index and key not in self.steps:
            meaningful = True
        if step_type == 'tool':
            if valid_index and prior == 'ACTIVE' and state in ('DONE', 'ERROR'):
                meaningful = True
            if state == 'ACTIVE':
                self.observed_state = 'tool_running'
            elif state in ('DONE', 'ERROR'):
                self.observed_state = 'awaiting_response'
        elif step_type == 'agent_thought':
            # Only the ACP normalizer's advancing, content-free sequence counts.
            # Empty, duplicate and stale notifications cannot extend the budget.
            sequence = step.get('activity_sequence')
            if type(sequence) is int and sequence > self.thought_activity_sequence:
                self.thought_activity_sequence = sequence
                self.thought_activity_events += 1
                meaningful = True
                self.observed_state = 'thinking'
        elif step_type == 'agent_response':
            delta = step.get('text_delta')
            if isinstance(delta, str) and delta:
                digest = hashlib.sha256(delta.encode('utf-8', errors='replace')).digest()
                if digest not in self.text_hashes:
                    self.text_hashes.add(digest)
                    meaningful = True
                self.observed_state = 'receiving_response'
        if valid_index:
            # Do not let repeated/stale ACTIVE events revive a completed tool.
            if prior not in ('DONE', 'ERROR'):
                self.steps[key] = state
        if step_type == 'tool':
            self.observed_state = ('tool_running' if any(k[1] == 'tool' and v == 'ACTIVE'
                                                       for k, v in self.steps.items())
                                   else 'awaiting_response')
        if meaningful:
            self.meaningful_events += 1
            self.last_activity = max(0, now - self.launched_at)
            self.last_activity_wall = wall_now

    def read_events(self, path, now, wall_now):
        try:
            with path.open('rb') as source:
                source.seek(self.offset)
                chunk = source.read(READ_BYTES)
        except OSError:
            # Observation is best effort: preserve the cursor and partial line
            # so a transient read failure cannot abort the live CLI process.
            return
        self.offset += len(chunk)
        if not chunk:
            return
        data = self.partial + chunk
        self.partial = b''
        rows = data.split(b'\n')
        tail = rows.pop()
        for row in rows:
            if self.discard_line:
                self.discard_line = False
                continue
            if len(row) > MAX_LINE_BYTES:
                continue
            try:
                event = json.loads(row)
            except (ValueError, UnicodeDecodeError):
                continue
            self.observe(event, now, wall_now)
        if len(tail) > MAX_LINE_BYTES:
            self.discard_line = True
        elif not self.discard_line:
            self.partial = tail

    def metadata(self, now):
        p = self.policy
        baseline = p['timeout_seconds'] + p['quiet_grace_seconds']
        activity = (self.last_activity + p['activity_grace_seconds']) if self.last_activity is not None else 0
        target = max(baseline, activity)
        reason = 'recent_activity' if activity > baseline else 'quiet_grace'
        if target >= p['hard_timeout_seconds']:
            reason = 'hard_limit'
        deadline = min(p['hard_timeout_seconds'], target) + p['hard_timeout_grace_seconds']
        return {'mode': 'activity', 'observed_state': self.observed_state,
                'conversation_id': self.conversation_id,
                'soft_timeout_seconds': p['timeout_seconds'],
                'hard_timeout_seconds': p['hard_timeout_seconds'],
                'effective_deadline_seconds': deadline, 'deadline_reason': reason,
                'hard_timeout_grace_seconds': p['hard_timeout_grace_seconds'],
                'deadline_includes_exit_grace': True,
                'last_meaningful_activity_at': self.last_activity_wall,
                'meaningful_events': self.meaningful_events,
                'thought_activity_events': self.thought_activity_events,
                'soft_budget_exceeded': now - self.launched_at >= p['timeout_seconds'],
                'provider_status': 'not_exposed_by_cli'}
