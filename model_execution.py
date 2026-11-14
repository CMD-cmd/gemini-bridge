"""Policy-snapshotted model execution contract, separate from workspace mode."""
import copy

ADJUSTABLE_EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
EFFORTS = (*ADJUSTABLE_EFFORTS, 'fixed')


def validate(profile):
    """Fixed is a configuration sentinel, never a provider thinking level."""
    effort = profile.get('effort')
    if effort not in EFFORTS:
        raise ValueError('Invalid effort')
    value = profile.get('model_execution')
    if value is None:
        if effort == 'fixed':
            raise ValueError('Fixed effort requires a registered model execution contract')
        return None
    if (not isinstance(value, dict) or value.get('mode') != 'orchestrated'
            or value.get('effort_mode') != 'fixed' or effort != 'fixed'
            or not isinstance(value.get('label'), str) or not value['label'].strip()
            or len(value['label']) > 160):
        raise ValueError('Invalid fixed model execution contract')
    components = value.get('components')
    if (not isinstance(components, list) or not 2 <= len(components) <= 8
            or any(not isinstance(c, dict) or not all(isinstance(c.get(k), str) and c[k].strip()
                   for k in ('role', 'model', 'label')) for c in components)
            or len({c['role'] for c in components}) != len(components)):
        raise ValueError('Invalid model execution components')
    names = value.get('tool_model_names')
    if (not isinstance(names, list) or not names or len(names) > 12
            or any(not isinstance(n, str) or not n.strip() for n in names)
            or profile.get('model') not in names):
        raise ValueError('Invalid exact tool model identities')
    return value


def bind(profile, record):
    """Only the trusted exact policy record may attach an execution contract."""
    out = dict(profile)
    out.pop('model_execution', None)
    if record and record.get('model_execution') is not None:
        if (record.get('model'), record.get('reasoning_effort')) != (profile.get('model'), profile.get('effort')):
            raise ValueError('Model execution policy identity mismatch')
        out['model_execution'] = copy.deepcopy(record['model_execution'])
    validate(out)
    return out


def tool_identity_allowed(policy, observed):
    value = policy.get('model_execution')
    if value is None:
        return observed == policy.get('model')
    validate(policy)
    return observed in value['tool_model_names']
