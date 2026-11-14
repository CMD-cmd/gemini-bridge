"""Approved child edges, out-of-order hooks and capacity; no model calls."""
import json
from pathlib import Path
import tempfile
import unittest
import uuid

import native_children as native
import workspace_guard as guard


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.job=Path(self.temp.name).resolve()
        self.root,self.child,self.other=[str(uuid.uuid4()) for _ in range(3)]
        self.policy={'job_dir':str(self.job),'events_path':str(self.job/'events.jsonl'),
            'model':'gemini-4-carbon-high','effort':'high','audit_path':str(self.job/'audit.jsonl'),
            'native_subagents':{'enabled':True,'type_name':'TaskReader','max_children':1,
                'capacity_mode':'concurrent','model':'gemini-4-carbon-high','effort':'high'}}
        self.state={'conversation_id':self.root}

    def payload(self,cid=None,name='',args=None,step=4):
        cid=cid or self.root
        return {'conversationId':cid,'modelName':self.policy['model'],'workspacePaths':[str(self.job)],
            'artifactDirectoryPath':str(Path.home()/'.gemini/antigravity-cli/brain'/cid),
            'stepIdx':step,'toolCall':{'name':name,'args':args or {}}}

    def approve(self,step=4):
        args={'Prompt':'Read scoped synthetic material','TypeName':'TaskReader','Role':'reader-'+str(step)}
        return native.approve_spawn(self.policy,self.payload(name='invoke_subagent',args=args,step=step),self.state)

    def edge(self,child=None,step=4):
        value={'event':'step_update','step_update':{'conversation_id':self.root,'step_index':step,
            'subagent_info':{'subagents':[{'conversation_id':child or self.child,'type_name':'TaskReader',
                'role':'reader-'+str(step)}]}}}
        (self.job/'events.jsonl').write_text(json.dumps(value)+'\n')

    def stop(self):
        payload=self.payload(self.child,step=None)
        payload.update(terminationReason='NO_TOOL_CALL',fullyIdle=True,error='')
        return payload

    def save(self):
        (self.job/'workspace-guard-state.json').write_text(json.dumps(self.state))

    def load(self):
        self.state=json.loads((self.job/'workspace-guard-state.json').read_text())

    def test_stop_without_first_tool_releases_only_proven_slot(self):
        self.approve();self.edge()
        native.observe_lifecycle(self.policy,self.stop(),self.state,'Stop')
        summary=native.summarize(self.policy,self.state)
        self.assertEqual((summary['identified'],summary['stop_observed'],summary['capacity_used']),(1,1,0))
        self.approve(5);self.assertEqual(native.summarize(self.policy,self.state)['capacity_used'],1)

    def test_stop_before_edge_is_replayed_before_next_spawn(self):
        self.approve()
        self.assertIsNone(native.observe_lifecycle(self.policy,self.stop(),self.state,'Stop'))
        self.assertEqual(native.summarize(self.policy,self.state)['capacity_used'],1)
        self.edge();self.approve(5)
        summary=native.summarize(self.policy,self.state)
        self.assertEqual(summary['identified'],1);self.assertEqual(summary['stop_observed'],1)
        self.assertEqual(summary['capacity_used'],1)

    def test_delayed_observation_binds_for_root_messages_and_management(self):
        self.approve();native.observe_lifecycle(self.policy,self.stop(),self.state,'Stop');self.edge()
        message=self.payload(name='send_message',args={'Recipient':self.child,'Message':'Thanks'})
        self.assertEqual(native.authorize_message_control(self.policy,message,self.state)['decision'],'auto_approve')
        kill=self.payload(name='manage_subagents',args={'Action':'kill_all'})
        self.assertEqual(native.authorize_message_control(self.policy,kill,self.state)['overwrite']['ConversationIds'],[self.child])

    def test_unrelated_lifecycle_never_grants_identity_or_capacity(self):
        self.approve();self.edge(self.other)
        native.observe_lifecycle(self.policy,self.stop(),self.state,'Stop')
        summary=native.summarize(self.policy,self.state)
        self.assertEqual(summary['identified'],0);self.assertEqual(summary['capacity_used'],1)
        self.assertFalse((self.job/'native-child-stop.requested').exists())

    def test_hydrogen_inherits_exact_model_and_effort_from_lifecycle(self):
        self.policy.update(model='gemini-4-hydrogen-xhigh',effort='xhigh')
        self.policy['native_subagents'].update(model=self.policy['model'],effort='xhigh')
        self.approve();self.edge();native.observe_lifecycle(self.policy,self.stop(),self.state,'Stop')
        evidence=self.state['native_subagents']['children'][self.child]['model_evidence']
        self.assertEqual((evidence['modelName'],evidence['effort']),(self.policy['model'],'xhigh'))

    def test_known_child_lifecycle_drift_is_preserved_and_requests_stop(self):
        self.approve();self.edge();native.resolve_child(self.policy,self.payload(self.child),self.state);self.save()
        bad=self.stop();bad['modelName']='wrong-model'
        guard.lifecycle(self.policy,bad,'Stop');self.load()
        self.assertTrue((self.job/'native-child-stop.requested').exists())
        self.assertTrue(native.summarize(self.policy,self.state)['identity_failures'])
        rows=[json.loads(x) for x in (self.job/'audit.jsonl').read_text().splitlines()]
        self.assertEqual(rows[-1]['decision'],'deny')
        self.assertFalse(self.state['native_subagents']['children'][self.child].get('stop_observed'))

    def test_denied_hook_before_edge_becomes_failure_only_after_proof(self):
        self.approve();self.save();bad=self.stop();bad['modelName']='wrong-model'
        guard.lifecycle(self.policy,bad,'Stop');self.load()
        self.assertFalse((self.job/'native-child-stop.requested').exists())
        self.assertEqual(native.summarize(self.policy,self.state)['identity_failures'],[])
        self.edge();summary=native.summarize(self.policy,self.state)
        self.assertTrue(summary['identity_failures']);self.assertTrue((self.job/'native-child-stop.requested').exists())
        self.assertEqual(summary['identified'],0)
        with self.assertRaisesRegex(ValueError,'identity failure'):self.approve(5)

    def test_unknown_denied_hook_cannot_stop_unrelated_task(self):
        self.approve();self.save();bad=self.stop();bad['modelName']='wrong-model'
        guard.lifecycle(self.policy,bad,'Stop');self.load();self.edge(self.other)
        self.assertEqual(native.summarize(self.policy,self.state)['identity_failures'],[])
        self.assertFalse((self.job/'native-child-stop.requested').exists())

    def test_resuming_idle_child_reserves_capacity_until_new_turn_stop(self):
        self.approve();self.edge();native.observe_lifecycle(self.policy,self.stop(),self.state,'Stop')
        message=self.payload(name='send_message',args={'Recipient':self.child,'Message':'Continue the scoped work'})
        native.authorize_message_control(self.policy,message,self.state)
        self.assertEqual(native.summarize(self.policy,self.state)['capacity_used'],1)
        # Duplicate old Stop is not proof that the newly requested turn ended.
        native.observe_lifecycle(self.policy,self.stop(),self.state,'Stop')
        self.assertEqual(native.summarize(self.policy,self.state)['capacity_used'],1)
        native.observe_lifecycle(self.policy,self.payload(self.child),self.state,'PreInvocation')
        self.assertFalse(self.state['native_subagents']['children'][self.child]['resume_requested'])
        native.observe_lifecycle(self.policy,self.stop(),self.state,'Stop')
        self.assertEqual(native.summarize(self.policy,self.state)['capacity_used'],0)

    def test_idle_child_cannot_resume_while_another_slot_is_occupied(self):
        self.approve();self.edge();native.observe_lifecycle(self.policy,self.stop(),self.state,'Stop');self.approve(5)
        message=self.payload(name='send_message',args={'Recipient':self.child,'Message':'Continue'})
        with self.assertRaisesRegex(ValueError,'quota exceeded'):native.authorize_message_control(self.policy,message,self.state)
        self.assertTrue(self.state['native_subagents']['children'][self.child]['stop_observed'])

    def test_legacy_unbound_observations_remain_readable_without_new_authority(self):
        self.state['native_subagents']={'approvals':{'4':{'reserved':1,'specs':[{'type_name':'TaskReader'}]}},
            'children':{},'unbound_observations':[{'conversation_id':self.child,'event':'Stop'}]}
        self.edge()
        summary=native.summarize(self.policy,self.state)
        self.assertEqual(summary['identified'],0);self.assertEqual(summary['capacity_used'],1)


if __name__=='__main__':unittest.main()
