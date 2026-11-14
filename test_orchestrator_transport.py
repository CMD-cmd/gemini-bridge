"""Transport-only regressions; no provider or service is started."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import acp_execution
import runner_adapter


MODEL = 'gemini-pro-orchestrator'
EXECUTION = {
    'mode': 'orchestrated', 'effort_mode': 'fixed', 'label': 'Dynamic orchestrator',
    'components': [
        {'role': 'reasoning', 'model': 'component-a', 'label': 'Reasoning component'},
        {'role': 'execution', 'model': 'component-b', 'label': 'Execution component'}],
    'tool_model_names': [MODEL],
}


def session(model=MODEL, effort='high'):
    return {'sessionId': 'provider-session',
            'models': {'availableModels': [{'modelId': model}]},
            'configOptions': [
                {'id': 'model', 'currentValue': 'old-model',
                 'options': [{'value': model}]},
                {'id': 'thought_level', 'currentValue': effort}]}


class ConfigClient:
    def __init__(self, initial, *, remove_thought=False, drift=False):
        self.options = copy.deepcopy(initial['configOptions'])
        self.calls = []
        self.remove_thought = remove_thought
        self.drift = drift

    def request(self, method, params, **kwargs):
        self.calls.append((method, copy.deepcopy(params)))
        for option in self.options:
            if option['id'] == params['configId']:
                option['currentValue'] = 'wrong-model' if self.drift else params['value']
        if self.remove_thought:
            self.options = [o for o in self.options if o['id'] != 'thought_level']
        return {'configOptions': copy.deepcopy(self.options)}


class SelectionTests(unittest.TestCase):
    def test_config_options_can_offer_model_without_legacy_models_field(self):
        value=session(MODEL);value.pop('models')
        client=ConfigClient(value,remove_thought=True)
        result=acp_execution.select_model(client,value,MODEL,'fixed',EXECUTION)
        self.assertTrue(result['matches'])
        self.assertNotIn('thought_level',result['config_options'])

    def test_missing_bootstrap_requires_exact_provider_options_and_ack(self):
        value={'sessionId':'provider-session','configOptions':[{'id':'mode','currentValue':'default'}]}
        class ReadyClient:
            def request(self,method,params,**kwargs):
                result=session(MODEL)
                for option in result['configOptions']:
                    if option['id']=='model':option['currentValue']=MODEL
                return result
        result=acp_execution.select_model(ReadyClient(),value,MODEL,'fixed',EXECUTION)
        self.assertTrue(result['matches'])
        class UnreadyClient:
            def request(self,*args,**kwargs):return copy.deepcopy(value)
        with self.assertRaisesRegex(ValueError,'configuration is not ready'):
            acp_execution.select_model(UnreadyClient(),value,MODEL,'fixed',EXECUTION)

    def test_exact_fixed_only_sets_model_and_clears_stale_thought_evidence(self):
        for remove in (True, False):
            with self.subTest(provider_removes_thought=remove):
                initial = session()
                client = ConfigClient(initial, remove_thought=remove)
                chosen = acp_execution.select_model(client, initial, MODEL, 'fixed', EXECUTION)
                self.assertEqual([p['configId'] for _, p in client.calls], ['model'])
                self.assertEqual(chosen['effort'], 'fixed')
                self.assertNotIn('thought_level', chosen['config_options'])
                self.assertEqual(chosen['model_execution'], EXECUTION)
                self.assertTrue(chosen['matches'])

    def test_fixed_requires_contract_and_exact_option(self):
        with self.assertRaises(ValueError):
            acp_execution.select_model(ConfigClient(session()), session(), MODEL, 'fixed')
        initial = session()
        initial['configOptions'][0]['options'] = [{'value': 'gemini-pro'}]
        with self.assertRaises(ValueError):
            acp_execution.select_model(ConfigClient(initial), initial, MODEL, 'fixed', EXECUTION)

    def test_ordinary_model_still_sets_and_confirms_effort(self):
        initial = session('ordinary-high')
        initial['configOptions'][0]['options'] = [{'value': 'ordinary'}]
        client = ConfigClient(initial)
        chosen = acp_execution.select_model(client, initial, 'ordinary-high', 'high')
        self.assertEqual([(p['configId'], p['value']) for _, p in client.calls],
                         [('model', 'ordinary'), ('thought_level', 'high')])
        self.assertEqual(chosen['config_options']['thought_level']['currentValue'], 'high')
        self.assertNotIn('model_execution', chosen)

    def test_fixed_rejects_model_ack_drift(self):
        initial = session()
        with self.assertRaisesRegex(ValueError, 'acknowledge'):
            acp_execution.select_model(ConfigClient(initial, drift=True), initial, MODEL, 'fixed', EXECUTION)


class HeadlessTests(unittest.TestCase):
    def test_fixed_omits_effort_for_new_and_continue_even_when_flag_unavailable(self):
        flags = [flag for name, flag in runner_adapter._FLAGS.items() if name != 'effort']
        kwargs = dict(model=MODEL, effort='fixed', agent='custom', timeout_seconds=12,
                      capabilities={'declared': {'flags': flags}})
        new = runner_adapter.build_new_task_args(Path('/mock-root'), **kwargs)
        resumed = runner_adapter.build_continue_args(Path('/mock-root'), 'provider-session', **kwargs)
        self.assertNotIn('--effort', new)
        self.assertNotIn('fixed', new)
        self.assertEqual(new[new.index('--model') + 1], MODEL)
        self.assertEqual(resumed, new + ['--conversation', 'provider-session'])
        self.assertIn('--sandbox', new)
        self.assertIn('--disable-slash-commands', new)
        self.assertEqual(new[new.index('--print-timeout') + 1], '12s')

    def test_ordinary_new_and_continue_keep_effort(self):
        for build, args in ((runner_adapter.build_new_task_args, ()),
                            (runner_adapter.build_continue_args, ('provider-session',))):
            argv = build(Path('/mock-root'), *args, model='ordinary-high', effort='high')
            self.assertEqual(argv[argv.index('--effort') + 1], 'high')


class RunTests(unittest.TestCase):
    def execute(self, *, drift=False, continuation=False, fixed=True, response="result"):
        model = MODEL if fixed else 'ordinary-high'
        profile = {'model': model, 'effort': 'fixed' if fixed else 'high', 'timeout_seconds': 3}
        if fixed:
            profile['model_execution'] = copy.deepcopy(EXECUTION)
        if continuation:
            profile['continuation'] = {'provider_session_id': 'provider-session'}
        calls = []

        class FakeClient(ConfigClient):
            pid = 123

            def __init__(self, *args, **kwargs):
                super().__init__(session(model), remove_thought=fixed)
                self.notification = kwargs['on_notification']

            def start(self):
                pass

            def close(self):
                pass

            def drain_notifications(self, **kwargs):
                pass

            def request(self, method, params, **kwargs):
                calls.append((method, copy.deepcopy(params)))
                if method == 'initialize':
                    return {'protocolVersion': 1, 'agentCapabilities': {'sessionCapabilities': {'resume': {}}}}
                if method in ('session/new', 'session/resume'):
                    return session(model)
                if method == 'session/set_config_option':
                    return super().request(method, params, **kwargs)
                if method == 'session/prompt':
                    if kwargs.get('on_sent'):
                        kwargs['on_sent']()
                    self.notification('session/update', {'sessionId': 'provider-session', 'update': {
                        'sessionUpdate': 'config_option_update', 'configOptions': [
                            {'id': 'model', 'currentValue': 'component-a' if drift else model},
                            {'id': 'thought_level', 'currentValue': 'low' if fixed else 'high'}]}})
                    self.notification('session/update', {'sessionId': 'provider-session', 'update': {
                        'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': response}}})
                    return {'stopReason': 'end_turn'}
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'inputs.json').write_text('[]')
            with patch.object(acp_execution, 'ACPClient', FakeClient), \
                    patch('bridge.ensure_account_route'), patch('bridge.prepare_task_agent', return_value='mock-agent'), \
                    patch('bridge.cli_environment', return_value={}), \
                    patch('bridge.check_output', return_value=[]):
                report = acp_execution.run(root, profile, 'test', root)
            evidence = json.loads((root / 'acp-model-evidence.json').read_text())
        return report, evidence, calls

    def test_fixed_resume_reports_contract_without_thought_evidence(self):
        report, evidence, calls = self.execute(continuation=True)
        self.assertEqual(report['status'], 'completed_unreviewed')
        self.assertEqual(report['model_execution'], EXECUTION)
        self.assertIn('fixed', report['effort_verification'])
        self.assertIn('no independent backend trace', report['effort_verification'])
        self.assertNotIn('thought_level', evidence['config_options'])
        self.assertTrue(report['remote_terminal_proven'])
        self.assertEqual(report['acp_stop_reason'], 'end_turn')
        self.assertFalse(report['cancel_protocol']['acknowledged'])
        self.assertIn(('session/resume', {'cwd': report['session_cwd'], 'mcpServers': [],
                                       'sessionId': 'provider-session'}), calls)

    def test_fixed_prompt_drift_rejects_component_model_identity(self):
        report, evidence, _ = self.execute(drift=True)
        self.assertEqual(report['status'], 'failed_or_unverified')
        self.assertFalse(report['model_matches'])
        self.assertIn('model_identity_mismatch', report['failure_reasons'])
        self.assertNotIn('thought_level', evidence['config_options'])

    def test_cli_error_banner_is_not_a_completed_delivery(self):
        report, _, _ = self.execute(response="\n⚠ Agent execution terminated due to error.\n")
        self.assertEqual(report['status'], 'failed_or_unverified')
        self.assertEqual(report['native_status'], 'ERROR')
        self.assertEqual(report['provider_error_category'], 'provider_execution_aborted')
        quoted, _, _ = self.execute(response='The documentation mentions: Agent execution terminated due to error.')
        self.assertEqual(quoted['status'], 'completed_unreviewed')

    def test_ordinary_report_preserves_thinking_confirmation(self):
        report, evidence, _ = self.execute(fixed=False)
        self.assertEqual(report['status'], 'completed_unreviewed')
        self.assertNotIn('model_execution', report)
        self.assertIn('thinking-level acknowledgement', report['effort_verification'])
        self.assertEqual(evidence['config_options']['thought_level']['currentValue'], 'high')


if __name__ == '__main__':
    unittest.main()
