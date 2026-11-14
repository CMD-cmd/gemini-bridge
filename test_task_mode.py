"""Isolated lifecycle tests; no external model or real session content."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock
import bridge
import jobs
import task_mode

THREAD = '00000000-0000-0000-0000-000000000001'
OTHER = '00000000-0000-0000-0000-000000000002'


class TaskModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'profile.json').write_text(json.dumps({'model':'synthetic-model','effort':'high'}))
        self.source = self.root / 'source.md'; self.source.write_text('Synthetic count 3')
        self.routing = self.root / 'routing'; self.routing.mkdir()
        self.policy = {'external_models': {'synthetic-model/'+e: {'usage_tier':'low'} for e in ('low','medium','high')}}
        (self.routing/'policy.json').write_text(json.dumps(self.policy))
        self.decision = {'enabled':True, 'allowed_kinds':list(task_mode.KINDS),
                         'evidence':{'thread_id':THREAD,'turn_id':'synthetic-active-turn','turn_state':'active'}}
        self.patches = [patch.object(task_mode,'ROOT',self.root), patch.object(jobs,'ROOT',self.root), patch.object(task_mode,'ROUTING',self.routing),
                        patch.object(task_mode,'runtime_decision',side_effect=lambda *a, **k:copy.deepcopy(self.decision))]
        for p in self.patches:p.start()
        self.modes = task_mode.TaskModes(self.root/'.runtime/task-modes')

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def enable(self, **kwargs):
        return self.modes.control(dict(action='enable',thread_id=THREAD,scope='Synthetic validation task',
                                       authorization='Explicit synthetic test opt-in',material_class='public_or_synthetic',
                                       allowed_files=[str(self.source)],**kwargs))['state']

    def submission(self, state, **kwargs):
        return dict(prompt='Extract the count',kind='extract',dispatch_mode='auto',model='synthetic-model',effort='high',
                    task_id=state['task_id'],thread_id=THREAD,**kwargs)

    def test_synthetic_diagnostic_submission_preserves_authorized_material_scope(self):
        mode=self.enable()
        payload=dict(self.submission(mode),kind='copy',files=[],diagnostic_proxy='http://127.0.0.1:45678')
        scoped=self.modes.authorize(payload,{'model':'synthetic-model','effort':'high'},scope_only=True)
        self.assertEqual(scoped['material_class'],'public_or_synthetic')
        with patch.object(jobs,'run_job',return_value={'status':'completed_unreviewed','response':'synthetic','provider_started':False}):
            manager=jobs.Jobs()
            state=manager.submit(payload)
            manager.queue.join()
        profile=json.loads((self.root/'runs'/state['job_id']/'requested-profile.json').read_text())
        self.assertEqual(profile['diagnostic_proxy'],'http://127.0.0.1:45678')

    def test_ultra_standing_both_tiers_no_expiry_and_legacy_scope_isolation(self):
        legacy = self.enable()
        closed = self.enable()
        self.modes.control(dict(action='complete', task_id=closed['task_id'], thread_id=THREAD))
        (self.root/'model-routing.json').write_text(json.dumps({'access':{
            'standing_high_tier':True, 'basis':'user_authorized_ultra',
            'authorization':'Synthetic Ultra standing approval', 'default_max_jobs':0, 'scope_ttl_seconds':None}}))
        mode = self.enable()
        self.assertEqual(mode['allowed_model_tiers'], ['high','low'])
        self.assertIsNone(mode['expires_at'])
        self.assertEqual(mode['max_jobs'], 0)
        self.policy['external_models']['synthetic-model/high']['usage_tier']='high';self.write_policy()
        for n in range(15):
            self.modes.authorize(self.submission(mode), {'model':'synthetic-model','effort':'high'}, reserve_job='ultra-'+str(n))
        self.assertTrue(self.modes.control(dict(action='status',task_id=mode['task_id'],thread_id=THREAD))['effective_enabled'])
        self.assertEqual(self.modes.read(legacy['task_id'])['allowed_model_tiers'], ['low'])
        with self.assertRaisesRegex(ValueError,'disabled'):
            self.modes.authorize(self.submission(self.modes.read(closed['task_id'])), {'model':'synthetic-model','effort':'high'})
        low = self.enable(allowed_model_tiers=['low'])
        self.assertEqual(low['max_jobs'], 0)
        with self.assertRaisesRegex(ValueError,'tier is outside'):
            self.modes.authorize(self.submission(low), {'model':'synthetic-model','effort':'high'})
        (self.root/'model-routing.json').unlink()
        with self.assertRaisesRegex(ValueError,'High tier'):
            self.modes.authorize(self.submission(mode), {'model':'synthetic-model','effort':'high'})

    def test_legacy_high_scope_cannot_gain_general_specialist_access(self):
        mode = self.enable(allowed_model_tiers=['low','high'],high_tier_authorization='$gemini-bridge')
        mode.pop('capability_routing_authorized',None);self.modes.write(mode)
        self.policy['external_models']['synthetic-model/high'].update(usage_tier='high',specialist_only=True);self.write_policy()
        (self.root/'model-routing.json').write_text(json.dumps({'access':{
            'standing_high_tier':True,'basis':'user_authorized_ultra','authorization':'Synthetic Ultra authorization',
            'default_max_jobs':0,'scope_ttl_seconds':None}}))
        with self.assertRaisesRegex(ValueError,'predates'):
            self.modes.authorize(self.submission(mode), {'model':'synthetic-model','effort':'high'})
        self.modes.authorize(dict(self.submission(mode),task_purpose='key_copy'), {'model':'synthetic-model','effort':'high'})
        fresh = self.enable()
        self.modes.authorize(self.submission(fresh), {'model':'synthetic-model','effort':'high'})

    def test_plain_argon_permission_and_unlimited_scope(self):
        for quote in ('本任务可以使用4argon', '允许调用 Gemini 4 Argon', '4 Argon 可以用'):
            self.assertTrue(task_mode.high_tier_quote_valid(quote))
        for quote in ('4argon是什么', '怎么调用4argon', '不要使用4argon', '4argon额度很多'):
            self.assertFalse(task_mode.high_tier_quote_valid(quote))
        high = self.enable(allowed_model_tiers=['low','high'], high_tier_authorization='本任务可以使用4argon')
        self.assertEqual(high['max_jobs'], 0)
        for index in range(12):
            self.modes.authorize(self.submission(high), {'model':'synthetic-model','effort':'high'}, reserve_job='job-'+str(index))
        status = self.modes.control(dict(action='status', task_id=high['task_id'], thread_id=THREAD))
        self.assertTrue(status['effective_enabled'])
        self.assertIsNone(status['remaining_jobs'])
        once = self.enable(allowed_model_tiers=['high'], high_tier_authorization='允许调用4argon', max_jobs=1)
        self.assertEqual(once['max_jobs'], 1)
        with self.assertRaises(ValueError):
            self.enable(max_jobs=0)

    def test_specialist_scope_and_context_forwarding(self):
        state = self.enable(allowed_purposes=['important_communication'])
        args = dict(self.submission(state), kind='copy', task_purpose='important_communication')
        with patch.object(task_mode, 'runtime_decision', return_value=self.decision) as runtime:
            checked = self.modes.authorize(args, {'model':'synthetic-model','effort':'medium'})
            self.assertTrue(checked['decision']['enabled'])
            self.assertEqual(runtime.call_args.kwargs, {'executing':False, 'task_purpose':'important_communication',
                                                       'kind':'copy', 'dispatch_mode':'auto'})
            self.assertEqual(self.modes.authorize(args, {}, scope_only=True)['allowed_files'], [str(self.source.resolve())])
        with self.assertRaisesRegex(ValueError, 'outside authorized scope'):
            self.modes.authorize(dict(args, task_purpose='tone_polish'), {})
        with self.assertRaisesRegex(ValueError, 'Invalid task purpose'):
            self.modes.authorize(dict(args, task_purpose='unknown'), {})
        with self.assertRaisesRegex(ValueError, 'dispatch_mode=auto'):
            self.modes.authorize({'task_purpose':'important_communication'}, {})

    def test_old_scope_does_not_gain_specialist_permission(self):
        state = self.enable()
        self.assertEqual(set(state['allowed_purposes']), set(task_mode.PURPOSES))
        state.pop('allowed_purposes'); self.modes.write(state)
        args = dict(self.submission(state), task_purpose='technical_second_opinion')
        with self.assertRaisesRegex(ValueError, 'outside authorized scope'):
            self.modes.authorize(args, {}, scope_only=True)
        with patch.object(task_mode, 'runtime_decision', return_value=self.decision) as runtime:
            self.modes.authorize(self.submission(state), {'model':'synthetic-model','effort':'high'})
            self.assertEqual(runtime.call_args.kwargs, {'executing':False})
        for purposes in ([], ['unknown'], 'general', None):
            with self.assertRaisesRegex(ValueError, 'Invalid allowed task purposes'):
                self.enable(allowed_purposes=purposes)

    def test_runtime_optional_context_legacy_and_required_account_route(self):
        self.patches[-1].stop()
        resolver = SimpleNamespace(read_parent=Mock(return_value=('parent','medium',{'turn_state':'active'})),
                                   decide_external=Mock(return_value=dict(self.decision)), RuntimeNotReady=RuntimeError)
        loader = Mock()
        spec = SimpleNamespace(loader=loader)
        routing = self.root / 'runtime-routing'; routing.mkdir()
        (routing/'policy.json').write_text('{}')
        with patch.object(task_mode, 'ROUTING', routing), patch.object(task_mode.importlib.util, 'spec_from_file_location', return_value=spec), \
             patch.object(task_mode.importlib.util, 'module_from_spec', return_value=resolver), \
             patch.object(task_mode, 'ensure_account_route') as account:
            profile = {'model':'synthetic-model','effort':'medium'}
            task_mode.runtime_decision(THREAD, profile)
            self.assertEqual(resolver.decide_external.call_args.kwargs, {})
            account.assert_not_called()
            resolver.decide_external.return_value = dict(self.decision, required_billing_route='existing_account_authentication_only')
            task_mode.runtime_decision(THREAD, profile, task_purpose='key_copy', kind='copy', dispatch_mode='auto')
            self.assertEqual(resolver.decide_external.call_args.kwargs,
                             {'task_purpose':'key_copy','kind':'copy','dispatch_mode':'auto'})
            account.assert_called_once()
            account.side_effect = ValueError('account route refused')
            with self.assertRaisesRegex(ValueError, 'account route refused'):
                task_mode.runtime_decision(THREAD, profile, task_purpose='key_copy', kind='copy', dispatch_mode='auto')

    def write_policy(self):
        (self.routing/'policy.json').write_text(json.dumps(self.policy))

    def test_model_tier_defaults_old_scope_and_high_quote(self):
        state = self.enable()
        self.assertEqual(state['allowed_model_tiers'], ['low'])
        state.pop('allowed_model_tiers'); self.modes.write(state)
        args = self.submission(state)
        self.assertEqual(self.modes.authorize(args, {}, scope_only=True)['allowed_model_tiers'], ['low'])
        self.modes.authorize(args, {'model':'synthetic-model','effort':'high'})
        self.policy['external_models']['synthetic-model/high']['usage_tier'] = 'high'; self.write_policy()
        with self.assertRaisesRegex(ValueError, 'tier is outside'):
            self.modes.authorize(args, {'model':'synthetic-model','effort':'high'})
        for quote in ('Use Gemini for this', 'Argon high please', '$gemini-bridge-other', '', None):
            with self.assertRaisesRegex(ValueError, 'explicit \$gemini-bridge'):
                self.enable(allowed_model_tiers=['low','high'], high_tier_authorization=quote)
        quote = 'Use [$gemini-bridge](skill://gemini-bridge) for this important message.'
        high = self.enable(allowed_model_tiers=['low','high'], high_tier_authorization=quote)
        self.assertEqual(high['high_tier_authorization'], quote)
        self.assertIn('coordinator', high['high_tier_authorization_basis'])
        self.modes.authorize(self.submission(high), {'model':'synthetic-model','effort':'high'})

    def test_high_manual_missing_policy_and_execution_recheck(self):
        profile = {'model':'synthetic-model','effort':'high'}
        self.assertIsNone(self.modes.authorize({'dispatch_mode':'manual'}, profile))
        self.policy['external_models']['synthetic-model/high'].pop('usage_tier'); self.write_policy()
        with self.assertRaisesRegex(ValueError, 'controlled dispatch_mode=auto'):
            self.modes.authorize({'dispatch_mode':'manual'}, profile)
        with self.assertRaisesRegex(ValueError, 'controlled dispatch_mode=auto'):
            self.modes.authorize({}, {'model':'unmapped','effort':'low'})
        self.policy['external_models']['synthetic-model/high']['usage_tier'] = 'low'; self.write_policy()
        state = self.enable()
        args = self.submission(state)
        self.modes.authorize(args, profile, reserve_job='reserved')
        self.policy['external_models']['synthetic-model/high']['usage_tier'] = 'high'; self.write_policy()
        with self.assertRaisesRegex(ValueError, 'tier is outside'):
            self.modes.authorize(args, profile, executing=True, executing_job='reserved')

    def test_exact_approved_low_tier_exception_needs_no_high_scope(self):
        profile = {'model':'synthetic-model','effort':'high'}
        key = 'synthetic-model/high'
        self.policy['external_models'][key].update(usage_tier='low', specialist_only=True)
        self.policy['external_specialist_exception'] = {'allowed_usage_tiers':{key:'low'}}
        self.write_policy()
        state = self.modes.control(dict(action='enable',thread_id=THREAD,scope='Approved new low tier',authorization='Allow Gemini for this task',material_class='public_or_synthetic',allowed_model_tiers=['low'],allowed_files=[str(self.source)]))['state']
        args = self.submission(state)
        self.modes.authorize(args, profile, reserve_job='low-exception')
        self.assertEqual(state['allowed_model_tiers'], ['low'])
        self.assertTrue(state['capability_routing_authorized'])
        self.policy['external_models'][key]['usage_tier'] = 'high'
        self.write_policy()
        with self.assertRaises(ValueError):
            self.modes.authorize(args, profile, executing=True, executing_job='low-exception')

    def test_specialist_cannot_be_relabelled_low_or_lose_high_quote(self):
        profile = {'model':'synthetic-model','effort':'high'}
        record = self.policy['external_models']['synthetic-model/high']
        record.update(usage_tier='high', specialist_only=True); self.write_policy()
        state = self.enable(allowed_model_tiers=['low','high'], high_tier_authorization='Use $gemini-bridge here')
        args = dict(self.submission(state), task_purpose='key_copy')
        self.modes.authorize(args, profile, reserve_job='reserved')
        record['usage_tier'] = 'low'; self.write_policy()
        with self.assertRaisesRegex(ValueError, 'retain high usage tier'):
            self.modes.authorize(args, profile, executing=True, executing_job='reserved')
        record['usage_tier'] = 'high'; self.write_policy()
        state = self.modes.read(state['task_id'])
        state['high_tier_authorization'] = 'Use Gemini'; self.modes.write(state)
        with self.assertRaisesRegex(ValueError, 'explicit \$gemini-bridge'):
            self.modes.authorize(args, profile, executing=True, executing_job='reserved')

    def test_cancel_at_pending_deadline_cannot_become_recoverable(self):
        with patch.object(jobs.threading.Thread,'start'):
            manager=jobs.Jobs()
        state=self.enable()
        first=manager.submit(self.submission(state))
        job_id=first['job_id'];event=manager.cancels[job_id]
        clock_calls=[0]
        def clock():
            clock_calls[0]+=1
            if clock_calls[0]>1:
                manager.cancel(job_id)
                return 100
            return 0
        with patch.object(task_mode,'runtime_decision',side_effect=task_mode.RuntimePending('writing')),patch.object(jobs.time,'monotonic',side_effect=clock):
            with self.assertRaises(jobs.PreflightInterrupted):
                manager._execution_gate(job_id,self.root/'runs'/job_id,event)
        actual=manager.state(job_id)
        self.assertEqual(actual['status'],'cancelled')
        self.assertFalse(actual.get('preflight_interrupted'))

    def test_default_off_other_chat_and_close(self):
        self.assertFalse(self.modes.control({'action':'status','thread_id':THREAD})['effective_enabled'])
        state=self.enable()
        with self.assertRaisesRegex(ValueError,'different chat'):
            self.modes.control({'action':'disable','thread_id':OTHER,'task_id':state['task_id']})
        self.modes.control({'action':'disable','thread_id':THREAD,'task_id':state['task_id']})
        with self.assertRaisesRegex(ValueError,'disabled'):
            self.modes.authorize(self.submission(state), {'model':'synthetic-model','effort':'high'})
        with self.assertRaisesRegex(ValueError,'cannot reset'):
            self.modes.control({'action':'enable','thread_id':THREAD,'task_id':state['task_id']})

    def test_limits_file_kind_expiration_and_cost(self):
        state=self.enable(allowed_kinds=['extract'],max_jobs=1)
        args=self.submission(state);profile={'model':'synthetic-model','effort':'high'}
        with self.assertRaisesRegex(ValueError,'outside authorized scope'):
            self.modes.authorize(dict(args,kind='prototype'),profile)
        with self.assertRaisesRegex(ValueError,'File is outside'):
            self.modes.authorize(dict(args,files=[str(self.root/'private.md')]),profile)
        self.decision.update(enabled=False,reason='cost gate refused')
        with self.assertRaisesRegex(ValueError,'cost gate refused'):self.modes.authorize(args,profile)
        self.decision['enabled']=True
        self.modes.authorize(args,profile,reserve_job='one')
        with self.assertRaisesRegex(ValueError,'budget exhausted'):self.modes.authorize(args,profile,reserve_job='two')
        self.modes.authorize(args,profile,executing=True,executing_job='one')
        state=self.modes.read(state['task_id']);state['expires_at']=0;self.modes.write(state)
        with self.assertRaisesRegex(ValueError,'expired'):self.modes.authorize(args,profile,executing=True)

    def test_parallel_reservations_are_bounded(self):
        state=self.enable(max_jobs=1);accepted=[];rejected=[]
        def reserve(i):
            try:self.modes.authorize(self.submission(state),{'model':'synthetic-model','effort':'high'},reserve_job=str(i));accepted.append(i)
            except ValueError:rejected.append(i)
        threads=[threading.Thread(target=reserve,args=(i,)) for i in range(8)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(len(accepted),1);self.assertEqual(len(rejected),7)

    def test_recheck_before_execution_and_no_retry(self):
        first_started=threading.Event();release=threading.Event();calls=[]
        def fake_run(profile_path,prompt_path,**kwargs):
            calls.append(kwargs['job_id']);first_started.set();release.wait(3)
            report={'job_id':kwargs['job_id'],'status':'completed_unreviewed','response':'3'}
            bridge.atomic_json(self.root/'runs'/kwargs['job_id']/'report.json',report)
            return report
        with patch.object(jobs,'run_job',fake_run):
            manager=jobs.Jobs();manager.modes=self.modes;state=self.enable()
            first=manager.submit(self.submission(state,request_id='synthetic-stable'))
            self.assertTrue(first_started.wait(2))
            again=manager.submit(self.submission(state,request_id='synthetic-stable'))
            self.assertEqual(first['job_id'],again['job_id'])
            with self.assertRaisesRegex(ValueError,'different task or submission'):
                manager.submit(dict(self.submission(state,request_id='synthetic-stable'),thread_id=OTHER))
            with self.assertRaisesRegex(ValueError,'different task or submission'):
                manager.submit(dict(self.submission(state,request_id='synthetic-stable'),prompt='Different request'))
            second=manager.submit(self.submission(state))
            self.decision.update(enabled=False,reason='runtime or cost changed')
            release.set();manager.queue.join()
            self.assertEqual(calls,[first['job_id']])
            self.assertEqual(manager.state(second['job_id'])['status'],'bridge_error')
            self.assertFalse(self.modes.read(state['task_id'])['enabled'])
            self.assertEqual(len(self.modes.read(state['task_id'])['job_ids']),2)

    def test_failure_closes_pending_siblings(self):
        started=threading.Event();release=threading.Event();calls=[]
        def fake_run(profile_path,prompt_path,**kwargs):
            calls.append(kwargs['job_id']);started.set();release.wait(3)
            report={'job_id':kwargs['job_id'],'status':'failed_or_unverified','response':''}
            bridge.atomic_json(self.root/'runs'/kwargs['job_id']/'report.json',report);return report
        with patch.object(jobs,'run_job',fake_run):
            manager=jobs.Jobs();manager.modes=self.modes;state=self.enable()
            first=manager.submit(self.submission(state));self.assertTrue(started.wait(2))
            second=manager.submit(self.submission(state));release.set();manager.queue.join()
            self.assertEqual(calls,[first['job_id']]);self.assertEqual(manager.state(second['job_id'])['status'],'cancelled')
            self.assertFalse(self.modes.read(state['task_id'])['enabled'])

    def test_preflight_wait_interruption_can_recover_same_request_without_extra_reservation(self):
        ready=threading.Event();calls=[]
        def decide(*args,**kwargs):
            if kwargs.get('executing') and not ready.is_set():
                raise task_mode.RuntimePending('Synthetic delayed context')
            return copy.deepcopy(self.decision)
        def run(profile_path,prompt_path,**kwargs):
            calls.append(kwargs['job_id'])
            report={'job_id':kwargs['job_id'],'status':'completed_unreviewed','response':'ready'}
            bridge.atomic_json(self.root/'runs'/kwargs['job_id']/'report.json',report)
            return report
        with patch.object(task_mode,'runtime_decision',side_effect=decide),patch.object(jobs,'IDENTITY_WAIT_SECONDS',0),patch.object(jobs,'run_job',side_effect=run):
            manager=jobs.Jobs();manager.modes=self.modes;mode=self.enable(max_jobs=1)
            args=self.submission(mode,request_id='preflight-stable')
            first=manager.submit(args);manager.queue.join()
            self.assertEqual(manager.state(first['job_id'])['status'],'interrupted')
            self.assertTrue(self.modes.read(mode['task_id'])['enabled'])
            self.assertEqual(calls,[])
            ready.set();again=manager.submit(args);manager.queue.join()
            self.assertEqual(again['job_id'],first['job_id'])
            self.assertEqual(calls,[first['job_id']])
            self.assertEqual(self.modes.read(mode['task_id'])['job_ids'],[first['job_id']])
            manager.submit(args)
            self.assertEqual(calls,[first['job_id']])

    def test_recovery_waits_for_old_worker_retirement(self):
        ready=threading.Event();retiring=threading.Event();release=threading.Event();calls=[]
        def decide(*args,**kwargs):
            if kwargs.get('executing') and not ready.is_set():raise task_mode.RuntimePending('Delayed context')
            return copy.deepcopy(self.decision)
        def run(profile_path,prompt_path,**kwargs):
            calls.append(kwargs['job_id'])
            report={'job_id':kwargs['job_id'],'status':'completed_unreviewed','response':'ready'}
            bridge.atomic_json(self.root/'runs'/kwargs['job_id']/'report.json',report)
            return report
        with patch.object(task_mode,'runtime_decision',side_effect=decide),patch.object(jobs,'IDENTITY_WAIT_SECONDS',0),patch.object(jobs,'run_job',side_effect=run):
            manager=jobs.Jobs();manager.modes=self.modes;mode=self.enable(max_jobs=1)
            original=manager._retire_job
            def retire(job_id,event):
                if manager.state(job_id).get('preflight_interrupted'):
                    retiring.set();release.wait(3)
                original(job_id,event)
            manager._retire_job=retire
            args=self.submission(mode,request_id='retirement-stable')
            first=manager.submit(args)
            try:
                self.assertTrue(retiring.wait(2))
                old_event=manager.cancels[first['job_id']]
                ready.set();pending=manager.submit(args)
                self.assertTrue(pending['recovery_pending'])
                self.assertIs(manager.cancels[first['job_id']],old_event)
                self.assertEqual(calls,[])
            finally:release.set()
            manager.queue.join()
            manager.submit(args);manager.queue.join()
            self.assertEqual(calls,[first['job_id']])
            self.assertTrue(self.modes.read(mode['task_id'])['enabled'])

    def test_cancel_during_pending_check_stays_cancelled_not_recoverable(self):
        checking=threading.Event();release=threading.Event()
        def decide(*args,**kwargs):
            if kwargs.get('executing'):
                checking.set();release.wait(3);raise task_mode.RuntimePending('Delayed context')
            return copy.deepcopy(self.decision)
        with patch.object(task_mode,'runtime_decision',side_effect=decide),patch.object(jobs,'IDENTITY_WAIT_SECONDS',0),patch.object(jobs,'run_job') as run:
            manager=jobs.Jobs();manager.modes=self.modes;mode=self.enable(max_jobs=1)
            first=manager.submit(self.submission(mode))
            try:
                self.assertTrue(checking.wait(2))
                manager.cancel(first['job_id'])
            finally:release.set()
            manager.queue.join()
            state=manager.state(first['job_id'])
            self.assertEqual(state['status'],'cancelled')
            self.assertFalse(state.get('preflight_interrupted',False))
            run.assert_not_called()


if __name__ == '__main__':unittest.main(verbosity=2)
