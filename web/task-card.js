// Minimal MCP Apps 2026-01-26 bridge; no network or model calls in the View.
// Protocol: https://github.com/modelcontextprotocol/ext-apps/blob/main/src/spec.types.ts
const articleEl=document.querySelector('article');
const $=id=>articleEl?.querySelector('#'+id)||document.getElementById(id),pending=new Map();
let serial=0,card=null,connected=false,refreshing=false,polling=false,active=true,opening=false,hostCapabilities={},lastCardJson='',seededJobId=null;
const terminal=new Set(['completed_unreviewed','failed_or_unverified','bridge_error','timeout','cancelled','interrupted']);
const errorStates=new Set(['failed','failed_or_unverified','bridge_error','timeout','incomplete_run','process_missing']);
const seenEventIds=new Set();

// Keep the canonical identifier in the tooltip; only the display name is formatted.
function renderModelHeader(value){
  const observed=String(value.observed_model||'').trim(),requested=String(value.requested_model||'').trim(),effort=String(value.requested_effort||'').trim();
  const raw=observed||requested;
  const suffix=effort&&['minimal','low','medium','high','xhigh','max'].includes(effort.toLowerCase())?'-'+effort.toLowerCase():'';
  const name=(suffix&&raw.toLowerCase().endsWith(suffix)?raw.slice(0,-suffix.length):raw).split(/[-_]+/).map(part=>part?part[0].toUpperCase()+part.slice(1):'').join(' ');
  $('model').textContent=raw?`${observed?'模型':'请求模型'} · ${name}`:'模型未记录';
  $('model').title=raw?`${observed?'实际返回模型':'请求模型'}：${raw}`:'';
  $('effort').textContent=effort?'思考 '+effort[0].toUpperCase()+effort.slice(1):'';
  $('effort').title='请求的思考强度；执行端未单独报告实际档位';
  $('effort').hidden=!effort;
  $('model').parentElement.hidden=false;
}

function notify(method,params={}){if(parent!==window)parent.postMessage({jsonrpc:'2.0',method,params},'*');}
function request(method,params){return new Promise((resolve,reject)=>{const id=++serial,timer=setTimeout(()=>{pending.delete(id);reject(new Error('连接暂未返回，请稍后刷新或查看详情。'));},15000);pending.set(id,{resolve,reject,timer});parent.postMessage({jsonrpc:'2.0',id,method,params},'*');});}
function resize(){notify('ui/notifications/size-changed',{height:Math.ceil(articleEl.getBoundingClientRect().height)});}
function showError(message){$('error').textContent=message;$('error').hidden=!message;}
function safeLink(value){try{const u=new URL(value);return u.protocol==='http:'&&u.hostname==='127.0.0.1'&&/^\d+$/.test(u.port)&&u.pathname==='/'&&!u.username&&!u.password&&u.searchParams.get('job')===card?.job_id?u.href:null;}catch{return null;}}
function timestamp(value){if(value===null||value===undefined)return '未记录';const d=new Date(typeof value==='number'?value*1000:value);return Number.isNaN(d.getTime())?'未记录':d.toLocaleString('zh-CN',{hour12:false});}
function deliveryURL(file){const link=safeLink(card?.task_view_url),base=link?new URL(link):null,job=card?.job_id;const path=file?.attachment_url,expected='/api/jobs/'+encodeURIComponent(job)+'/deliveries/'+encodeURIComponent(String(file?.id));if(!base||typeof path!=='string'||path.split('?')[0]!==expected)return null;try{const u=new URL(path,base.origin),params=[...u.searchParams];if(u.origin!==base.origin||u.pathname!==expected||u.username||u.password||path.includes('#')||u.hash||params.length>1||params.some(([key,value])=>key!=='ticket'||!value)||u.search&&params.length!==1)return null;return u.href;}catch{return null;}}
function fileSize(value){return Number.isFinite(value)&&value>=0?(value<1024?value+' B':value<1048576?(value/1024).toFixed(1)+' KB':(value/1048576).toFixed(1)+' MB'):'大小未记录';}
function extraSection(id,parent,before){let section=$(id);if(!section){section=document.createElement('section');section.id=id;parent.insertBefore(section,before||null);}section.replaceChildren();return section;}
function fileSummary(files){const outputs=files.filter(f=>f.source!=='trusted_project_tool');if(!outputs.length)return '已保存 '+files.length+' 份来源与工具记录';const names=[...new Set(outputs.map(f=>String(f.name||'未命名文件').split('/').pop()))];return '已保存 '+outputs.length+' 个交付文件 · '+names.slice(0,3).join('、')+(names.length>3?'等':'');}
function renderFiles(){
 const files=Array.isArray(card.deliverables)?card.deliverables:[],section=extraSection('delivery-files',$('delivery').parentNode,$('delivery'));section.hidden=!files.length;if(!files.length)return;
 const outputs=files.filter(f=>f.source!=='trusted_project_tool'),records=files.filter(f=>f.source==='trusted_project_tool');
 const append=(target,items)=>{for(const file of items){const row=document.createElement('p');row.textContent=(file.name||'未命名文件')+' · '+fileSize(file.bytes)+' ';const url=deliveryURL(file);if(url){const link=document.createElement('a');link.textContent='查看 / 下载';link.style.color='inherit';link.href=url;link.rel='noopener noreferrer';link.target='_blank';link.addEventListener('click',async event=>{event.preventDefault();if(!connected){showError('请通过完整页面中的文件链接查看。');return;}try{const r=await request('ui/open-link',{url});if(r?.isError)throw new Error();}catch{showError('宿主未能打开文件，请使用完整页面链接查看或下载。');resize();}});row.append(link);}else row.append(document.createTextNode('链接暂不可用，请查看完整页面'));target.append(row);}};
 if(outputs.length){if(outputs.length>4){const details=document.createElement('details'),title=document.createElement('summary');details.dataset.detailsKey='delivery-files';title.textContent='交付文件 · '+outputs.length+' · 查看全部';details.append(title);append(details,outputs);section.append(details);}else{const title=document.createElement('h3');title.textContent='交付文件 · '+outputs.length;section.append(title);append(section,outputs);}}
 if(records.length){const details=document.createElement('details'),title=document.createElement('summary');details.dataset.detailsKey='source-records';title.textContent='来源与工具记录 · '+records.length;details.append(title);append(details,records);section.append(details);}
}
function renderWorkspace(){const w=card.workspace,section=extraSection('workspace-details',$('task-details'),$('full-page'));section.hidden=w?.mode!=='workspace';if(section.hidden)return;const title=document.createElement('h3');title.textContent='任务工作区';section.append(title);for(const [label,value]of [['可读取目录',w.read_roots],['可写入目录',w.write_roots],['交付目录',w.output_directory]]){const row=document.createElement('p');row.style.overflowWrap='anywhere';row.textContent=label+'：'+(Array.isArray(value)?value.join('、')||'无':value||'未记录');section.append(row);}const details=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');details.dataset.detailsKey='workspace-cmd';summary.textContent='命令与工作区详情';pre.tabIndex=0;pre.textContent=JSON.stringify({working_directory:w.working_directory,commands:w.commands,worktree:w.worktree,enforcement:w.enforcement},null,2);details.append(summary,pre);section.append(details);}
function renderDetails(){
 const fields=[['任务编号',card.job_id],['创建时间',timestamp(card.created_at)],['更新时间',timestamp(card.updated_at)],['输入文件',card.input_count??'未记录'],['验收时间',timestamp(card.reviewed_at)]];
 if(card.error)fields.push(['异常原因',card.error]);
 $('detail-fields').replaceChildren();for(const [key,value]of fields){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=key;dd.textContent=String(value);$('detail-fields').append(dt,dd);}
 const isReviewed=['accepted','rejected'].includes(card.review);
 $('review-note').textContent=(isReviewed&&card.review_note)?card.review_note:(card.review==='pending'?'尚无验收记录，等待 Codex 对照要求核对。':'未记录详细验收说明。');
 $('criteria').replaceChildren();for(const criterion of Array.isArray(card.criteria)?card.criteria:[]){if(typeof criterion!=='string')continue;const li=document.createElement('li');li.textContent=criterion;$('criteria').append(li);}
 renderWorkspace();
 const url=safeLink(card.task_view_url);$('full-page').hidden=!url;if(url){$('full-link').href=url;$('page-url').value=url;}else{$('full-link').removeAttribute('href');$('page-url').value='';}
 $('inline-details').textContent=$('task-details').hidden?'卡片内详情':'收起详情';$('inline-details').setAttribute('aria-expanded',String(!$('task-details').hidden));
}
// Keep disclosure, keyboard focus and scroll positions during read-only updates.
function captureViewState(root){
 const details=[...root.querySelectorAll('details')];
 const key=d=>d.dataset.detailsKey||d.querySelector('summary')?.textContent||String(details.indexOf(d));
 const locate=el=>{const owner=el.closest('details'),scope=owner&&root.contains(owner)?owner:root;return {owner:scope===root?null:key(scope),id:el.id||null,key:el.dataset.focusKey||null,tag:el.tagName.toLowerCase(),index:[...scope.querySelectorAll(el.tagName.toLowerCase())].indexOf(el)};};
 const activeEl=document.activeElement;
 return {open:details.map(d=>[key(d),d.open]),focus:root.contains(activeEl)?locate(activeEl):null,scroll:[...root.querySelectorAll('pre, table')].map(el=>({target:locate(el),top:el.scrollTop,left:el.scrollLeft}))};
}
function restoreViewState(root,state){
 const details=[...root.querySelectorAll('details')],key=d=>d.dataset.detailsKey||d.querySelector('summary')?.textContent||String(details.indexOf(d));
 for(const [name,open]of state.open){const el=details.find(d=>key(d)===name);if(el)el.open=open;}
 const find=target=>{const scope=target.owner?details.find(d=>key(d)===target.owner):root;if(!scope)return null;const nodes=[...scope.querySelectorAll(target.tag)];return (target.id&&nodes.find(el=>el.id===target.id))||(target.key&&nodes.find(el=>el.dataset.focusKey===target.key))||nodes[target.index];};
 for(const saved of state.scroll){const el=find(saved.target);if(el){el.scrollTop=saved.top;el.scrollLeft=saved.left;}}
 if(state.focus){const el=find(state.focus);if(el&&typeof el.focus==='function')el.focus({preventScroll:true});}
}

function render(value){
 const next=value?.structuredContent?.card;if(!next||typeof next.job_id!=='string')return;
 const sig=JSON.stringify(next);
 if(sig===lastCardJson)return;
 lastCardJson=sig;
 if(document.body.dataset.ready!=='true')document.body.dataset.ready='true';
 if(card?.job_id&&card.job_id!==next.job_id){seenEventIds.clear();seededJobId=null;}
 const previous=captureViewState(articleEl);
 card=next;
 const isErr=errorStates.has(card.status)||errorStates.has(card.progress?.stage);
 let s=COPY.states[card.status]||{label:'需要查看',description:'请查看任务详情与结束原因。'};
 if(card.status==='completed_unreviewed'&&card.review==='accepted')s={label:'检查通过',description:'交付已由 Codex 核对通过，可查看内容与完整记录。'};
 if(card.status==='completed_unreviewed'&&card.review==='rejected')s={label:'检查未通过',description:'交付已返回，尚未被采用。请查看验收记录与待处理问题。'};
 $('title').textContent=(card.title||card.job_id).replace(/^(?:operator|scout|reader|researcher|analyst|builder|reviewer)\s*[:：]\s*/i,'');
 $('status').textContent=s.label;
 const statusClass=card.review==='accepted'?'accepted':(card.review==='rejected'||isErr)?'failed':card.status==='completed_unreviewed'?'pending':card.status==='running'?'running':'neutral';
 $('status').className='status '+statusClass;
 const hasDeliverables=Array.isArray(card.deliverables)&&card.deliverables.length>0;
 const errText=String(card.error||'').trim();
 $('description').textContent=hasDeliverables?fileSummary(card.deliverables):(isErr&&errText?errText:s.description);
 $('description').dataset.tone=(isErr&&!hasDeliverables)?'error':'';
 renderModelHeader(card);
 const revEl=$('review');
 revEl.textContent=isErr&&card.review==='pending'?'尚无结果可核对':(COPY.review[card.review]||COPY.review.pending);
 revEl.dataset.tone=card.review==='accepted'?'done':card.review==='rejected'?'error':'';
 const p=card.progress||{},waiting=!terminal.has(card.status);
 const notice=waiting&&p.no_recent_stream_activity?COPY.quiet_notice:waiting&&p.soft_budget_exceeded&&p.meaningful_events>0?COPY.progress_notice:'';
 $('notice').textContent=notice;$('notice').hidden=!notice;
 $('response').textContent=card.response||'';$('truncated').hidden=!card.response_truncated;
 $('delivery').hidden=!card.response;$('expand').disabled=!card.response;
 $('details').disabled=opening;$('inline-details').disabled=false;$('refresh').disabled=!connected||refreshing;
 Object.entries(COPY.actions).forEach(([id,label])=>{const el=$(id);if(el&&!(id==='refresh'&&refreshing))el.textContent=label;});
 renderDetails();renderFiles();renderActivity();restoreViewState(articleEl,previous);
 resize();
}
function renderActivity(){
 const a=card.progress?.activity||{},section=extraSection('live-activity',$('delivery').parentNode,$('delivery'));
 const isRunning=card.status==='running',isErr=errorStates.has(card.status),isDone=card.status==='completed_unreviewed'||Boolean(card.response);
 const isSeeded=seededJobId===card.job_id;
 if(isRunning){
   const heading=document.createElement('h3');heading.textContent='当前动态：'+(a.current_action||'等待下一条动态');section.append(heading);
 }else if((isErr||['cancelled','interrupted'].includes(card.status))&&a.current_action){
   const prev=document.createElement('p');prev.textContent='中断前最后动作：'+a.current_action;section.append(prev);
 }
 const last=document.createElement('p');last.textContent='最近有效活动：'+timestamp(a.last_effective_activity_at);section.append(last);
 for(const c of (Array.isArray(a.children)?a.children:[])){const row=document.createElement('p');row.textContent=c.role+' · '+c.short_id+' · '+c.status+' · '+c.last_action;section.append(row);}
 if(isRunning&&a.public_response_preview){const label=document.createElement('p'),pre=document.createElement('pre');label.textContent='正在回复'+(a.public_response_truncated?'（最近一段）':'');pre.dataset.focusKey='public-response';pre.tabIndex=0;pre.textContent=a.public_response_preview;section.append(label,pre);}
 else if(!isRunning&&!isDone&&a.public_response_preview){const label=document.createElement('p'),pre=document.createElement('pre');label.textContent='中断前已记录的回复片段';pre.dataset.focusKey='public-response';pre.tabIndex=0;pre.textContent=a.public_response_preview;section.append(label,pre);}
 const events=Array.isArray(a.recent_events)?a.recent_events.slice(-12):[];
 if(events.length){
   const details=document.createElement('details'),title=document.createElement('summary'),list=document.createElement('ol');
   details.dataset.detailsKey='recent-events';title.textContent='最近动态 · '+events.length;
   for(const e of events){
     const eKey=String(e.id||(e.action+':'+(e.at||'')));
     const isNew=isSeeded&&!seenEventIds.has(eKey);
     seenEventIds.add(eKey);
     const stateLabel=e.state==='running'?(isRunning?'进行中':isDone?'已记录':'未完成'):(({done:'已完成',error:'工具报告错误',observed:'已观察到'}[e.state])||'已观察到');
     const row=document.createElement('li');
     if(isNew)row.className='is-new';
     row.textContent=e.action+' · '+stateLabel+(e.at?' · '+timestamp(e.at):'');
     list.append(row);
   }
   details.append(title,list);section.append(details);
 }
 seededJobId=card.job_id;
}
async function refresh({silent=false}={}){
 if(!active||!connected||!card||refreshing||polling)return;
 if(silent)polling=true;
 else{refreshing=true;$('refresh').disabled=true;$('refresh').textContent='刷新中…';showError('');}
 try{
   const r=await request('tools/call',{name:'bridge_task_card',arguments:{job_id:card.job_id}});
   if(r?.isError)throw new Error('暂时无法读取，已有交付仍保留。');
   if(!silent)showError('');
   render(r);
 }catch(e){
   if(!silent)showError(e.message);
 }finally{
   if(silent)polling=false;
   else{refreshing=false;$('refresh').disabled=!connected;$('refresh').textContent=COPY.actions.refresh;}
 }
}
$('refresh').addEventListener('click',()=>refresh({silent:false}));
$('expand').addEventListener('click',()=>{$('delivery').open=!$('delivery').open;resize();});
function inlineDetails(){if(!card)return;$('task-details').hidden=!$('task-details').hidden;if(!$('task-details').hidden&&card.response)$('delivery').open=true;renderDetails();resize();}
$('inline-details').addEventListener('click',inlineDetails);
$('details').addEventListener('click',async()=>{
 if(!card||opening)return;const url=safeLink(card.task_view_url);showError('');
 if(!url){showError('任务完整页面链接暂不可用，请使用卡片内详情。');resize();return;}
 const prompt=`[Gemini Bridge 查看详情] 请只读查看已有任务 ${card.job_id}，使用 open_in_codex(target={type:"browser",url:"${url}"},placement="right") 在本聊天右侧打开完整页面。不创建、重试生成或修改验收。若返回 queued，只说明已排队，不宣称页面已打开。`;
 opening=true;$('details').disabled=true;$('action-status').hidden=false;$('action-status').textContent='正在请求 Codex 在右侧打开完整页面…';resize();
 try{
  const send=window.openai?.sendFollowUpMessage;
  let result;
  if(connected&&hostCapabilities.message?.text!==undefined)result=await request('ui/message',{role:'user',content:[{type:'text',text:prompt}]});
  else if(typeof send==='function')result=await window.openai.sendFollowUpMessage({prompt,scrollToBottom:false});
  else throw new Error('当前宿主未提供聊天操作接口。');
  if(result?.isError)throw new Error('宿主未接受打开请求。');
  $('action-status').textContent='已发送只读打开请求，等待 Codex 在右侧打开。';
 }catch(e){$('action-status').hidden=true;showError(`${e.message} 可使用卡片内详情或复制完整页面链接。`);if($('task-details').hidden)inlineDetails();}
 finally{opening=false;$('details').disabled=false;resize();}
});
$('page-url').addEventListener('click',()=>{$('page-url').select();});
$('full-link').addEventListener('click',async e=>{if(!connected)return;e.preventDefault();const url=safeLink(card?.task_view_url);if(!url)return;try{const r=await request('ui/open-link',{url});if(r?.isError)throw new Error();}catch{showError('宿主未能打开完整页面，请复制上方链接到浏览器。');resize();}});
function context(value){
 if(value?.theme==='dark'||value?.theme==='light'){
   document.documentElement.dataset.theme=value.theme;
   document.documentElement.style.colorScheme=value.theme;
 }
 const vars=value?.styles?.variables;
 if(vars&&typeof vars==='object'){
   for(const [name,v]of Object.entries(vars)){
     if((name.startsWith('--color-')||name.startsWith('--font-'))&&typeof v==='string'){
       document.documentElement.style.setProperty(name,v);
     }
   }
 }
}
window.addEventListener('message',e=>{
 if(e.source!==parent||!e.data||e.data.jsonrpc!=='2.0')return;const m=e.data;
 if(m.id!==undefined&&pending.has(m.id)&&!m.method){const p=pending.get(m.id);clearTimeout(p.timer);pending.delete(m.id);if(m.error)p.reject(new Error('宿主暂不支持此操作，请使用任务详情。'));else p.resolve(m.result);return;}
 if(m.method==='ui/notifications/tool-result')render(m.params);
 if(m.method==='ui/notifications/host-context-changed')context(m.params);
 if(m.method==='ping'&&m.id!==undefined)parent.postMessage({jsonrpc:'2.0',id:m.id,result:{}},'*');
 if(m.method==='ui/resource-teardown'){active=false;clearInterval(poll);for(const p of pending.values()){clearTimeout(p.timer);p.reject(new Error('视图已关闭'));}pending.clear();if(m.id!==undefined)parent.postMessage({jsonrpc:'2.0',id:m.id,result:{}},'*');}
});
function needsLiveRefresh(){return card&&(!terminal.has(card.status)||(card.status==='completed_unreviewed'&&!['accepted','rejected'].includes(card.review)));}
let lastPollAt=0;
const poll=setInterval(()=>{const now=Date.now(),delay=document.hidden||terminal.has(card?.status)?15000:2000;if(active&&needsLiveRefresh()&&!refreshing&&!polling&&now-lastPollAt>=delay){lastPollAt=now;refresh({silent:true});}},2000);
document.addEventListener('visibilitychange',()=>{if(active&&!document.hidden&&needsLiveRefresh())refresh({silent:true});});
if(parent!==window)request('ui/initialize',{appInfo:{name:'Gemini Bridge task card',version:'0.1.0'},appCapabilities:{availableDisplayModes:['inline']},protocolVersion:'2026-01-26'}).then(r=>{if(r.protocolVersion!=='2026-01-26')throw new Error('当前宿主暂不支持此卡片协议，请使用任务详情。');connected=true;hostCapabilities=r.hostCapabilities||{};context(r.hostContext);notify('ui/notifications/initialized');if(card)render({structuredContent:{card}});}).catch(e=>showError(e.message));
else showError('请在支持任务卡片的聊天中打开；完整详情页仍可独立使用。');
