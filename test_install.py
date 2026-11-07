"""Local setup regression checks; never log in, generate, or alter real settings."""
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parent


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'script'/f'{name}.py')
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'checkout'
        self.root.mkdir()
        shutil.copytree(ROOT/'routing', self.root/'routing', ignore=shutil.ignore_patterns('policy.json', '__pycache__'))
        shutil.copytree(ROOT/'skills', self.root/'skills')
        self.install = module('install')
        self.connect = module('connect_codex')
        self.install.ROOT = self.connect.ROOT = self.root
        self.codex = Path(self.temp.name)/'codex'
        example = json.loads((self.root/'routing/policy.example.json').read_text())
        selected = next(v for v in example['external_models'].values() if not v.get('specialist_only'))
        self.args = ['--setup', '--skip-dependencies', '--cli', sys.executable,
                     '--model', selected['model'], '--effort', selected['reasoning_effort']]

    def invoke(self, args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return self.install.main(args)

    def test_check_has_no_setup_side_effects(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        self.invoke(['--check'])
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))

    def test_unknown_mapping_fails_before_writing(self):
        args = self.args.copy()
        args[args.index('--model')+1] = 'unreviewed-future-model'
        with self.assertRaises(SystemExit):
            self.invoke(args+['--adopt-example-policy'])
        self.assertFalse((self.root/'profile.json').exists())

    def test_existing_profile_is_preserved(self):
        path = self.root/'profile.json'
        path.write_text('{"existing":true}')
        with self.assertRaises(SystemExit):
            self.invoke(self.args)
        self.assertEqual(path.read_text(), '{"existing":true}')

    def test_example_policy_needs_explicit_exceptions_and_keeps_global_routing(self):
        self.invoke(self.args+['--adopt-example-policy'])
        policy = json.loads((self.root/'routing/policy.json').read_text())
        self.assertFalse(policy['external_specialist_exception']['enabled'])
        self.assertFalse(policy['external_subscription_exception']['enabled'])
        self.assertFalse((self.codex/'agent-routing').exists())

    def test_explicit_member_exceptions_and_installed_paths(self):
        self.invoke(self.args+['--adopt-example-policy','--subscription-exceptions',
                              '--install-skills','--codex-home',str(self.codex)])
        policy = json.loads((self.root/'routing/policy.json').read_text())
        self.assertTrue(policy['external_specialist_exception']['enabled'])
        skill = self.codex/'skills/gemini-bridge'
        self.assertEqual(json.loads((skill/'runtime-location.json').read_text())['bridge_root'], str(self.root))
        self.assertNotIn('<BRIDGE_ROOT>', (skill/'SKILL.md').read_text())
        self.assertTrue((skill/'assets/gemini.png').is_file())
        self.assertEqual((self.root/'bin/agy').resolve(), Path(sys.executable).resolve())

    def test_existing_skill_is_preserved_before_profile_creation(self):
        skill = self.codex/'skills/gemini-bridge'
        skill.mkdir(parents=True)
        (skill/'SKILL.md').write_text('existing')
        with self.assertRaises(SystemExit):
            self.invoke(self.args+['--install-skills','--codex-home',str(self.codex)])
        self.assertEqual((skill/'SKILL.md').read_text(), 'existing')
        self.assertFalse((self.root/'profile.json').exists())

    def test_existing_quoted_mcp_entry_is_not_replaced(self):
        self.codex.mkdir()
        path = self.codex/'config.toml'
        text = '[mcp_servers."gemini_bridge_http"]\nurl="existing"\n'
        path.write_text(text)
        with patch.object(self.connect.subprocess, 'run') as run, self.assertRaises(ValueError):
            self.connect.main(['--write-config','--codex-home',str(self.codex)])
        run.assert_not_called()
        self.assertEqual(path.read_text(), text)

    def test_config_merge_keeps_others_and_does_not_print_credential(self):
        self.codex.mkdir()
        path = self.codex/'config.toml'
        original = '[mcp_servers.other]\ncommand="keep"\n'
        path.write_text(original)
        (self.root/'.venv-http/bin').mkdir(parents=True)
        (self.root/'.venv-http/bin/python').touch()
        (self.root/'.runtime').mkdir()
        (self.root/'.runtime/http-mcp.json').write_text(json.dumps({'url':'http://127.0.0.1:50252/mcp','token':'synthetic-secret'}))
        output = io.StringIO()
        with patch.object(self.connect.subprocess, 'run'), redirect_stdout(output):
            self.connect.main(['--write-config','--codex-home',str(self.codex)])
        self.assertTrue(path.read_text().startswith(original))
        self.assertIn('[mcp_servers.gemini_bridge_http]', path.read_text())
        self.assertNotIn('synthetic-secret', output.getvalue())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(list(self.codex.glob('config.toml.bridge-backup-*'))), 1)

    def test_http_restart_retains_connection_identity(self):
        runtime = module('http_mcp_runtime')
        runtime.RUNTIME = self.root/'.runtime'
        runtime.RUNTIME.mkdir()
        runtime.STATE = runtime.RUNTIME/'http-mcp.json'
        identity = {'token': 'a'*64, 'instance': 'b'*48, 'url': runtime.URL,
                    'pid': 123, 'process_start': 'old'}
        with patch.object(runtime, 'healthy', side_effect=[False, True]), \
             patch.object(runtime, 'owned_process', return_value=False), \
             patch.object(runtime.socket, 'socket'), \
             patch.object(runtime.subprocess, 'Popen', return_value=Mock(pid=456)), \
             patch.object(runtime.subprocess, 'check_output', return_value='new'), \
             patch.object(runtime, 'status', return_value={'running': True}):
            self.assertEqual(runtime.start(identity), {'running': True})
        saved = json.loads(runtime.STATE.read_text())
        self.assertEqual(saved['token'], identity['token'])
        self.assertEqual(saved['instance'], identity['instance'])
        self.assertEqual(saved['pid'], 456)


class PickerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.helper = module('dedupe_skills')

    def primary(self):
        path = self.home/'skills/gemini-bridge/SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text('primary workflow')
        return path

    def test_check_never_changes_settings(self):
        self.assertFalse(self.helper.configure(self.home)['changed'])
        self.assertFalse((self.home/'config.toml').exists())

    def test_no_primary_cannot_hide_only_viewer(self):
        with self.assertRaises(ValueError):
            self.helper.configure(self.home, True)
        self.assertFalse((self.home/'config.toml').exists())

    def test_dedup_keeps_other_settings_and_is_idempotent(self):
        self.primary()
        config = self.home/'config.toml'
        original = '[mcp_servers.existing]\ncommand="keep"\n'
        config.write_text(original)
        self.assertTrue(self.helper.configure(self.home, True)['changed'])
        text = config.read_text()
        self.assertTrue(text.startswith(original))
        self.assertIn('name = "gemini-bridge-local:gemini-bridge-view"', text)
        self.assertFalse(self.helper.configure(self.home, True)['changed'])
        self.assertEqual(config.read_text(), text)
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_disabled_main_workflow_is_preserved(self):
        self.primary()
        config = self.home/'config.toml'
        original = '[[skills.config]]\nname="gemini-bridge"\nenabled=false\n'
        config.write_text(original)
        with self.assertRaises(ValueError):
            self.helper.configure(self.home, True)
        self.assertEqual(config.read_text(), original)


if __name__ == '__main__':
    unittest.main()
