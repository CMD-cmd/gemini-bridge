"""Headless native child roles. These profiles grant no authority without guard policy."""
from __future__ import annotations
import hashlib

ROLES = ('reader', 'builder', 'reviewer')
READ_TOOLS = ['view_file', 'read_file', 'list_dir', 'find_by_name', 'grep_search', 'send_message']
WRITE_TOOLS = ['write_to_file', 'replace_file_content', 'multi_replace_file_content']


def validate_roles(roles=None):
    roles = ['reader'] if roles is None else roles
    if (not isinstance(roles, list) or not roles
            or any(not isinstance(role, str) or role not in ROLES for role in roles)
            or len(set(roles)) != len(roles)):
        raise ValueError('Native roles must be unique reader/builder/reviewer roles')
    return list(roles)


def build_profiles(job_id, roles=None):
    """Return roles, exact job-scoped type_names and profiles keyed by TypeName.

    Caller persists native tool configuration and adds enabled/max_children/model/
    effort to policy.native_subagents. Profiles have role, description,
    system_prompt and tools; no filesystem authority comes from these strings.
    """
    if not isinstance(job_id, str) or not job_id:
        raise ValueError('Native profiles require a job ID')
    roles = validate_roles(roles)
    token = hashlib.sha256(job_id.encode()).hexdigest()[:20]
    names = {role: 'bridge-' + role + '-' + token for role in roles}
    profiles = {}
    for role, name in names.items():
        instruction = ('Read relevant scoped sources and return evidence to the parent.' if role == 'reader' else
                       'Independently review the scoped work and report findings with evidence.' if role == 'reviewer' else
                       'Implement only the assigned change. Write deliverable files or patches only inside '
                       'the exact isolated output directory supplied in your invocation. The parent integrates '
                       'changes and runs commands. Never edit the shared worktree or another child directory.')
        profiles[name] = {'role': role, 'description': 'Task-scoped native ' + role,
            'system_prompt': instruction + ' Send messages only to the supplied parent conversation ID. '
                'Do not delegate, run commands, change workspace, or use MCP.',
            'tools': READ_TOOLS + (WRITE_TOOLS if role == 'builder' else [])}
    return {'roles': roles, 'type_names': names, 'profiles': profiles}
