"""Native declarations must not collide after CLI alias resolution."""
import json
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import bridge
import native_child_profiles
import workspace
import workspace_guard
import jobs


class ToolDeclarationTests(unittest.TestCase):
    def assert_reader_is_unambiguous(self,tools):
        resolved=['read_file' if name=='view_file' else name for name in tools]
        self.assertEqual(resolved.count('read_file'),1)
        self.assertEqual(len(resolved),len(set(resolved)))

    def test_ordinary_composite_and_child_enabled_parent_declarations(self):
        for record in ({},{'model_execution':{'mode':'orchestrated'}},
                       {'native_subagents':{'enabled':True}}):
            with self.subTest(record=record):
                self.assert_reader_is_unambiguous(workspace.declared_tools(record))
        self.assertIn('read_file',workspace_guard.READ_TOOLS)
        self.assertIn('view_file',workspace_guard.READ_TOOLS)

    def test_every_native_child_role_has_one_reader_and_no_added_commands(self):
        profiles=native_child_profiles.build_profiles('synthetic',list(native_child_profiles.ROLES))
        for child in profiles['profiles'].values():
            self.assert_reader_is_unambiguous(child['tools'])
            self.assertNotIn('run_command',child['tools'])
            if child['role']!='builder':self.assertNotIn('write_to_file',child['tools'])

    def test_generated_ordinary_agent_and_material_channel_remain_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();source=root/'source';source.mkdir()
            job=root/'job';job.mkdir();(job/'inputs').mkdir()
            record=workspace.prepare(job,workspace.normalize_spec({'read_roots':[str(source)]}),'ordinary-high',30,effort='high')
            workspace.configure_agent(job,record)
            agent=job/'.agents/agents/bridge-workspace/agent.md'
            line=next(x for x in agent.read_text().splitlines() if x.startswith('tools:'))
            self.assert_reader_is_unambiguous(json.loads(line.removeprefix('tools:')))
            visual=bridge.prepare_task_agent(job,False,model='ordinary-high')
            text=(job/'.agents/agents'/visual/'agent.md').read_text()
            self.assertIn('tools: [view_file]',text)

    def test_material_reader_alias_requires_selected_unchanged_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            job=Path(directory).resolve();image=job/'image.jpg';image.write_bytes(b'synthetic image')
            (job/'inputs.json').write_text(json.dumps([{'kind':'image','staged_path':str(image),'sha256':hashlib.sha256(image.read_bytes()).hexdigest()}]))
            def step(name,path,key=None):
                return {'tool_name':name,'tool_info':{'parameters':{key or ('Path' if name=='read_file' else 'AbsolutePath'):path}}}
            for name in ('view_file','read_file'):
                self.assertTrue(bridge.material_read_audit(job,[step(name,str(image))])['verified'])
            for value in [step('read_file',str(job/'outside.jpg')),step('read_file','image.jpg'),
                          step('read_file',str(image),'AbsolutePath'),step('read_file',[])]:
                self.assertFalse(bridge.material_read_audit(job,[value])['verified'])
            image.write_bytes(b'changed')
            self.assertFalse(bridge.material_read_audit(job,[step('read_file',str(image))])['verified'])

    def test_image_guard_reader_alias_keeps_selected_paths_and_fields(self):
        spec=importlib.util.spec_from_file_location('image_guard_alias',Path(__file__).parent/'script/image_tool_guard.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            job=Path(directory).resolve();image=job/'selected.jpg'
            (job/'image-permissions.json').write_text(json.dumps({'model':'synthetic','image_paths':[str(image)]}))
            conversation='11111111-1111-1111-1111-111111111111'
            payload={'conversationId':conversation,'modelName':'synthetic','workspacePaths':[str(job)],
                     'artifactDirectoryPath':str(Path.home()/'.gemini/antigravity-cli/brain'/conversation)}
            for name,key in [('view_file','AbsolutePath'),('read_file','Path')]:
                payload['toolCall']={'name':name,'args':{key:str(image)}}
                self.assertEqual(module.gate(job,payload),{})
            for args in ({'Path':str(job/'other.jpg')},{'AbsolutePath':str(image)}):
                payload['toolCall']={'name':'read_file','args':args}
                with self.assertRaises(ValueError):module.gate(job,payload)

    def test_local_revalidation_preserves_original_reply_review_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);job_id='20261114T000000Z-12345678';job=root/'runs'/job_id;job.mkdir(parents=True)
            image=job/'image.jpg';image.write_bytes(b'image')
            (job/'inputs.json').write_text(json.dumps([{'kind':'image','staged_path':str(image),'sha256':hashlib.sha256(image.read_bytes()).hexdigest()}]))
            state={'job_id':job_id,'kind':'visual','status':'failed_or_unverified','review':'partially_accepted'}
            report={'response':'Useful original result','failure_reasons':['unexpected_tool_use_in_task'],
                    'exit_code':0,'native_status':'SUCCESS','model_matches':True,'selected_agent':'bridge-visual',
                    'observed_agent':'bridge-visual','tool_calls':['read_file'],'parse_errors':0}
            (job/'state.json').write_text(json.dumps(state));(job/'report.json').write_text(json.dumps(report))
            event={'event':'step_update','step_update':{'step_type':'tool','tool_name':'read_file','tool_info':{'parameters':{'Path':str(image)}}}}
            (job/'events.jsonl').write_text(json.dumps(event)+'\n')
            owner=jobs.Jobs.__new__(jobs.Jobs);owner.lock=threading.RLock()
            with patch.object(jobs,'ROOT',root),patch.object(jobs,'run_job') as provider:
                invalid=dict(report,failure_reasons=['unexpected_tool_use_in_task','model_identity_unverified'])
                (job/'report.json').write_text(json.dumps(invalid))
                with self.assertRaises(ValueError):owner.revalidate_material(job_id)
                self.assertFalse((job/'compatibility-revalidation-before.json').exists())
                (job/'report.json').write_text(json.dumps(report))
                result=owner.revalidate_material(job_id)
                self.assertEqual(result['status'],'completed_unreviewed')
                self.assertEqual(result['review'],'partially_accepted')
                before=json.loads((job/'compatibility-revalidation-before.json').read_text())
                self.assertEqual(before['report'],report)
                updated=(job/'report.json').read_bytes()
                owner.revalidate_material(job_id)
                self.assertEqual((job/'report.json').read_bytes(),updated)
                self.assertEqual(json.loads(updated)['response'],report['response'])
                provider.assert_not_called()


if __name__=='__main__':unittest.main()
