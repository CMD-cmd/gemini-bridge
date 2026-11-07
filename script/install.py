"""Configure this checkout without touching another Bridge or global routing policy."""
from __future__ import annotations
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def write_new(path, value):
    if path.exists():
        raise ValueError(f"Already exists; preserve and review it before updating: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)


def install_skill(source, destination):
    if destination.exists():
        raise ValueError(f"Skill already exists; it was not replaced: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    for path in destination.rglob('*'):
        if path.is_file() and path.suffix in ('.md', '.json', '.yaml'):
            path.write_text(path.read_text().replace('<BRIDGE_ROOT>', str(ROOT)))
    write_new(destination / 'runtime-location.json', {'bridge_root': str(ROOT)})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Read-only prerequisite check')
    parser.add_argument('--cli', help='Path to an already-installed official agy executable')
    parser.add_argument('--model', help='Exact identifier from your own agy models output')
    parser.add_argument('--effort', choices=('low', 'medium', 'high', 'xhigh', 'max'))
    parser.add_argument('--setup', action='store_true')
    parser.add_argument('--skip-dependencies', action='store_true', help='Use an existing environment')
    parser.add_argument('--install-skills', action='store_true')
    parser.add_argument('--keep-view-shortcut', action='store_true', help='Keep the optional separate plugin viewing Skill in the picker')
    parser.add_argument('--codex-home', type=Path, default=Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex'))
    parser.add_argument('--adopt-example-policy', action='store_true', help='Explicitly adopt the dated routing example')
    parser.add_argument('--subscription-exceptions', action='store_true', help='Opt into the example member-route policy exceptions')
    args = parser.parse_args(argv)
    cli = Path(args.cli or shutil.which('agy') or ROOT/'bin/agy').expanduser()
    ready = cli.is_file() and os.access(cli, os.X_OK)
    if args.check or not args.setup:
        print(json.dumps({'python': sys.version.split()[0], 'platform': sys.platform,
                          'official_cli_found': ready, 'profile_configured': (ROOT/'profile.json').exists(),
                          'routing_policy_configured': (ROOT/'routing/policy.json').exists(),
                          'mcp_environment_ready': (ROOT/'.venv-http/bin/python').exists(),
                          'generation_started': False}, ensure_ascii=False))
        return 0
    if sys.platform != 'darwin':
        parser.error('This release supports macOS; other systems have not been validated.')
    if sys.version_info < (3, 10):
        parser.error('Python 3.10 or newer is required.')
    if not ready or not args.model or not args.effort:
        parser.error('Use --cli with an installed official CLI, plus --model and --effort from its catalog.')
    if args.subscription_exceptions and not args.adopt_example_policy:
        parser.error('--subscription-exceptions requires --adopt-example-policy')
    if (ROOT/'profile.json').exists() or (args.adopt_example_policy and (ROOT/'routing/policy.json').exists()):
        parser.error('Existing configuration found; no settings were replaced.')
    key = args.model + '/' + args.effort
    policy = json.loads((ROOT/'routing/policy.example.json').read_text())
    if args.adopt_example_policy and key not in policy['external_models']:
        parser.error('This exact combination is absent from the example policy. Supply a reviewed policy; do not infer eligibility from catalog presence.')
    if args.install_skills and (args.codex_home/'skills/gemini-bridge').exists():
        parser.error('An installed gemini-bridge Skill exists; no files were replaced.')
    if not args.skip_dependencies:
        env = ROOT/'.venv-http'
        if not (env/'bin/python').exists():
            venv.EnvBuilder(with_pip=True).create(env)
        subprocess.run([str(env/'bin/python'), '-m', 'pip', 'install', '-r', str(ROOT/'requirements-http.lock')], check=True)
    link = ROOT/'bin/agy'
    link.parent.mkdir(exist_ok=True)
    if link.exists() or link.is_symlink():
        if link.resolve() != cli.resolve():
            raise ValueError('bin/agy already points elsewhere; it was preserved.')
    else:
        link.symlink_to(cli.resolve())
    write_new(ROOT/'profile.json', {'model': args.model, 'effort': args.effort, 'timeout_seconds': 600})
    if args.adopt_example_policy:
        if args.subscription_exceptions:
            for field in ('external_subscription_exception', 'external_specialist_exception'):
                policy[field]['enabled'] = True
                policy[field]['authorization'] = 'The installing user explicitly adopted this example with --subscription-exceptions; chat-level permission remains required.'
                policy[field]['authorized_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        write_new(ROOT/'routing/policy.json', policy)
    if args.install_skills:
        install_skill(ROOT/'skills/gemini-bridge', args.codex_home/'skills/gemini-bridge')
        if not args.keep_view_shortcut:
            subprocess.run([sys.executable, str(Path(__file__).with_name('dedupe_skills.py')),
                            '--apply', '--codex-home', str(args.codex_home)], check=True)
    print('Local setup complete. Sign in through the official CLI, then run the connect helper. No model generation was started.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
