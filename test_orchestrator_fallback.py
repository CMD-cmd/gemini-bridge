"""No-provider regressions for fixed-mode fallback and cancellation."""
import copy
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import acp_execution
import jobs
import model_selection
from run_control import RunControlRegistry
import task_mode

SOURCE=Path(__file__).resolve().parent
MODEL='gemini-pro-orchestrator'
SID='11111111-1111-1111-1111-111111111111'


def contract():
    return copy.deepcopy(json.loads((SOURCE/'routing/policy.example.json').read_text())['external_models'][MODEL+'/fixed']['model_execution'])


class FallbackTests(unittest.TestCase):
    def test_budget_rejection_rebinds_before_authorizing_ordinary_fallback(self):
        config=model_selection.load_config()
        for raises in (False,True):
            with self.subTest(reservation_raises=raises),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/'runs').mkdir();(root/'routing').mkdir()
                policy=json.loads((SOURCE/'routing/policy.example.json').read_text())
                policy['external_models']['ordinary-medium/medium']={'model':'ordinary-medium','reasoning_effort':'medium','usage_tier':'low'}
                (root/'routing/policy.json').write_text(json.dumps(policy))
                authorized=[]
                class Modes:
                    def authorize(self,args,profile,**kwargs):
                        if kwargs.get('scope_only'):
                            return {'allowed_model_tiers':['low','high'],'capability_routing_authorized':True,'workspace':{'commands':['python3']}}
                        _,specialist=task_mode.model_usage(profile);authorized.append(copy.deepcopy(profile))
                        return {'decision':{'specialist_exception':specialist}}
                fixed={'model':MODEL,'effort':'fixed','timeout_seconds':1200}
                ordinary={'model':'ordinary-medium','effort':'medium','timeout_seconds':180}
                owner=jobs.Jobs.__new__(jobs.Jobs);owner.queue=queue.Queue();owner.lock=threading.RLock();owner.cancels={};owner.modes=Modes();owner.specialist_budget=Mock()
                if raises:owner.specialist_budget.reserve.side_effect=OSError('synthetic ledger failure')
                else:owner.specialist_budget.reserve.return_value={'allowed':False,'reason':'ledger changed after selection'}
                with patch.object(jobs,'ROOT',root),patch.object(task_mode,'ROUTING',root/'routing'),patch.object(jobs,'load_profile',return_value=ordinary.copy()),patch.object(model_selection,'load_config',return_value=config),patch.object(model_selection,'select',side_effect=[(fixed,{}),(ordinary,{'specialist':False})]) as select,patch('workspace.prompt_description',return_value='synthetic scope'),patch.object(jobs,'run_job') as provider:
                    state=owner._submit({'prompt':'synthetic local test','kind':'prototype','dispatch_mode':'auto','thread_id':'synthetic','task_id':'synthetic','transport':'headless','workspace':{}})
                saved=json.loads((root/'runs'/state['job_id']/'requested-profile.json').read_text())
                self.assertEqual((state['status'],state['model'],state['effort']),('queued','ordinary-medium','medium'))
                self.assertFalse(state['specialist']);self.assertNotIn('model_execution',saved)
                self.assertTrue(authorized[0].get('model_execution'))
                self.assertTrue(all('model_execution' not in p for p in authorized[1:]))
                self.assertIn('specialist_reservation_fallback',state['model_selection'])
                self.assertEqual(select.call_args_list[1].kwargs['allowed_model_tiers'],['low'])
                self.assertEqual(owner.queue.get_nowait(),state['job_id']);provider.assert_not_called()


class CancellationTests(unittest.TestCase):
    def execute(self,ack):
        cancel=threading.Event();released=threading.Event();calls=[];prompt_thread=[];execution=contract()
        initial={'sessionId':SID,'models':{'availableModels':[{'modelId':MODEL}]},'configOptions':[{'id':'model','currentValue':'old','options':[{'value':MODEL}]}]}
        class FakeClient:
            pid=123
            def __init__(self,*args,**kwargs):self.options=copy.deepcopy(initial['configOptions'])
            def start(self):pass
            def drain_notifications(self,**kwargs):pass
            def close(self):
                released.set()
                if prompt_thread:prompt_thread[0].join(timeout=1)
            def notify(self,method,params):
                calls.append((method,copy.deepcopy(params)))
                if method=='session/cancel' and ack:released.set()
            def request(self,method,params,**kwargs):
                calls.append((method,copy.deepcopy(params)))
                if method=='initialize':return {'protocolVersion':1}
                if method=='session/new':return copy.deepcopy(initial)
                if method=='session/set_config_option':
                    for option in self.options:
                        if option['id']==params['configId']:option['currentValue']=params['value']
                    return {'configOptions':copy.deepcopy(self.options)}
                if method=='session/prompt':
                    prompt_thread.append(threading.current_thread())
                    if kwargs.get('on_sent'):kwargs['on_sent']()
                    cancel.set();released.wait(2)
                    return {'stopReason':'cancelled'}
                return {}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'inputs.json').write_text('[]')
            profile={'model':MODEL,'effort':'fixed','model_execution':execution,'timeout_seconds':10}
            with patch.object(acp_execution,'ACPClient',FakeClient),patch.object(acp_execution,'CANCEL_ACK_WAIT_SECONDS',.05),patch('run_control.registry',RunControlRegistry()),patch('bridge.ensure_account_route'),patch('bridge.prepare_task_agent',return_value='mock-agent'),patch('bridge.cli_environment',return_value={}):
                report=acp_execution.run(root,profile,'synthetic local test',root,cancel=cancel)
        self.assertIn(('session/cancel',{'sessionId':SID}),calls)
        self.assertEqual(sum(method=='session/prompt' for method,_ in calls),1)
        self.assertFalse(prompt_thread[0].is_alive())
        self.assertEqual(report['status'],'cancelled');self.assertTrue(report['provider_started']);self.assertTrue(report['model_matches'])
        self.assertTrue(report['cancel_protocol']['requested']);self.assertEqual(report['model_execution'],execution)
        return report
    def test_cancelled_response_is_acknowledged(self):
        report=self.execute(True)
        self.assertTrue(report['cancel_protocol']['acknowledged']);self.assertTrue(report['remote_terminal_proven'])
        self.assertEqual(report['acp_stop_reason'],'cancelled')
    def test_local_cancel_without_response_remains_unproven(self):
        report=self.execute(False)
        self.assertFalse(report['cancel_protocol']['acknowledged']);self.assertFalse(report['remote_terminal_proven'])
        self.assertIsNone(report['acp_stop_reason']);self.assertIn('provider_prompt_terminal_unproven',report['failure_reasons'])


if __name__=='__main__':unittest.main()
