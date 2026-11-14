import base64
import concurrent.futures
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from workspace_guard import gate, post, run_sandboxed, _path, _SandboxProcesses, _tee
from unittest.mock import patch


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.job = self.root / 'job'
        self.source = self.root / 'source'
        self.output = self.root / 'output'
        for p in (self.job, self.source, self.output):
            p.mkdir()
        self.policy = dict(job_dir=str(self.job), model='test-model', read_roots=[str(self.source)],
                           write_roots=[str(self.output)], protected_paths=[],
                           command_prefixes=[['/bin/echo'], ['/bin/sleep'], ['/usr/bin/python3']],
                           command_profile=str(self.job / 'sandbox.sb'), command_timeout_seconds=1,
                           command_runner=str(Path(__file__).parent / 'script/workspace_command.py'),
                           python=sys.executable, audit_path=str(self.job / 'audit.jsonl'),
                           events_path=str(self.job / 'events.jsonl'))
        (self.job / 'workspace-policy.json').write_text(json.dumps(self.policy))
        self.conversation = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'

    def payload(self, tool, args, step=1):
        return dict(modelName='test-model', conversationId=self.conversation,
                    workspacePaths=[str(self.job)],
                    artifactDirectoryPath=str(Path.home() / '.gemini/antigravity-cli/brain' / self.conversation),
                    stepIdx=step, toolCall=dict(name=tool, args=args))

    def decision(self, tool, args, expected='auto_approve'):
        result = gate(self.policy, self.payload(tool, args))
        self.assertEqual(result['decision'], expected, result)
        return result

    def test_current_cli_read_file_alias_keeps_paths_and_audit(self):
        source = self.source / 'current.txt'
        source.write_text('current CLI reference')
        self.decision('read_file', {'Path': str(source)})
        record = json.loads(Path(self.policy['audit_path']).read_text().splitlines()[-1])
        self.assertEqual(record['path'], str(source))
        self.assertEqual(record['file_sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
        self.decision('read_file', {'Path': str(self.root / 'outside.txt')}, 'deny')
        self.decision('read_file', {'Path': str(self.source / '.env')}, 'deny')
        self.decision('read_file', {'AbsolutePath': str(source)}, 'deny')
        self.decision('read_file', {'Path': 'relative.txt'}, 'deny')
        (self.source / 'escape').symlink_to(self.root / 'outside.txt')
        self.decision('read_file', {'Path': str(self.source / 'escape')}, 'deny')

    def test_normal_read_write(self):
        self.decision('view_file', {'AbsolutePath': str(self.source / 'a.txt')})
        self.decision('write_to_file', {'TargetFile': str(self.output / 'new/sub/file.txt'), 'CodeContent': 'secret body'})
        self.decision('replace_file_content', {'TargetFile': str(self.source / 'a.txt')}, 'deny')
        self.assertNotIn('secret body', Path(self.policy['audit_path']).read_text())

    def test_current_native_write_edit_aliases_preserve_same_scope(self):
        for name,body in [('write_file',{'Content':'private text','Overwrite':False}),
                          ('edit_file',{'Target':'before','Replacement':'after'})]:
            with self.subTest(tool=name):
                target=self.output/'allowed.txt'
                self.decision(name,{'Path':str(target),**body})
                self.decision(name,{'Path':str(self.source/'original.txt'),**body},'deny')
                self.decision(name,{'Path':str(self.output/'.env'),**body},'deny')
                self.decision(name,{'Path':'relative.txt',**body},'deny')
                self.decision(name,{'TargetFile':str(target),**body},'deny')
                link=self.output/(name+'-escape');link.symlink_to(self.root/'outside.txt')
                self.decision(name,{'Path':str(link),**body},'deny')
        self.assertNotIn('private text',Path(self.policy['audit_path']).read_text())

    def test_prefix_symlink_hardlink(self):
        self.decision('view_file', {'AbsolutePath': str(self.root / 'source-evil/a')}, 'deny')
        (self.output / 'escape').symlink_to(self.root)
        self.decision('write_to_file', {'TargetFile': str(self.output / 'escape/brand-new/a')}, 'deny')
        original = self.source / 'original'
        original.write_text('original')
        os.link(original, self.output / 'linked')
        self.decision('replace_file_content', {'TargetFile': str(self.output / 'linked')}, 'deny')

    def test_controls_credentials_unknown(self):
        for name in ('.env', '.env.local', '.codex/config.toml', '.agents/hooks.json', '.git/config', '.ssh/id_rsa', 'private.pem'):
            self.decision('view_file', {'AbsolutePath': str(self.source / name)}, 'deny')
        self.policy['write_roots'].append(str(self.job))
        for name in ('workspace-policy.json', 'workspace-guard-state.json', 'workspace-guard.lock', 'workspace-guard-state.tmp', 'sandbox.sb', 'audit.jsonl'):
            self.decision('write_to_file', {'TargetFile': str(self.job / name)}, 'deny')
        self.policy['write_roots'].append(str(Path(__file__).parent.resolve()))
        self.decision('replace_file_content', {'TargetFile': str(Path(__file__).parent.resolve() / 'workspace_guard.py')}, 'deny')
        self.decision('browser', {}, 'deny')
        self.decision('delete_file', {'TargetFile': str(self.output / 'a')}, 'deny')
        self.decision('command_status', {'CommandId': 'foreign-id'}, 'deny')

    def test_identity_and_explicit_deny(self):
        for key, value in [('modelName', 'other'), ('workspacePaths', []), ('artifactDirectoryPath', '/tmp'), ('conversationId', '')]:
            payload = self.payload('list_dir', {'DirectoryPath': str(self.source)})
            payload[key] = value
            self.assertEqual(gate(self.policy, payload)['decision'], 'deny')
        self.decision('list_dir', {'DirectoryPath': str(self.source)})
        self.conversation = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
        self.decision('list_dir', {'DirectoryPath': str(self.source)}, 'deny')

    def test_commands_literal_rewrite(self):
        result = self.decision('run_command', dict(CommandLine='/bin/echo "hello world" "a;b"', Cwd=str(self.source)))
        argv = shlex.split(result['overwrite']['CommandLine'])
        request = json.loads(base64.urlsafe_b64decode(argv[-1]))
        self.assertEqual(request['argv'], ['/bin/echo', 'hello world', 'a;b'])
        self.assertEqual(argv[1], self.policy['command_runner'])
        for command in ('/bin/echo a; /bin/echo b', '/bin/echo a > file', '/bin/echo a|cat', '/bin/echo `id`', '/bin/echo $(id)', '/bin/echo a\n/bin/echo b', '/bin/echo a && echo b', '/bin/echo a &'):
            self.decision('run_command', dict(CommandLine=command, Cwd=str(self.source)), 'deny')
        self.decision('run_command', dict(CommandLine='/bin/echo ok', Cwd=str(self.source), RunPersistent=True), 'deny')
        self.decision('run_command', dict(CommandLine='/bin/echo-evil ok', Cwd=str(self.source)), 'deny')
        payload = self.payload('run_command', dict(CommandLine='/bin/echo ok', Cwd=str(self.source)))
        payload['permissionDecision'] = 'deny'
        self.assertEqual(gate(self.policy, payload)['decision'], 'deny')

    def test_audit_concurrent_dedup_and_post(self):
        payload = self.payload('view_file', {'AbsolutePath': str(self.source / 'a')})
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(lambda _: gate(self.policy, payload), range(10)))
        self.assertTrue(all(x['decision'] == 'auto_approve' for x in results))
        self.assertEqual(len(Path(self.policy['audit_path']).read_text().splitlines()), 1)
        self.assertEqual(post(self.policy, payload), {})
        records = [json.loads(x) for x in Path(self.policy['audit_path']).read_text().splitlines()]
        self.assertEqual([x['kind'] for x in records], ['pre', 'post'])

    def test_read_file_hash_evidence(self):
        source = self.source / 'file.bin'
        content = b'confidential body' * 100000
        source.write_bytes(content)
        self.decision('view_file', {'AbsolutePath': str(source)})
        record = json.loads(Path(self.policy['audit_path']).read_text().splitlines()[0])
        self.assertEqual(record['file_sha256'], hashlib.sha256(content).hexdigest())
        self.assertEqual(record['file_size'], len(content))
        self.assertNotIn('confidential body', Path(self.policy['audit_path']).read_text())

    @unittest.skipUnless(Path('/usr/bin/sandbox-exec').exists(), 'macOS sandbox required')
    def test_executable_mapping_and_bytecode_environment(self):
        self.sandbox_profile()
        self.policy['command_prefixes'].append(['python3'])
        self.policy['executable_map'] = {'python3': str(Path(sys.executable).resolve())}
        output = self.output / 'interpreter.json'
        code = 'import sys,os,json;from pathlib import Path;Path(' + repr(str(output)) + ').write_text(json.dumps([sys.executable,os.getenv("PYTHONDONTWRITEBYTECODE")]))'
        self.assertEqual(run_sandboxed(self.policy, ['python3', '-c', code], str(self.source)), 0)
        self.assertEqual(json.loads(output.read_text()), [str(Path(sys.executable).resolve()), '1'])
        self.policy['executable_map'] = {'python3': 'relative/python'}
        with self.assertRaises(ValueError):
            run_sandboxed(self.policy, ['python3', '-V'], str(self.source))

    @unittest.skipUnless(Path('/usr/bin/sandbox-exec').exists(), 'macOS sandbox required')
    def test_job_state_cancellation_and_marker(self):
        self.sandbox_profile()
        self.policy['command_timeout_seconds'] = 10
        state = self.job / 'state.json'
        state.write_text(json.dumps({'status': 'running'}))
        timer = threading.Timer(0.3, lambda: state.write_text(json.dumps({'status': 'cancelling'})))
        timer.start()
        started = time.monotonic()
        try:
            self.assertEqual(run_sandboxed(self.policy, ['/bin/sleep', '10'], str(self.source)), 143)
        finally:
            timer.join()
        self.assertLess(time.monotonic() - started, 2)
        for status in ('cancelled', 'timeout', 'interrupted', 'failed_or_unverified'):
            state.write_text(json.dumps({'status': status}))
            self.assertEqual(run_sandboxed(self.policy, ['/bin/sleep', '10'], str(self.source)), 124 if status == 'timeout' else 143)
        state.write_text(json.dumps({'status': 'running'}))
        (self.job / 'cancel.flag').touch()
        self.assertEqual(run_sandboxed(self.policy, ['/bin/sleep', '10'], str(self.source)), 143)

    def test_recursive_search_checks_links_each_time(self):
        (self.source / '.git').mkdir()
        (self.source / '.env').write_text('secret')
        for tool, field in [('list_dir', 'DirectoryPath'), ('find_by_name', 'SearchDirectory'), ('grep_search', 'SearchPath')]:
            self.decision(tool, {field: str(self.source)})
            link = self.source / 'new-link'
            link.symlink_to(self.root)
            self.decision(tool, {field: str(self.source)}, 'deny')
            link.unlink()
            link.symlink_to(self.source / '.env')
            self.decision(tool, {field: str(self.source)}, 'deny')
            link.unlink()
        ordinary = self.source / 'ordinary'
        ordinary.write_text('text')
        os.link(ordinary, self.source / 'hard-link')
        self.decision('grep_search', {'SearchPath': str(self.source)}, 'deny')

    def test_runtime_controls_except_own_job(self):
        fake_root = self.root / 'runtime'
        policy = dict(self.policy)
        policy['job_dir'] = str(fake_root / 'runs/current')
        policy['read_roots'] = [str(fake_root)]
        policy['write_roots'] = [str(fake_root / 'runs/current')]
        with patch('workspace_guard.__file__', str(fake_root / 'workspace_guard.py')):
            for relative in ('.runtime/queue', 'runs/other/artifacts/a.txt'):
                with self.assertRaises(ValueError):
                    _path(policy, str(fake_root / relative))
            for relative in ('bridge.py', 'profile.json', 'nested/config.json', 'script/a.sh', 'bin/x'):
                self.assertEqual(_path(policy, str(fake_root / relative)), str(fake_root / relative))
                with self.assertRaises(ValueError):
                    _path(policy, str(fake_root / relative), write=True)
            policy['archive_read_roots'] = [str(fake_root / 'runs/previous/artifacts')]
            self.assertEqual(_path(policy, str(fake_root / 'runs/previous/artifacts/a.json')), str(fake_root / 'runs/previous/artifacts/a.json'))
            with self.assertRaises(ValueError):
                _path(policy, str(fake_root / 'runs/previous/state.json'))
            self.assertEqual(_path(policy, str(fake_root / 'runs/current/artifacts/out.json'), write=True),
                             str(fake_root / 'runs/current/artifacts/out.json'))

    @unittest.skipUnless(Path('/usr/bin/sandbox-exec').exists(), 'macOS sandbox required')
    def test_setsid_descendants_cleaned_on_parent_exit(self):
        self.sandbox_profile()
        marker = self.output / 'escaped-marker'
        child = 'import time;from pathlib import Path;time.sleep(0.8);Path(' + repr(str(marker)) + ').write_text("escaped")'
        code = 'import subprocess,sys;[subprocess.Popen([sys.executable,"-c",' + repr(child) + '],start_new_session=True) for _ in range(5)]'
        # An untagged process created by this test must never be killed.
        unrelated = subprocess.Popen(['/bin/sleep', '10'], start_new_session=True)
        try:
            self.assertEqual(run_sandboxed(self.policy, ['/usr/bin/python3', '-c', code], str(self.source)), 0)
            self.assertIsNone(unrelated.poll())
            time.sleep(1)
            self.assertFalse(marker.exists())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=2)

    @unittest.skipUnless(Path('/usr/bin/sandbox-exec').exists(), 'macOS sandbox required')
    def test_venv_launch_path_and_identity(self):
        self.sandbox_profile()
        venv = self.root / 'venv'
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True, capture_output=True)
        launch = venv / 'bin/python3'
        resolved = launch.resolve()
        stat = resolved.stat()
        self.policy['command_prefixes'].append(['python3'])
        self.policy['executable_map'] = {'python3': str(launch)}
        self.policy['executable_identities'] = {'python3': {'resolved_path': str(resolved), 'dev': stat.st_dev, 'ino': stat.st_ino}}
        output = self.output / 'venv.json'
        code = 'import sys,json;from pathlib import Path;Path(' + repr(str(output)) + ').write_text(json.dumps([sys.prefix,sys.executable]))'
        self.assertEqual(run_sandboxed(self.policy, ['python3', '-c', code], str(self.source)), 0)
        self.assertEqual(json.loads(output.read_text()), [str(venv), str(launch)])
        self.policy['executable_identities']['python3']['ino'] += 1
        with self.assertRaises(ValueError):
            run_sandboxed(self.policy, ['python3', '-V'], str(self.source))

    @unittest.skipUnless(Path('/usr/bin/sandbox-exec').exists(), 'macOS sandbox required')
    def test_nested_sandbox_reinitialization_is_fail_closed(self):
        self.sandbox_profile()
        outer = self.job / 'outer.sb'
        outer.write_text('(version 1)(allow default)(deny mach-lookup)')
        inner = Path(self.policy['command_profile'])
        inner.write_text(inner.read_text() + '\n(deny mach-lookup)\n')
        marker = self.output / 'nested-escape'
        child = 'import time;from pathlib import Path;time.sleep(0.7);Path(' + repr(str(marker)) + ').write_text("escaped")'
        code = 'import subprocess,sys;subprocess.Popen([sys.executable,"-c",' + repr(child) + '],start_new_session=True)'
        encoded = base64.urlsafe_b64encode(json.dumps({'argv': ['/usr/bin/python3', '-c', code], 'cwd': str(self.source)}).encode()).decode()
        completed = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(outer), sys.executable,
                                    self.policy['command_runner'], str(self.job / 'workspace-policy.json'), encoded, '--execute-local'], capture_output=True, timeout=5)
        # This macOS refuses changing a Seatbelt profile after initialization.
        # The wrapper must fail closed rather than executing without its profile.
        self.assertEqual(completed.returncode, 71, completed.stderr.decode())
        self.assertIn(b'sandbox_apply', completed.stderr)
        self.assertFalse(marker.exists())

    @unittest.skipUnless(Path('/usr/bin/sandbox-exec').exists(), 'macOS sandbox required')
    def test_command_records_exit_logs_protection_and_dedup(self):
        self.sandbox_profile()
        directory = self.job / 'command-results'
        self.policy['command_results_dir'] = str(directory)
        counter = self.output / 'count'
        code = 'import sys;from pathlib import Path; p=Path(' + repr(str(counter)) + ');p.write_text(p.read_text()+"x" if p.exists() else "x");print("stdout receipt");print("stderr receipt",file=sys.stderr);sys.exit(7)'
        record_id = 'a' * 64
        argv = ['/usr/bin/python3', '-c', code]
        self.assertEqual(run_sandboxed(self.policy, argv, str(self.source), record_id, 'b' * 64), 7)
        self.assertEqual(run_sandboxed(self.policy, argv, str(self.source), record_id, 'b' * 64), 7)
        self.assertEqual(counter.read_text(), 'x')
        record = json.loads((directory / (record_id + '.json')).read_text())
        self.assertEqual((record['status'], record['exit_code'], record['runner_exit_code']), ('exited', 7, 7))
        self.assertIn('stdout receipt', Path(record['stdout_path']).read_text())
        self.assertIn('stderr receipt', Path(record['stderr_path']).read_text())
        self.decision('view_file', {'AbsolutePath': str(directory / (record_id + '.json'))})
        self.policy['write_roots'].append(str(self.job))
        self.decision('write_to_file', {'TargetFile': str(directory / 'forged.json')}, 'deny')
        attack = 'from pathlib import Path;Path(' + repr(str(directory / 'forged.json')) + ').write_text("forged")'
        self.assertNotEqual(run_sandboxed(self.policy, ['/usr/bin/python3', '-c', attack], str(self.source), 'c' * 64), 0)
        self.assertFalse((directory / 'forged.json').exists())
        with self.assertRaises(ValueError):
            run_sandboxed(self.policy, ['/bin/echo', 'different'], str(self.source), record_id, 'b' * 64)
        result = self.decision('run_command', {'CommandLine': '/bin/echo receipt', 'Cwd': str(self.source)})
        self.assertEqual(result['overwrite']['WaitMsBeforeAsync'], 10000)
        self.assertIn(str(directory), result['reason'])
        request = json.loads(base64.urlsafe_b64decode(shlex.split(result['overwrite']['CommandLine'])[-1]))
        self.assertRegex(request['record_id'], '^[0-9a-f]{64}$')

    @unittest.skipUnless(Path('/usr/bin/sandbox-exec').exists(), 'macOS sandbox required')
    def test_command_path_env_shebang(self):
        self.sandbox_profile()
        scripts = self.root / 'tools'
        scripts.mkdir()
        (scripts / 'bridge-python').symlink_to(Path(sys.executable).resolve())
        launch = scripts / 'hello'
        launch.write_text('#!/usr/bin/env bridge-python\nimport sys;sys.exit(9)\n')
        launch.chmod(0o700)
        self.policy['command_prefixes'].append(['hello'])
        self.policy['executable_map'] = {'hello': str(launch)}
        self.policy['command_path'] = [str(scripts), '/usr/bin', '/bin']
        self.assertEqual(run_sandboxed(self.policy, ['hello'], str(self.source)), 9)
        self.policy['command_path'] = ['relative']
        with self.assertRaises(ValueError):
            run_sandboxed(self.policy, ['hello'], str(self.source))

    @unittest.skipUnless(Path('/usr/bin/sandbox-exec').exists(), 'macOS sandbox required')
    def test_file_probe_with_mach_lookup_denied(self):
        self.sandbox_profile()
        cache = self.job / 'command-cache'
        cache.mkdir()
        members = _SandboxProcesses(cache)
        profile = self.job / 'probe.sb'
        profile.write_text('(version 1)(allow default)(deny mach-lookup)' + members.rules())
        marker = self.output / 'probe-escape'
        child = 'import time;from pathlib import Path;time.sleep(0.8);Path(' + repr(str(marker)) + ').write_text("escaped")'
        code = 'import subprocess,sys;subprocess.Popen([sys.executable,"-c",' + repr(child) + '],start_new_session=True)'
        parent = subprocess.Popen(['/usr/bin/sandbox-exec', '-f', str(profile), '/usr/bin/python3', '-c', code])
        self.assertEqual(parent.wait(timeout=3), 0)
        self.assertTrue(members.pids())
        members.terminate()
        time.sleep(1)
        self.assertFalse(marker.exists())

    def test_default_shim_fake_broker_and_no_fallback(self):
        directory = self.job / 'command-results'
        directory.mkdir()
        self.policy['command_results_dir'] = str(directory)
        policy_path = self.job / 'workspace-policy.json'
        policy_path.write_text(json.dumps(self.policy))
        record_id = 'd' * 64
        request = {'argv': ['/bin/echo', 'original'], 'cwd': str(self.source), 'record_id': record_id}
        encoded = base64.urlsafe_b64encode(json.dumps(request).encode()).decode()
        observed = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                observed.append((handler.path, handler.headers.get('Authorization'), json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))))
                (directory / (record_id + '.stdout.log')).write_text('broker stdout')
                (directory / (record_id + '.stderr.log')).write_text('broker stderr')
                (directory / (record_id + '.json')).write_text(json.dumps({'record_id': record_id, 'status': 'exited', 'runner_exit_code': 6}))
                handler.send_response(200)
                handler.end_headers()
                handler.wfile.write(json.dumps({'record_path': str(directory / (record_id + '.json'))}).encode())
            def log_message(handler, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = dict(os.environ, GEMINI_BRIDGE_COMMAND_URL='http://127.0.0.1:' + str(server.server_port) + '/jobs/test/commands', GEMINI_BRIDGE_COMMAND_TOKEN='job-only-token', HTTP_PROXY='http://127.0.0.1:1', http_proxy='http://127.0.0.1:1')
        command = [sys.executable, self.policy['command_runner'], str(policy_path), encoded]
        try:
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 6, result.stderr)
            self.assertIn('broker stdout', result.stdout)
            self.assertIn('broker stderr', result.stderr)
            self.assertEqual(observed[0], ('/jobs/test/commands', 'Bearer job-only-token', request))
            env.pop('GEMINI_BRIDGE_COMMAND_TOKEN')
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 126)
            self.assertNotIn('original', result.stdout)
            env['GEMINI_BRIDGE_COMMAND_TOKEN'] = 'job-only-token'
            env['GEMINI_BRIDGE_COMMAND_URL'] = 'http://example.com/jobs/test/commands'
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 126)
            self.assertEqual(len(observed), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_gate_binds_exact_command_request_to_record_id(self):
        self.policy['command_results_dir'] = str(self.job / 'command-results')
        args = {'CommandLine': '/bin/echo "hello world"', 'Cwd': str(self.source)}
        result = self.decision('run_command', args)
        request = json.loads(base64.urlsafe_b64decode(shlex.split(result['overwrite']['CommandLine'])[-1]))
        state = json.loads((self.job / 'workspace-guard-state.json').read_text())
        expected = {'argv': ['/bin/echo', 'hello world'], 'cwd': str(self.source), 'args_sha256': request['args_sha256']}
        self.assertEqual(state['command_requests'], {request['record_id']: expected})
        self.decision('run_command', args)
        self.assertEqual(json.loads((self.job / 'workspace-guard-state.json').read_text())['command_requests'], state['command_requests'])
        state['command_requests'][request['record_id']]['argv'] = ['/bin/echo', 'tampered']
        (self.job / 'workspace-guard-state.json').write_text(json.dumps(state))
        self.decision('run_command', args, 'deny')
        self.policy.pop('command_results_dir')
        self.decision('run_command', {'CommandLine': '/bin/echo no-record', 'Cwd': str(self.source)})
        self.assertEqual(len(json.loads((self.job / 'workspace-guard-state.json').read_text())['command_requests']), 1)

    def test_tee_flushes_log_before_process_finishes(self):
        read_fd, write_fd = os.pipe()
        sink = os.open(os.devnull, os.O_WRONLY)
        source = os.fdopen(read_fd, 'rb')
        log = self.output / 'stream.log'
        thread = threading.Thread(target=_tee, args=(source, sink, log))
        thread.start()
        try:
            os.write(write_fd, b'already printed\n')
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if log.exists() and log.read_bytes() == b'already printed\n':
                    break
                time.sleep(0.01)
            self.assertTrue(thread.is_alive(), 'The writer pipe must remain open during verification')
            self.assertEqual(log.read_bytes(), b'already printed\n')
        finally:
            os.close(write_fd)
            thread.join(timeout=2)
            os.close(sink)

    @unittest.skipUnless(Path('/usr/bin/sandbox-exec').exists(), 'macOS sandbox required')
    def test_probe_namespace_cannot_be_deleted_before_setsid(self):
        self.sandbox_profile()
        control = self.job / 'command-control'
        report = self.output / 'probe-attempts.json'
        marker = self.output / 'probe-late-write'
        child = 'import time;from pathlib import Path;time.sleep(0.8);Path(' + repr(str(marker)) + ').write_text("escaped")'
        body = ('import subprocess,sys,json\nfrom pathlib import Path\n'
                'control=Path(' + repr(str(control)) + ')\n'
                'attempts=[]\n'
                'for probe in control.glob("probe-*"):\n'
                ' for action in (lambda p=probe:p.unlink(), lambda p=probe:p.rename(' + repr(str(self.output / 'moved-probe')) + '), lambda p=probe:p.chmod(0o000)):\n'
                '  try: action(); attempts.append("allowed")\n'
                '  except OSError: attempts.append("denied")\n'
                'try: control.rename(' + repr(str(self.output / 'moved-control')) + '); attempts.append("allowed")\n'
                'except OSError: attempts.append("denied")\n'
                'Path(' + repr(str(report)) + ').write_text(json.dumps(attempts))\n'
                'subprocess.Popen([sys.executable,"-c",' + repr(child) + '],start_new_session=True)')
        self.assertEqual(run_sandboxed(self.policy, ['/usr/bin/python3', '-c', 'exec(' + repr(body) + ')'], str(self.source)), 0)
        attempts = json.loads(report.read_text())
        self.assertEqual(len(attempts), 7)
        self.assertEqual(set(attempts), {'denied'})
        time.sleep(1)
        self.assertFalse(marker.exists())

    @unittest.skipUnless(Path('/usr/bin/sandbox-exec').exists(), 'macOS sandbox required')
    def test_unicode_sandbox_marker_detection_reaping_and_result_protection(self):
        directory = self.root / '中文任务 空格'
        directory.mkdir()
        control = directory / '命令控制'; control.mkdir()
        members = _SandboxProcesses(control)
        profile = directory / 'marker.sb'
        profile.write_text('(version 1)\n(allow default)\n' + members.rules())
        child = subprocess.Popen(['/usr/bin/sandbox-exec', '-f', str(profile), '/bin/sleep', '30'])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and child.pid not in members.pids():
            time.sleep(.02)
        self.assertIn(child.pid, members.pids(), 'Unicode-path sandbox marker must identify the running child')
        members.terminate()
        self.assertEqual(child.wait(timeout=3), -signal.SIGKILL)
        self.assertNotIn(child.pid, members.pids())

        job = directory / '工作任务'; job.mkdir()
        results = job / 'command-results'; results.mkdir()
        base = directory / 'commands.sb'; base.write_text('(version 1)\n(allow default)\n(deny network*)\n')
        policy = dict(self.policy, job_dir=str(job), command_profile=str(base), command_timeout_seconds=5)
        denied = directory / '拒绝结果.json'
        forged = results / '伪造.png'
        late = directory / '逃逸写入'
        detached = 'import time;from pathlib import Path;time.sleep(.8);Path(' + repr(str(late)) + ').write_text("late")'
        body = ('import subprocess,sys,json\nfrom pathlib import Path\n'
                'try: Path(' + repr(str(forged)) + ').write_text("forged"); denied=False\n'
                'except OSError: denied=True\n'
                'Path(' + repr(str(denied)) + ').write_text(json.dumps(denied))\n'
                'subprocess.Popen([sys.executable,"-c",' + repr(detached) + '],start_new_session=True)')
        self.assertEqual(run_sandboxed(policy, ['/usr/bin/python3', '-c', 'exec(' + repr(body) + ')'], str(self.source)), 0)
        self.assertIs(json.loads(denied.read_text()), True)
        self.assertFalse(forged.exists())
        time.sleep(1)
        self.assertFalse(late.exists(), 'Detached child must be reaped for Unicode task paths')

    def test_native_command_without_cwd_uses_trusted_default_only(self):
        self.policy['working_directory'] = str(self.source)
        self.policy['command_results_dir'] = str(self.job / 'command-results')
        result = self.decision('run_command', {'CommandLine': '/bin/echo native'})
        request = json.loads(base64.urlsafe_b64decode(shlex.split(result['overwrite']['CommandLine'])[-1]))
        self.assertEqual(request['cwd'], str(self.source))
        state = json.loads((self.job / 'workspace-guard-state.json').read_text())
        self.assertEqual(state['command_requests'][request['record_id']]['cwd'], str(self.source))
        for cwd in ('', None, 'relative', str(self.root / 'outside')):
            self.decision('run_command', {'CommandLine': '/bin/echo native', 'Cwd': cwd}, 'deny')
        self.policy.pop('working_directory')
        self.decision('run_command', {'CommandLine': '/bin/echo native'}, 'deny')

    def test_hook_policy_load_failure_deny(self):
        result = subprocess.run([sys.executable, 'script/workspace_tool_guard.py', str(self.job / 'missing')],
                                input='{}', capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)['decision'], 'deny')

    def sandbox_profile(self):
        profile = Path(self.policy['command_profile'])
        profile.write_text('(version 1)\n(allow default)\n(deny network*)\n(deny file-write* (subpath ' + json.dumps(str(self.source)) + '))\n')
        probe = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(profile), '/bin/echo', 'probe'], capture_output=True)
        if probe.returncode != 0:
            self.skipTest('sandbox-exec not available in this execution environment: ' + probe.stderr.decode()[:120])

    @unittest.skipUnless(Path('/usr/bin/sandbox-exec').exists(), 'macOS sandbox required')
    def test_sandbox_write_network_and_environment(self):
        self.sandbox_profile()
        output = self.output / 'result.json'
        code = 'import os,json,socket; from pathlib import Path; denied=False\ntry: socket.socket().connect(("1.1.1.1",443))\nexcept OSError: denied=True\nPath(' + repr(str(output)) + ').write_text(json.dumps([os.getenv("BRIDGE_TEST_TOKEN"),denied,os.getenv("HOME")]))\nPath(' + repr(str(self.source / 'forbidden')) + ').write_text("bad")'
        code = 'exec(' + repr(code) + ')'
        os.environ['BRIDGE_TEST_TOKEN'] = 'not-for-child'
        self.addCleanup(os.environ.pop, 'BRIDGE_TEST_TOKEN', None)
        self.assertNotEqual(run_sandboxed(self.policy, ['/usr/bin/python3', '-c', code], str(self.source)), 0)
        result = json.loads(output.read_text())
        self.assertEqual(result[:2], [None, True])
        self.assertEqual(result[2], str(self.job / 'command-tmp'))
        self.assertFalse((self.source / 'forbidden').exists())

    @unittest.skipUnless(Path('/usr/bin/sandbox-exec').exists(), 'macOS sandbox required')
    def test_timeout_and_cancel_process_group(self):
        self.sandbox_profile()
        started = time.monotonic()
        self.assertEqual(run_sandboxed(self.policy, ['/bin/sleep', '10'], str(self.source)), 124)
        self.assertLess(time.monotonic() - started, 4)
        marker = self.output / 'descendant'
        code = 'import subprocess,time; subprocess.Popen(["/bin/sh","-c",' + repr('sleep 2; touch ' + shlex.quote(str(marker))) + ']); time.sleep(10)'
        encoded = base64.urlsafe_b64encode(json.dumps({'argv': ['/usr/bin/python3', '-c', code], 'cwd': str(self.source)}).encode()).decode()
        child = subprocess.Popen([sys.executable, self.policy['command_runner'], str(self.job / 'workspace-policy.json'), encoded, '--execute-local'])
        time.sleep(0.3)
        child.terminate()
        self.assertEqual(child.wait(timeout=3), 143)
        time.sleep(2.1)
        self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
