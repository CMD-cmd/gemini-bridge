"""Pure argv adaptation for the project's Antigravity CLI. No process/service IO."""
import math
from pathlib import Path

# Verified against the installed agy --help, not inferred for other providers.
_FLAGS = {'print': '--print', 'conversation': '--conversation', 'continue_latest': '--continue',
          'model': '--model', 'effort': '--effort', 'agent': '--agent', 'sandbox': '--sandbox',
          'input_format': '--input-format', 'output_format': '--output-format',
          'print_timeout': '--print-timeout', 'disable_slash_commands': '--disable-slash-commands'}


class AntigravityAdapter:
    provider = 'antigravity'
    aliases = ('agy', 'gemini')

    def descriptor(self, capabilities=None):
        declared = None if capabilities is None else set(capabilities.get('declared', {}).get('flags', []))
        flags = {name: flag for name, flag in _FLAGS.items() if declared is None or flag in declared}
        return {'provider': self.provider, 'aliases': list(self.aliases), 'flags': flags,
                'supports': {name: name in flags for name in _FLAGS},
                'evidence': 'installed CLI help declarations; generation not tested by adapter'}

    def build_new_task_args(self, root, *, model=None, effort=None, agent=None,
                            prompt=None, input_format='stream-json', output_format='stream-json',
                            timeout_seconds=None, sandbox=True, capabilities=None):
        desc = self.descriptor(capabilities)
        args = [str(Path(root) / 'bin' / 'agy')]

        def add(name, value=None):
            if name not in desc['flags']:
                raise ValueError(f'Antigravity CLI does not declare {name}')
            args.append(desc['flags'][name])
            if value is not None:
                args.append(str(value))

        for name, value in [('model', model), ('effort', effort), ('agent', agent)]:
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f'{name} must be a nonempty string')
                add(name, value)
        if sandbox:
            add('sandbox')
        if 'disable_slash_commands' in desc['flags']:
            add('disable_slash_commands')
        if input_format not in (None, 'text', 'stream-json') or output_format not in (None, 'text', 'json', 'stream-json'):
            raise ValueError('Unsupported IO format')
        if input_format == 'stream-json' and output_format != 'stream-json':
            raise ValueError('stream-json input requires stream-json output')
        if prompt is not None:
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError('prompt must be a nonempty string')
            if input_format == 'stream-json':
                raise ValueError('Use stdin for stream-json input; prompt requires text input')
            add('print', prompt)
        if input_format is not None:
            add('input_format', input_format)
        if output_format is not None:
            add('output_format', output_format)
        if timeout_seconds is not None:
            if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds < 0:
                raise ValueError('timeout_seconds must be nonnegative')
            add('print_timeout', f'{timeout_seconds:g}s')
        return args

    def build_continue_args(self, root, conversation_id, **kwargs):
        if not isinstance(conversation_id, str) or not conversation_id.strip() or conversation_id.startswith('-'):
            raise ValueError('conversation_id must be a nonempty provider ID')
        desc = self.descriptor(kwargs.get('capabilities'))
        if not desc['supports']['conversation']:
            raise ValueError('Antigravity CLI does not declare conversation')
        return self.build_new_task_args(root, **kwargs) + [desc['flags']['conversation'], conversation_id]


_ADAPTER = AntigravityAdapter()


def get_adapter(provider='antigravity'):
    if provider not in ('antigravity', *_ADAPTER.aliases):
        raise ValueError(f'Unsupported runner provider: {provider}')
    return _ADAPTER


def descriptor(capabilities=None):
    return _ADAPTER.descriptor(capabilities)


def build_new_task_args(root, **kwargs):
    return _ADAPTER.build_new_task_args(root, **kwargs)


def build_continue_args(root, conversation_id, **kwargs):
    return _ADAPTER.build_continue_args(root, conversation_id, **kwargs)
