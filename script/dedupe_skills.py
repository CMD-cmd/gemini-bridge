"""Keep the primary workflow and hide the redundant plugin viewing shortcut."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import time

VIEW = 'gemini-bridge-local:gemini-bridge-view'


def _last_setting(text, names, paths=()):
    found = None
    for section in re.findall(r'^\s*\[\[skills\.config\]\][^\n]*\n(.*?)(?=^\s*\[|\Z)', text, re.M|re.S):
        def field(key):
            match = re.search(r'^\s*'+key+r'\s*=\s*(["\'])(.*?)\1\s*(?:#.*)?$', section, re.M)
            return match[2] if match else None
        if field('name') in names or field('path') in paths:
            enabled = re.search(r'^\s*enabled\s*=\s*(true|false)\s*(?:#.*)?$', section, re.M)
            if enabled:
                found = enabled[1] == 'true'
    return found


def configure(home, apply=False):
    home = Path(home).expanduser()
    primary = home/'skills/gemini-bridge/SKILL.md'
    config = home/'config.toml'
    before = config.read_text() if config.exists() else ''
    primary_enabled = primary.is_file() and _last_setting(before, ('gemini-bridge',), (str(primary),)) is not False
    hidden = _last_setting(before, (VIEW,)) is False
    status = {'primary_workflow_enabled': primary_enabled, 'view_shortcut_hidden': hidden, 'changed': False}
    if not apply or hidden:
        return status
    if not primary_enabled:
        raise ValueError('Install and enable the main gemini-bridge Skill before hiding its viewing shortcut.')
    section = '\n[[skills.config]]\nname = '+json.dumps(VIEW)+'\nenabled = false\n'
    home.mkdir(parents=True, exist_ok=True)
    if config.exists():
        backup = config.with_name('config.toml.bridge-skills-backup-'+str(time.time_ns()))
        shutil.copy2(config, backup)
        backup.chmod(0o600)
    temp = config.with_name(config.name+'.bridge-skills-'+str(os.getpid())+'.tmp')
    try:
        fd = os.open(temp, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(before+section)
        if (config.read_text() if config.exists() else '') != before:
            raise ValueError('Codex settings changed during the update; the current file was preserved.')
        os.replace(temp, config)
    finally:
        temp.unlink(missing_ok=True)
    return dict(status, view_shortcut_hidden=True, changed=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Save only the redundant viewing-Skill setting')
    parser.add_argument('--codex-home', type=Path, default=Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex'))
    args = parser.parse_args()
    print(json.dumps(configure(args.codex_home, args.apply)))


if __name__ == '__main__':
    main()
