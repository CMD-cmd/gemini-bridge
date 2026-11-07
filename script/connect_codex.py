"""Start the owned HTTP MCP and add only its configuration entry to Codex."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-config', action='store_true')
    parser.add_argument('--codex-home', type=Path, default=Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex'))
    args = parser.parse_args(argv)
    if not args.write_config:
        print('Use --write-config to start this checkout\'s local MCP and append its entry. Existing servers and routing settings are preserved.')
        return
    config = args.codex_home/'config.toml'
    original = config.read_text() if config.exists() else ''
    if re.search(r'^\s*\[\s*mcp_servers\s*\.\s*"?gemini_bridge_http"?(?:\s*\.|\s*\])', original, re.M):
        raise ValueError('gemini_bridge_http is already configured. Existing configuration was preserved; reconnect it or review it manually.')
    python = ROOT/'.venv-http/bin/python'
    if not python.exists():
        raise ValueError('Run install.py --setup first.')
    subprocess.run([str(python), str(ROOT/'script/http_mcp_runtime.py'), 'ensure'], check=True, stdout=subprocess.DEVNULL)
    state = json.loads((ROOT/'.runtime/http-mcp.json').read_text())
    q = lambda value: json.dumps(value, ensure_ascii=False)
    section = '\n[mcp_servers.gemini_bridge_http]\nurl = '+q(state['url'])+'\n[mcp_servers.gemini_bridge_http.http_headers]\nAuthorization = '+q('Bearer '+state['token'])+'\n'
    args.codex_home.mkdir(parents=True, exist_ok=True)
    if config.exists():
        backup = config.with_name('config.toml.bridge-backup-'+str(time.time_ns()))
        shutil.copy2(config, backup)
        backup.chmod(0o600)
    temp = config.with_name(config.name+'.bridge-'+str(os.getpid())+'.tmp')
    try:
        fd = os.open(temp, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(original+section)
        if (config.read_text() if config.exists() else '') != original:
            raise ValueError('Codex settings changed during setup; no settings were replaced.')
        os.replace(temp, config)
    finally:
        temp.unlink(missing_ok=True)
    print('Added the local Gemini Bridge MCP connection. Refresh the current chat or reconnect it in Codex. Connection credentials stayed in local files; no Gemini request was sent.')


if __name__ == '__main__':
    main()
