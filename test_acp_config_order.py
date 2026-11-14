"""Configuration receipts and notifications are ordered without provider calls."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from acp_client import ACPClient
import acp_execution as execution
from run_control import RunControlRegistry
from test_orchestrator_transport import ConfigClient, session

SID='11111111-1111-1111-1111-111111111111'


class ReceiptTests(unittest.TestCase):
    def test_receipt_shares_notification_wire_order_with_delayed_worker(self):
        script='''import json,sys
for line in sys.stdin:
 r=json.loads(line)
 if r['method']=='config':
  print(json.dumps({'jsonrpc':'2.0','method':'setting','params':{'state':'old'}}),flush=True)
  print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':'ack'}),flush=True)
  print(json.dumps({'jsonrpc':'2.0','method':'setting','params':{'state':'new'}}),flush=True)
 else: print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':True}),flush=True)
'''
        seen=[]
        def notification(method,params):
            time.sleep(.01);seen.append(params['state'])
        with tempfile.TemporaryDirectory() as directory:
            client=ACPClient([sys.executable,'-u','-c',script],stderr_path=Path(directory)/'stderr',on_notification=notification)
            try:
                client.start();client.request('config',{},on_response=lambda value:seen.append(value))
                client.request('barrier',{})
                client.drain_notifications(timeout=2)
                self.assertEqual(seen,['old','ack','new'])
            finally:client.close()

    def test_ordinary_empty_bootstrap_accepts_only_provider_normalized_identity(self):
        for model,base,effort in [('gemini-4-carbon-high','gemini-4-carbon','high'),
                                  ('gemini-4-hydrogen-xhigh','gemini-4-hydrogen','xhigh')]:
            with self.subTest(model=model):
                class Client:
                    def __init__(self):self.calls=[]
                    def request(self,method,params,**kwargs):
                        self.calls.append((params['configId'],params['value']))
                        return {'configOptions':[{'id':'model','currentValue':base,'options':[{'value':base}]},
                            {'id':'thought_level','currentValue':effort}]}
                client=Client()
                chosen=execution.select_model(client,{'sessionId':SID,'models':None,'configOptions':None},model,effort)
                self.assertTrue(chosen['matches']);self.assertEqual(chosen['base_model'],base)
                self.assertEqual(client.calls,[('model',model),('thought_level',effort)])
                class WrongClient(Client):
                    def request(self,*args,**kwargs):
                        return {'configOptions':[{'id':'model','currentValue':'wrong','options':[{'value':base}]}]}
                with self.assertRaisesRegex(ValueError,'not ready'):
                    execution.select_model(WrongClient(),{'sessionId':SID},model,effort)


class StartupBindingTests(unittest.TestCase):
    def execute(self,drift=False,interleave=False):
        model='ordinary-high';calls=[];clients=[]
        class Client(ConfigClient):
            pid=123
            def __init__(self,*args,**kwargs):
                super().__init__(session(model));self.callback=kwargs['on_notification'];self.pending=[];self.drains=0;clients.append(self)
            def start(self):pass
            def close(self):pass
            def drain_notifications(self,**kwargs):
                self.drains+=1
                while self.pending:self.pending.pop(0)()
                if drift and self.drains==1:self.change('wrong-model')
            def change(self,value):
                self.callback('session/update',{'sessionId':SID,'update':{'sessionUpdate':'config_option_update',
                    'configOptions':[{'id':'model','currentValue':value}]}})
            def request(self,method,params,**kwargs):
                calls.append(method)
                if method=='initialize':return {'protocolVersion':1}
                if method=='session/new':
                    value=session(model);value['sessionId']=SID;return value
                if method=='session/set_config_option':
                    value=super().request(method,params,**kwargs)
                    if params['configId']=='model':
                        interim=copy.deepcopy(value);interim['configOptions'][1]['currentValue']='low'
                        self.pending.append(lambda:self.callback('session/update',{'sessionId':SID,
                            'update':{'sessionUpdate':'config_option_update',**interim}}))
                    receipt=kwargs.get('on_response')
                    if receipt:self.pending.append(lambda value=copy.deepcopy(value):receipt(value))
                    return value
                if method=='session/prompt':
                    if kwargs.get('on_sent'):kwargs['on_sent']()
                    self.callback('session/update',{'sessionId':SID,'update':{'sessionUpdate':'agent_message_chunk',
                        'content':{'type':'text','text':'synthetic result'}}})
                    return {'stopReason':'end_turn'}
                return {}
        original=execution.select_model
        def selected(*args,**kwargs):
            value=original(*args,**kwargs)
            if interleave:
                class InterleavingDict(dict):
                    fired=False
                    def update(self,*a,**kw):
                        super().update(*a,**kw)
                        if not self.fired:
                            self.fired=True;clients[0].change('wrong-model')
                value['config_options']=InterleavingDict(value['config_options'])
            return value
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'inputs.json').write_text('[]')
            with patch.object(execution,'ACPClient',Client),patch.object(execution,'select_model',selected),\
                    patch('bridge.ensure_account_route'),patch('bridge.prepare_task_agent',return_value='synthetic'),\
                    patch('bridge.cli_environment',return_value={}),patch('run_control.registry',RunControlRegistry()):
                report=execution.run(root,{'model':model,'effort':'high','timeout_seconds':3},'synthetic',root)
        return report,calls

    def test_final_receipt_supersedes_legitimate_intermediate_notification(self):
        report,calls=self.execute()
        self.assertEqual(report['status'],'completed_unreviewed');self.assertTrue(report['model_matches'])
        self.assertEqual(calls.count('session/prompt'),1)

    def test_post_receipt_drift_stops_before_prompt(self):
        report,calls=self.execute(drift=True)
        self.assertEqual(report['status'],'failed_or_unverified');self.assertFalse(report['model_matches'])
        self.assertEqual(calls.count('session/prompt'),0)

    def test_merge_activation_interleaving_cannot_hide_drift(self):
        report,calls=self.execute(interleave=True)
        self.assertFalse(report['model_matches']);self.assertEqual(calls.count('session/prompt'),0)


if __name__=='__main__':unittest.main()
