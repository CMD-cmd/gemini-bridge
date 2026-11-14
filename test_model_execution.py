import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import model_execution as me
import model_selection as ms
import task_mode


class ModelExecutionTests(unittest.TestCase):
    def setUp(self):
        self.policy=json.loads((Path(__file__).parent/'routing/policy.example.json').read_text())
        self.record=self.policy['external_models']['gemini-pro-orchestrator/fixed']
        self.profile=me.bind({'model':self.record['model'],'effort':'fixed'},self.record)

    def test_fixed_requires_exact_registered_contract(self):
        for value in ({'model':'unknown','effort':'fixed'},
                      {**self.profile,'effort':'high'}):
            with self.assertRaises(ValueError):me.validate(value)
        with self.assertRaises(ValueError):
            me.bind({'model':'other','effort':'fixed'},self.record)
        me.validate({'model':'single-high','effort':'high'})

    def test_tool_identity_is_exact_and_no_generic_gemini_bypass(self):
        self.assertTrue(me.tool_identity_allowed(self.profile,self.profile['model']))
        self.assertFalse(me.tool_identity_allowed(self.profile,'gemini-4-carbon-high'))
        self.assertFalse(me.tool_identity_allowed(self.profile,'some-other-model'))
        self.assertTrue(me.tool_identity_allowed({'model':'single'},'single'))
        self.assertFalse(me.tool_identity_allowed({'model':'single'},self.profile['model']))

    def test_policy_binding_copies_and_drops_stale_metadata(self):
        bound=me.bind(self.profile,self.record)
        bound['model_execution']['label']='changed'
        self.assertNotEqual(bound['model_execution']['label'],self.record['model_execution']['label'])
        single=me.bind({**self.profile,'model':'single','effort':'medium'},None)
        self.assertNotIn('model_execution',single)

    def test_registered_fixed_wait_does_not_use_high_effort(self):
        cfg=ms.load_config()
        wait=ms.waiting_for('high','fixed',config=cfg)
        self.assertEqual((wait['timeout_seconds'],wait['hard_timeout_seconds']),(1200,1800))
        self.assertEqual(ms.waiting_for('high','high',config=cfg)['timeout_seconds'],1200)
        cfg['waiting']['high'].pop('effort_overrides')
        with self.assertRaisesRegex(ValueError,'own waiting'):ms.waiting_for('high','fixed',config=cfg)

    def test_model_usage_rejects_changed_execution_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'policy.json';p.write_text(json.dumps(self.policy))
            with patch.object(task_mode,'ROUTING',Path(directory)):
                self.assertEqual(task_mode.model_usage(self.profile),('high',True))
                altered=copy.deepcopy(self.profile);altered['model_execution']['tool_model_names'].append('unapproved')
                with self.assertRaisesRegex(ValueError,'current exact policy'):task_mode.model_usage(altered)

    def test_composite_runtime_qualification_is_scoped_until_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'resolve.py').write_text(
                'def read_parent(*args, **kwargs): return "parent", "high", {}\n'
                'def decide_external(*args, **kwargs): return {"enabled": True}\n')
            for qualification,allowed in (({},False),({'status':'pending'},False),
                    ({'status':'trial','thread_id':'other'},False),
                    ({'status':'trial','thread_id':'current'},True),
                    ({'status':'qualified'},True)):
                with self.subTest(qualification=qualification):
                    policy=copy.deepcopy(self.policy)
                    policy['external_models']['gemini-pro-orchestrator/fixed']['runtime_qualification']=qualification
                    (root/'policy.json').write_text(json.dumps(policy))
                    with patch.object(task_mode,'ROUTING',root):
                        result=task_mode.runtime_decision('current',self.profile)
                    self.assertEqual(result['enabled'],allowed)

    def test_orchestrator_auto_preference_needs_implementation_workspace(self):
        import hashlib
        cfg=ms.load_config();policy=self.policy
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'policy.json';path.write_text(json.dumps(policy))
            sha=hashlib.sha256(path.read_bytes()).hexdigest()
            catalog='\n'.join(r['model']+' label' for r in policy['external_models'].values())
            with patch.object(ms,'load_config',return_value=cfg),patch.object(task_mode,'ROUTING',Path(directory)),patch.object(ms,'budget_status',return_value={'allowed':True}),patch.object(ms,'get_model_catalog',return_value=catalog),patch.object(task_mode,'runtime_decision',return_value={'enabled':True,'policy_sha256':sha}):
                ordinary,_=ms.select('thread','prototype',1,allowed_model_tiers=['low','high'])
                self.assertNotEqual(ordinary['effort'],'fixed')
                picked,_=ms.select('thread','prototype',1,allowed_model_tiers=['low','high'],workspace={'commands':['python3']})
                self.assertEqual((picked['model'],picked['effort']),('gemini-pro-orchestrator','fixed'))
                children,_=ms.select('thread','prototype',1,allowed_model_tiers=['low','high'],workspace={'commands':['python3'],'subagents':{'max_children':1}})
                self.assertNotEqual(children['effort'],'fixed')


if __name__=='__main__':unittest.main()
