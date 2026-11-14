"""Task-aware routing; scarce specialists require explicit purpose and budget."""
import hashlib
import json
import re
from pathlib import Path
from bridge import ROOT, get_model_catalog, MAX_GENERATION_SECONDS, GENERATION_EXIT_GRACE_SECONDS
import task_mode
from input_limits import load_limits

PURPOSES = task_mode.PURPOSES
DEFAULT = {
 'access':{},
 'general_preferences':{},
 'long_text_bytes':64000,
 'kind_complexity':{'copy':'standard','english':'standard','extract':'routine','code-map':'routine','visual':'complex','prototype':'complex','image':'complex'},
 'preferences':{
  'routine':[{'family':'flash','efforts':['medium','high']}],
  'standard':[{'family':'flash','efforts':['medium','high']}],
  'complex':[{'family':'flash','efforts':['high','medium']}]},
 'timeouts':{'routine':180,'standard':300,'complex':600},
 'waiting':{
  'low':{'max_seconds':600,'quiet_notice_seconds':90,'mode':'activity','activity_grace_seconds':120,'quiet_grace_seconds':120},
  'high':{'effort_seconds':{'low':600,'medium':900,'high':1200},'max_seconds':1800,'quiet_notice_seconds':{'low':180,'medium':300,'high':420},'mode':'activity','activity_grace_seconds':300,'quiet_grace_seconds':300}},
 'specialist_budget':{'per_task':1,'per_day':1,'rolling_days':7,'max_calls':3,'max_high_calls':1,'max_units':6,'effort_units':{'low':1,'medium':2,'high':3}},
 'specialist_routes':{
  'tone_polish':{'family':'reasoning','routing_role':'scarce_specialist','effort':'low','allowed_kinds':['copy','english'],'max_input_bytes':16000,'target_output_chars':1500,'flash_complexity':'routine'},
  'important_communication':{'family':'reasoning','routing_role':'scarce_specialist','effort':'medium','allowed_kinds':['copy','english'],'max_input_bytes':32000,'target_output_chars':3000,'flash_complexity':'standard'},
  'sensitive_communication':{'family':'reasoning','routing_role':'scarce_specialist','effort':'high','allowed_kinds':['copy','english'],'max_input_bytes':64000,'target_output_chars':4500,'flash_complexity':'complex'},
  'key_copy':{'family':'reasoning','routing_role':'scarce_specialist','effort':'medium','allowed_kinds':['copy','english'],'max_input_bytes':32000,'target_output_chars':4000,'flash_complexity':'standard'},
  'technical_second_opinion':{'family':'reasoning','routing_role':'scarce_specialist','effort':'medium','allowed_kinds':['code-map','prototype'],'max_input_bytes':48000,'target_output_chars':4000,'flash_complexity':'standard'},
  'technical_deep_review':{'family':'reasoning','routing_role':'scarce_specialist','effort':'high','allowed_kinds':['code-map'],'max_input_bytes':64000,'target_output_chars':6000,'flash_complexity':'complex'}}
}

def load_config(root=None):
 path=Path(root or ROOT)/'model-routing.json'
 config=json.loads(path.read_text()) if path.exists() else json.loads(json.dumps(DEFAULT))
 required={'long_text_bytes','kind_complexity','preferences','timeouts'}
 if not required <= set(config) or set(config)-set(DEFAULT) or type(config['long_text_bytes']) is not int or config['long_text_bytes']<1:
  raise ValueError('Invalid model-routing.json')
 config.setdefault('specialist_routes',{})
 config.setdefault('specialist_budget',dict(DEFAULT['specialist_budget']))
 config.setdefault('waiting',json.loads(json.dumps(DEFAULT['waiting'])))
 validate_waiting(config['waiting'])
 if set(config['preferences']) != {'routine','standard','complex'}:raise ValueError('Invalid complexity preferences')
 for complexity,prefs in config['preferences'].items():
  if not prefs or any(not isinstance(x,dict) or set(x)!={'family','efforts'} or not isinstance(x['family'],str) or not x['efforts'] or any(e not in ('low','medium','high','xhigh','max','fixed') for e in x['efforts']) for x in prefs):raise ValueError('Invalid family/effort preference')
  if type(config['timeouts'].get(complexity)) is not int or not 10<=config['timeouts'][complexity]<=600:raise ValueError('Invalid automatic timeout')
 if any(x not in config['preferences'] for x in config['kind_complexity'].values()):raise ValueError('Invalid kind complexity')
 for purpose,route in config['specialist_routes'].items():
  if purpose not in PURPOSES or not isinstance(route,dict) or route.get('effort') not in ('low','medium','high') or route.get('flash_complexity') not in config['preferences'] or not route.get('family') or not route.get('routing_role') or not route.get('allowed_kinds'):raise ValueError('Invalid specialist route')
  for key,maximum in [('max_input_bytes',load_limits(root or ROOT)['MAX_PACKED_BYTES']),('target_output_chars',64000)]:
   if route.get(key) is not None and (type(route[key]) is not int or not 1<=route[key]<=maximum):raise ValueError('Invalid specialist '+key)
 for kind,levels in config.get('general_preferences',{}).items():
  if kind not in task_mode.KINDS or not isinstance(levels,dict) or set(levels)-set(config['preferences']):raise ValueError('Invalid general capability route')
  for prefs in levels.values():
   if not isinstance(prefs,list) or not prefs or any(not isinstance(p,dict) or set(p)!={'family','efforts'} or not isinstance(p['family'],str) or not isinstance(p['efforts'],list) or not p['efforts'] or any(e not in ('low','medium','high','xhigh','max','fixed') for e in p['efforts']) for p in prefs):raise ValueError('Invalid capability effort preferences')
 return config

def validate_waiting(waiting):
 if not isinstance(waiting,dict) or set(waiting)!={'low','high'}:raise ValueError('Invalid waiting tiers')
 for tier,row in waiting.items():
  cap=600 if tier=='low' else MAX_GENERATION_SECONDS
  if not isinstance(row,dict) or type(row.get('max_seconds')) is not int or not 10<=row['max_seconds']<=cap:raise ValueError('Invalid generation waiting cap')
  if row.get('mode','activity') not in ('activity','fixed'):raise ValueError('Invalid waiting mode')
  for key in ('activity_grace_seconds','quiet_grace_seconds'):
   value=row.get(key,120 if tier=='low' else 300)
   if type(value) is not int or not 0<=value<=row['max_seconds']:raise ValueError('Invalid activity waiting grace')
  quiet=row.get('quiet_notice_seconds')
  if tier=='high':
   efforts=row.get('effort_seconds')
   if not isinstance(efforts,dict) or set(efforts)!={'low','medium','high'} or any(type(v) is not int or not 10<=v<=row['max_seconds'] for v in efforts.values()):raise ValueError('Invalid effort waiting defaults')
   if not isinstance(quiet,dict) or set(quiet)!=set(efforts):raise ValueError('Invalid quiet waiting thresholds')
   quiet=list(quiet.values())
  else:quiet=[quiet]
  if any(type(v) is not int or not 10<=v<=MAX_GENERATION_SECONDS for v in quiet):raise ValueError('Invalid quiet waiting threshold')

 # Optional effort budgets preserve the ordinary tier cap while allowing slower thinking modes.
 for tier,row in waiting.items():
  overrides=row.get('effort_overrides',{})
  if not isinstance(overrides,dict) or set(overrides)-{'low','medium','high','xhigh','max','fixed'}:raise ValueError('Invalid effort waiting overrides')
  for effort,extra in overrides.items():
   if not isinstance(extra,dict) or set(extra)-{'max_seconds','default_seconds','quiet_notice_seconds','activity_grace_seconds','quiet_grace_seconds'}:raise ValueError('Invalid effort budget fields')
   cap=extra.get('max_seconds',row['max_seconds'])
   if type(cap) is not int or not 10<=cap<=MAX_GENERATION_SECONDS:raise ValueError('Invalid effort budget cap')
   for key,value in extra.items():
    if type(value) is not int or not 0<=value<=cap:raise ValueError('Invalid effort budget value')
   if 'default_seconds' in extra and extra['default_seconds']<10:raise ValueError('Invalid effort timeout')

def waiting_for(tier,effort,fallback_seconds=180,*,requested=None,config=None):
 """Snapshot generation policy after choosing the actual model, never by ID."""
 config=config or load_config()
 if tier not in ('low','high'):raise ValueError('Unknown model waiting tier')
 row=config['waiting'][tier]
 if requested is not None and (type(requested) is not int or not 10<=requested<=MAX_GENERATION_SECONDS):raise ValueError('Invalid requested generation timeout')
 extra=row.get('effort_overrides',{}).get(effort,{})
 if effort=='fixed':
  if 'default_seconds' not in extra:raise ValueError('Fixed configuration requires its own waiting budget')
  default=extra['default_seconds'];quiet=extra.get('quiet_notice_seconds',180)
 elif tier=='high':
  if effort not in row['effort_seconds']:raise ValueError('Unknown high-tier effort waiting policy')
  default=row['effort_seconds'][effort];quiet=row['quiet_notice_seconds'][effort]
 else:default=fallback_seconds;quiet=row['quiet_notice_seconds']
 cap=extra.get('max_seconds',row['max_seconds']);default=extra.get('default_seconds',default);quiet=extra.get('quiet_notice_seconds',quiet)
 seconds=min(cap,requested if requested is not None else default)
 return {'usage_tier':tier,'timeout_seconds':seconds,'max_seconds':cap,
         'mode':row.get('mode','activity'),'hard_timeout_seconds':cap if row.get('mode','activity')=='activity' else seconds,
         'activity_grace_seconds':extra.get('activity_grace_seconds',row.get('activity_grace_seconds',120 if tier=='low' else 300)),
         'quiet_grace_seconds':extra.get('quiet_grace_seconds',row.get('quiet_grace_seconds',120 if tier=='low' else 300)),
         'quiet_notice_seconds':quiet,'hard_timeout_grace_seconds':GENERATION_EXIT_GRACE_SECONDS,
         'clock':'CLI process launch; excludes queue and preflight'}

def classify(kind,material_bytes,requested=None,config=None):
 config=config or load_config()
 if requested is not None and requested not in config['preferences']:raise ValueError('task_complexity must be routine, standard or complex')
 if requested:return requested,'Coordinator specified task complexity'
 if material_bytes>config['long_text_bytes']:return 'complex','Whole long text exceeds configured routing threshold'
 return config['kind_complexity'].get(kind,'standard'),'Configured task-kind preference'

def budget_status(task_id=None,effort=None):
 from specialist_budget import SpecialistBudget
 try:return SpecialistBudget(ROOT).status(load_config()['specialist_budget'],task_id=task_id,effort=effort)
 except (ValueError,OSError) as error:return {'allowed':False,'reason':'Specialist budget unavailable: '+str(error)}

def candidates(thread_id,kind,complexity,*,catalog=None,executing=False,task_purpose='general',material_bytes=0,task_id=None,allowed_model_tiers=None,workspace=None):
 config=load_config()
 if task_purpose not in PURPOSES:raise ValueError('Unsupported task_purpose')
 route=config['specialist_routes'].get(task_purpose)
 catalog=get_model_catalog() if catalog is None else catalog
 raw=(task_mode.ROUTING/'policy.json').read_bytes();policy=json.loads(raw)
 available={line.split()[0] for line in catalog.splitlines() if line.split()}
 prefs=config.get('general_preferences',{}).get(kind,{}).get(complexity) if task_purpose=='general' else None
 if prefs is None:prefs=([{'family':route['family'],'efforts':[route['effort']]}] if route else [])+config['preferences'][complexity]
 rows=[]
 allowed_model_tiers = ['low'] if allowed_model_tiers is None else allowed_model_tiers
 for record in policy.get('external_models',{}).values():
  family=record.get('routing_family');effort=record.get('reasoning_effort');specialist=record.get('specialist_only',False)
  # Ordinary work never promotes itself to scarce specialists because of size.
  if specialist and not route:continue
  rank=next(((i,p['efforts'].index(effort)) for i,p in enumerate(prefs) if p['family']==family and effort in p['efforts']),None)
  if rank is None:continue
  reason=None;decision=None;budget=None
  tier=record.get('usage_tier','high')
  if tier not in allowed_model_tiers:reason='Model tier is outside this task scope; explicit Gemini chat opt-in permits both tiers unless the user limits them'
  elif record.get('requires_workspace_execution') and (workspace is None or not workspace.get('commands') or workspace.get('subagents')):reason='Composite auto preference requires a scoped implementation workspace with commands and no native children'
  elif record.get('model') not in available:reason='Not reported by live CLI catalog'
  elif specialist and tier!=policy.get('external_specialist_exception',{}).get('allowed_usage_tiers',{}).get(record['model']+'/'+effort,'high'):reason='Model tier differs from its exact approved exception mapping'
  elif specialist and record.get('routing_role')!=route.get('routing_role'):reason='Model is not assigned to this specialist role'
  elif specialist and kind not in route['allowed_kinds']:reason='Purpose does not qualify this task kind for specialist use'
  elif specialist and route.get('max_input_bytes') is not None and material_bytes>route['max_input_bytes']:reason='Material exceeds bounded specialist scope; retain full material on Flash'
  elif not specialist and record.get('automatic_eligible',True) is not True:reason=record.get('automatic_reason','Automatic qualification pending')
  elif kind not in record.get('allowed_kinds',[]):reason='Model is not qualified for task kind'
  if reason is None and specialist:
   budget=budget_status(task_id,effort)
   if not budget['allowed']:reason=budget['reason']
  if reason is None:
   kwargs={'executing':executing}
   if task_purpose!='general' or specialist:kwargs.update(task_purpose=task_purpose,kind=kind,dispatch_mode='auto')
   decision=task_mode.runtime_decision(thread_id,{'model':record['model'],'effort':effort},**kwargs)
   if decision.get('policy_sha256') != hashlib.sha256(raw).hexdigest():raise ValueError('Policy changed during model selection; retry read-only selection')
   if not decision.get('enabled'):reason=decision.get('reason','Policy gate rejected')
  rows.append({'model':record['model'],'effort':effort,'tier':tier,'eligible':reason is None,'reason':reason,'decision':decision,'rank':rank,'priority':record.get('routing_priority',0),'score':record.get('score',0) or 0,'specialist':specialist,'budget':budget,'model_execution':record.get('model_execution')})
 rows.sort(key=lambda r:(r['rank'],-r['priority'],-r['score'],r['model']))
 return rows

def select(thread_id,kind,material_bytes,requested=None,*,task_purpose='general',task_id=None,allowed_model_tiers=None,workspace=None):
 config=load_config();complexity,reason=classify(kind,material_bytes,requested,config)
 route=config['specialist_routes'].get(task_purpose)
 if route and task_purpose!='general' and requested is None:
  complexity=route['flash_complexity'];reason='Purpose-specific specialist effort and Flash fallback'
 rows=candidates(thread_id,kind,complexity,task_purpose=task_purpose,material_bytes=material_bytes,task_id=task_id,allowed_model_tiers=allowed_model_tiers,workspace=workspace)
 chosen=next((r for r in rows if r['eligible']),None)
 if chosen is None:raise ValueError('No eligible live model: '+json.dumps([{k:r[k] for k in ('model','effort','reason')} for r in rows],ensure_ascii=False))
 wait=waiting_for(chosen['tier'],chosen['effort'],config['timeouts'][complexity],config=config)
 picked={'model':chosen['model'],'effort':chosen['effort'],'timeout_seconds':wait['timeout_seconds']}
 trace={'mode':'task_aware','task_purpose':task_purpose,'complexity':complexity,'reason':reason,'material_bytes':material_bytes,'selected':{'model':chosen['model'],'effort':chosen['effort'],'tier':chosen['tier']},'specialist':chosen['specialist'],'target_output_chars':route['target_output_chars'] if chosen['specialist'] else None,'candidates':[{k:r[k] for k in ('model','effort','tier','eligible','reason')} for r in rows],'policy_sha256':chosen['decision']['policy_sha256'],'explicit_fallback':chosen['rank'][0]>0,'effort_evidence':'requested configuration; independent backend effort not reported'}
 trace['waiting_policy']=wait
 if route:trace['budget']=budget_status(task_id,route['effort'])
 return picked,trace
