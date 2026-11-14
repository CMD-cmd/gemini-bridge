import copy,hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import model_selection as ms

class SelectionTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  self.config=copy.deepcopy(ms.DEFAULT)
  self.records={}
  for model,family,effort,priority,score in [('future-fast-low','flash','low',100,30),('future-fast-medium','flash','medium',100,39),('future-fast-high','flash','high',100,40),('future-specialist-low','reasoning','low',100,None),('future-specialist-medium','reasoning','medium',100,None),('future-specialist-high','reasoning','high',100,53),('legacy-fast-high','flash','high',10,35)]:
   self.records[model+'/'+effort]={'model':model,'reasoning_effort':effort,'routing_family':family,'routing_priority':priority,'score':score,'allowed_kinds':['copy','english','extract','code-map','prototype']}
   self.records[model+'/'+effort]['usage_tier']='high' if family=='reasoning' else 'low'
   if family=='reasoning':self.records[model+'/'+effort].update(specialist_only=True,routing_role='scarce_specialist',automatic_eligible=False)
  self.write_policy()
  self.catalog='\n'.join(x['model']+'\tlabel' for x in self.records.values())
 def write_policy(self):
  path=self.root/'policy.json';path.write_text(json.dumps({'external_models':self.records}));self.sha=hashlib.sha256(path.read_bytes()).hexdigest()
 def tearDown(self):self.temp.cleanup()
 def run_select(self,kind='extract',size=1,requested=None,blocked=(),purpose='general',budget=True,tiers=('low','high')):
  def decision(thread,profile,**kw):return {'enabled':profile['model'] not in blocked,'policy_sha256':self.sha,'reason':'capability ceiling' if profile['model'] in blocked else 'approved'}
  with patch.object(ms,'load_config',return_value=self.config),patch.object(ms.task_mode,'ROUTING',self.root),patch.object(ms.task_mode,'runtime_decision',side_effect=decision),patch.object(ms,'get_model_catalog',return_value=self.catalog),patch.object(ms,'budget_status',return_value={'allowed':budget,'reason':'available' if budget else 'per_day'}):
   return ms.select('thread',kind,size,requested,task_purpose=purpose,task_id='task',allowed_model_tiers=tiers)
 def test_explicit_low_tier_exception_replaces_fast_model_without_high_authority(self):
  self.config=json.loads((ms.ROOT/'model-routing.json').read_text())
  model='replacement-fast-medium';key=model+'/medium'
  self.records[key]=copy.deepcopy(self.records['future-specialist-medium/medium'])
  self.records[key].update(model=model,routing_family='flash',usage_tier='low',routing_priority=200,score=None)
  self.write_policy();path=self.root/'policy.json';policy=json.loads(path.read_text());policy['external_specialist_exception']={'allowed_usage_tiers':{key:'low'}};path.write_text(json.dumps(policy));self.sha=hashlib.sha256(path.read_bytes()).hexdigest();self.catalog+='\n'+model+' label'
  result,_=self.run_select(kind='extract',tiers=('low',));self.assertEqual(result['model'],model)
  result,_=self.run_select(kind='extract',tiers=('low',),blocked=(model,));self.assertEqual(result['model'],'future-fast-medium')
  policy['external_specialist_exception']['allowed_usage_tiers'][key]='high';path.write_text(json.dumps(policy));self.sha=hashlib.sha256(path.read_bytes()).hexdigest()
  result,_=self.run_select(kind='extract',tiers=('low',));self.assertEqual(result['model'],'future-fast-medium')

 def test_xhigh_wait_budget_does_not_change_usage_tier_or_regular_low_cap(self):
  config=copy.deepcopy(ms.DEFAULT)
  config['waiting']['low']['effort_overrides']={'xhigh':{'max_seconds':1800,'default_seconds':900,'quiet_notice_seconds':180}}
  ms.validate_waiting(config['waiting'])
  chosen=ms.waiting_for('low','xhigh',config=config)
  self.assertEqual((chosen['usage_tier'],chosen['timeout_seconds'],chosen['hard_timeout_seconds']),('low',900,1800))
  self.assertEqual(ms.waiting_for('low','high',requested=1800,config=config)['hard_timeout_seconds'],600)
  config['waiting']['low']['effort_overrides']['xhigh']['max_seconds']=1801
  with self.assertRaises(ValueError):ms.validate_waiting(config['waiting'])

 def test_exact_fast_image_exception_selects_only_qualified_variant(self):
  self.config=json.loads((ms.ROOT/'model-routing.json').read_text())
  model='replacement-fast-image-high';key=model+'/high'
  self.records[key]=copy.deepcopy(self.records['future-specialist-high/high'])
  self.records[key].update(model=model,routing_family='flash',usage_tier='low',routing_priority=200,allowed_kinds=['image'])
  self.write_policy();path=self.root/'policy.json';policy=json.loads(path.read_text())
  policy['external_specialist_exception']={'allowed_usage_tiers':{key:'low'}}
  path.write_text(json.dumps(policy));self.sha=hashlib.sha256(path.read_bytes()).hexdigest()
  self.catalog+='\n'+model+' label'
  picked,_=self.run_select(kind='image',tiers=('low',))
  self.assertEqual((picked['model'],picked['effort']),(model,'high'))
  policy['external_models'][key]['allowed_kinds']=['copy']
  path.write_text(json.dumps(policy));self.sha=hashlib.sha256(path.read_bytes()).hexdigest()
  with self.assertRaisesRegex(ValueError,'No eligible live model'):
   self.run_select(kind='image',tiers=('low',))

 def test_ultra_capability_routes_and_long_input_without_model_ids(self):
  self.config=json.loads((ms.ROOT/'model-routing.json').read_text())
  for row in self.records.values():row['allowed_kinds']+=['visual','image']
  self.write_policy()
  for kind,size,requested,model,effort in [
   ('extract',1,None,'future-fast-medium','medium'),
   ('extract',1,'standard','future-fast-high','high'),
   ('code-map',1,None,'future-fast-medium','medium'),
   ('code-map',1,'standard','future-fast-high','high'),
   ('copy',1,None,'future-specialist-medium','medium'),
   ('english',1,None,'future-specialist-medium','medium'),
   ('prototype',1,None,'future-specialist-medium','medium'),
   ('prototype',1,'complex','future-specialist-high','high'),
   ('visual',1,None,'future-specialist-medium','medium'),
   ('visual',1,'complex','future-specialist-high','high'),
   ('extract',250000,None,'future-specialist-high','high'),
   ('copy',1,'routine','future-fast-medium','medium'),
   ('image',1,None,'future-fast-high','high')]:
   with self.subTest(kind=kind,size=size,requested=requested):
    picked,trace=self.run_select(kind,size,requested)
    self.assertEqual((picked['model'],picked['effort']),(model,effort))
    self.assertIsNone(trace['target_output_chars'])
  picked,_=self.run_select('english',250000,purpose='key_copy')
  self.assertEqual(picked['model'],'future-specialist-medium')
  picked,_=self.run_select('copy',tiers=['low'])
  self.assertEqual(picked['model'],'future-fast-high')

 def test_low_tier_scope_never_promotes_even_important_work(self):
  picked,trace=self.run_select('copy',purpose='important_communication',tiers=['low'])
  self.assertEqual(picked['model'],'future-fast-medium')
  self.assertIn('outside this task scope',trace['candidates'][0]['reason'])
  self.assertEqual(trace['selected']['tier'],'low')
 def test_flash_automatic_routes_do_not_silently_fall_back_to_low_effort(self):
  self.config=json.loads((ms.ROOT/'model-routing.json').read_text())
  self.catalog='future-fast-low label'
  with self.assertRaisesRegex(ValueError,'No eligible live model'):
   self.run_select('extract',tiers=['low'])
 def test_general_kind_and_long_material_stay_on_flash(self):
  self.assertEqual(self.run_select()[0]['model'],'future-fast-medium')
  self.assertEqual(self.run_select('english')[0]['effort'],'medium')
  picked,trace=self.run_select(size=150000)
  self.assertEqual(picked['model'],'future-fast-high');self.assertFalse(trace['specialist'])
 def test_purpose_efforts_and_future_ids(self):
  for purpose,kind,effort in [('tone_polish','copy','low'),('important_communication','copy','medium'),('sensitive_communication','english','high'),('key_copy','english','medium'),('technical_second_opinion','code-map','medium'),('technical_deep_review','code-map','high')]:
   with self.subTest(purpose=purpose):
    picked,trace=self.run_select(kind=kind,purpose=purpose)
    self.assertEqual(picked['model'],'future-specialist-'+effort)
    self.assertTrue(trace['specialist']);self.assertNotIn('require_no_overage',picked)
    self.assertEqual(picked['timeout_seconds'],{'low':600,'medium':900,'high':1200}[effort])
 def test_wait_defaults_follow_tier_and_selected_effort(self):
  self.assertEqual(ms.waiting_for('low','high',300)['timeout_seconds'],300)
  self.assertEqual(ms.waiting_for('high','low')['timeout_seconds'],600)
  self.assertEqual(ms.waiting_for('high','medium')['quiet_notice_seconds'],300)
  self.assertEqual(ms.waiting_for('high','high',requested=1800)['timeout_seconds'],1800)
  self.assertEqual(ms.waiting_for('low','high',requested=1800)['timeout_seconds'],600)
  for value in (True,0,1801):
   with self.assertRaises(ValueError):ms.waiting_for('high','high',requested=value)
 def test_invalid_wait_configuration_is_rejected(self):
  import copy
  for field,value in [('max_seconds',1801),('effort_seconds',{'high':True}),('quiet_notice_seconds',{'low':180,'medium':False,'high':420})]:
   waiting=copy.deepcopy(ms.DEFAULT['waiting']);waiting['high'][field]=value
   with self.assertRaises(ValueError):ms.validate_waiting(waiting)
  for cap in (601,1800):
   waiting=copy.deepcopy(ms.DEFAULT['waiting']);waiting['low']['max_seconds']=cap
   with self.assertRaises(ValueError):ms.validate_waiting(waiting)
 def test_quota_full_falls_back_without_failure(self):
  picked,trace=self.run_select('copy',purpose='important_communication',budget=False)
  self.assertEqual(picked['model'],'future-fast-medium');self.assertTrue(trace['explicit_fallback'])
  self.assertEqual(trace['candidates'][0]['reason'],'per_day')
 def test_gate_rejected_specialist_records_reason(self):
  picked,trace=self.run_select('code-map',purpose='technical_deep_review',blocked=('future-specialist-high',))
  self.assertEqual(picked['model'],'future-fast-high');self.assertTrue(trace['explicit_fallback'])
  self.assertEqual(trace['candidates'][0]['reason'],'capability ceiling')
 def test_large_material_falls_back_intact(self):
  picked,trace=self.run_select('english',size=150000,purpose='key_copy')
  self.assertEqual(picked['model'],'future-fast-medium')
  self.assertIn('Material exceeds',trace['candidates'][0]['reason'])
  self.assertEqual(trace['material_bytes'],150000)
 def test_wrong_kind_cannot_spend_specialist(self):
  picked,trace=self.run_select('extract',purpose='important_communication')
  self.assertEqual(picked['model'],'future-fast-medium');self.assertFalse(trace['specialist'])
 def test_effort_not_silently_escalated_when_missing(self):
  self.catalog='future-specialist-high label\nfuture-fast-medium label'
  picked,_=self.run_select('copy',purpose='important_communication')
  self.assertEqual(picked['model'],'future-fast-medium')
 def test_live_catalog_exact_intersection(self):
  self.catalog='future-specialist-high-extra label\nfuture-fast-high label'
  picked,_=self.run_select('code-map',purpose='technical_deep_review')
  self.assertEqual(picked['model'],'future-fast-high')
 def test_pending_ordinary_candidate_not_selected(self):
  self.records['future-fast-high/high'].update(automatic_eligible=False,automatic_reason='unknown effort');self.write_policy()
  picked,trace=self.run_select(size=150000)
  self.assertEqual(picked['model'],'legacy-fast-high');self.assertEqual(trace['candidates'][0]['reason'],'unknown effort')
 def test_policy_change_no_dispatch(self):
  self.sha='stale'
  with self.assertRaisesRegex(ValueError,'Policy changed'):self.run_select()
 def test_explicit_complexity_and_bad_value(self):
  self.assertEqual(self.run_select(requested='complex')[0]['model'],'future-fast-high')
  with self.assertRaises(ValueError):self.run_select(requested='maximum')
  with self.assertRaises(ValueError):self.run_select(purpose='unrestricted')

if __name__=='__main__':unittest.main()
