"""Fixed execution uses only the task hook or its host-owned stable lease."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import session_workspace as sessions
import workspace


class FixedHookTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();(self.root/'runs').mkdir()
        self.source=self.root/'source';self.source.mkdir()
        self.job=self.root/'runs/synthetic';self.job.mkdir();(self.job/'inputs').mkdir()
        policy=json.loads((Path(__file__).parent/'routing/policy.example.json').read_text())
        execution=policy['external_models']['gemini-pro-orchestrator/fixed']['model_execution']
        self.profile={'model':'gemini-pro-orchestrator','effort':'fixed','model_execution':execution}
        self.record=workspace.prepare(self.job,workspace.normalize_spec({'read_roots':[str(self.source)]}),
            self.profile['model'],30,effort='fixed',model_execution=execution)
        workspace.configure_agent(self.job,self.record)
        self.agent=self.job/'.agents/agents/bridge-workspace/agent.md'
        for name,value in [('submission.json',{'thread_id':'synthetic','dispatch_mode':'auto'}),
                           ('requested-profile.json',self.profile),('state.json',{'status':'running'})]:
            path=self.job/name;path.write_text(json.dumps(value));path.chmod(0o600)
        self.patcher=patch.object(sessions,'_ROOT',self.root);self.patcher.start();self.addCleanup(self.patcher.stop)

    def activate(self):
        lease=sessions.activate(self.root,self.job,self.record,self.profile)
        self.addCleanup(lambda:sessions.retire(lease['lease_file'],self.job,no_prompt_started=True))
        return lease

    def test_headless_has_exact_task_hook_with_ambient_customizations_off(self):
        text=self.agent.read_text()
        self.assertIn('inheritCustomizations: false\n',text)
        self.assertIn('hooks: '+json.dumps([str(self.job/'.agents/hooks.json')]),text)
        hooks=json.loads((self.job/'.agents/hooks.json').read_text())
        self.assertIn('PreToolUse',hooks['bridge-workspace'])

    def test_acp_replaces_only_task_hook_with_stable_lease_hook(self):
        lease=self.activate();stable=Path(lease['session_cwd'])/'.agents/hooks.json'
        for agent in [self.agent,Path(lease['session_cwd'])/'.agents/agents/bridge-workspace/agent.md']:
            lines=[x for x in agent.read_text().splitlines() if x.startswith('hooks:')]
            self.assertEqual(lines,['hooks: '+json.dumps([str(stable)])])
        self.assertIn('workspace_session_guard.py',stable.read_text())

    def test_untrusted_or_duplicate_hook_declaration_is_not_inherited(self):
        text=self.agent.read_text();original='hooks: '+json.dumps([str(self.job/'.agents/hooks.json')])
        for replacement in ('hooks: ["/untrusted/hooks.json"]',original+'\n'+original):
            with self.subTest(replacement=replacement):
                self.agent.write_text(text.replace(original,replacement))
                with self.assertRaisesRegex(ValueError,'Unexpected pre-existing'):self.activate()


if __name__=='__main__':unittest.main()
