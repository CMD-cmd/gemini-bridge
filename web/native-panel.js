// Read-only task view. Display and refresh never dispatch a model.
const mainEl=document.querySelector('main');
const $=id=>mainEl?.querySelector('#'+id)||document.getElementById(id),pending=new Map();
let sequence=0,connected=false,card=null,detail=null,mode='inline',localExpanded=false,tab='submission',userPickedTab=false,lastJobId=null,busy=false,polling=false,loadError='',proseExpanded=false,displaySeq=0;
const modeWaiters=new Set(),seenEventIds=new Set();
let seededJobId=null,renderedContent=null,lastSize=-1,switchTimer=null;
const ERROR_STATES=new Set(['failed','failed_or_unverified','bridge_error','timeout','incomplete_run','process_missing']);

// Keep the canonical identifier in the tooltip; only the display name is formatted.
function renderModelHeader(value){
  const observed=String(value.observed_model||'').trim(),requested=String(value.requested_model||'').trim(),effort=String(value.requested_effort||'').trim();
  const execution=value.model_execution||{},orchestrated=execution.mode==='orchestrated',fixed=orchestrated||execution.effort_mode==='fixed'||effort.toLowerCase()==='fixed';
  const executionLabel=execution.label||value.execution_label;
  const raw=observed||requested;
  const suffix=effort&&['minimal','low','medium','high','xhigh','max'].includes(effort.toLowerCase())?'-'+effort.toLowerCase():'';
  const name=(suffix&&raw.toLowerCase().endsWith(suffix)?raw.slice(0,-suffix.length):raw).split(/[-_]+/).map(part=>part?part[0].toUpperCase()+part.slice(1):'').join(' ');
  $('model').textContent=executionLabel?`组合执行 · ${executionLabel}`:raw?`${observed?'模型':'请求模型'} · ${name}`:'模型未记录';
  $('model').title=orchestrated?`所选组合：${raw||'未记录'}；记录未逐步追踪实际后端模型`:raw?`${observed?'实际返回模型':'请求模型'}：${raw}`:'';
  $('effort').textContent=fixed?'思考 系统固定':effort?'思考 '+effort[0].toUpperCase()+effort.slice(1):'';
  $('effort').title=fixed?'组合采用系统固定配置，不提供独立的思考强度选项':'请求的思考强度；执行端未单独报告实际档位';
  $('effort').hidden=!effort&&!fixed;
  $('model').parentElement.hidden=false;
}

function executionConfiguration(){
  const execution=card?.model_execution||detail?.state?.model_execution||detail?.report?.model_execution||detail?.profile?.model_execution||{};
  return {label:execution.label||card?.execution_label||card?.requested_model,
    orchestrated:execution.mode==='orchestrated',
    fixed:execution.mode==='orchestrated'||execution.effort_mode==='fixed'||card?.requested_effort==='fixed'};
}
function executionConfigurationHTML(){
  const e=executionConfiguration();
  if(!e.orchestrated&&!e.fixed)return '';
  return '<h2>执行配置</h2><p>'+escapeHTML(e.label||'组合选项未记录')+' · 思考 系统固定</p>';
}

function waitForMode(target){if(mode===target)return Promise.resolve(true);return new Promise(resolve=>{const entry={target,resolve,timer:null};entry.timer=setTimeout(()=>{modeWaiters.delete(entry);resolve(false);},3500);modeWaiters.add(entry);});}
function request(method,params){return new Promise((resolve,reject)=>{const id=++sequence;const timer=setTimeout(()=>{pending.delete(id);reject(new Error('页面暂时没有响应，请稍后再试。'));},10000);pending.set(id,{resolve,reject,timer});parent.postMessage({jsonrpc:'2.0',id,method,params},'*');});}
function size(){const height=Math.ceil(mainEl.getBoundingClientRect().height);if(parent!==window&&height!==lastSize){lastSize=height;parent.postMessage({jsonrpc:'2.0',method:'ui/notifications/size-changed',params:{height}},'*');}}
function note(text,tone=''){const el=$('notice');el.textContent=text;if(tone)el.dataset.tone=tone;else delete el.dataset.tone;size();}

function syncTabSlider(animate=false){
  const nav=mainEl.querySelector('nav.tabs');
  const activeBtn=nav?.querySelector(`[data-tab="${tab}"]`);
  if(!nav||!activeBtn||$('viewer').hidden)return;
  if(animate)nav.dataset.slide='true';
  else delete nav.dataset.slide;
  nav.style.setProperty('--tab-x',activeBtn.offsetLeft+'px');
  nav.style.setProperty('--tab-w',activeBtn.offsetWidth+'px');
}

function syncViewerVisibility(){
  const wasHidden=$('viewer').hidden;
  const showViewer=mode==='fullscreen'||(mode==='inline'&&localExpanded);
  document.body.dataset.mode=mode;
  document.body.dataset.expanded=String(mode==='inline'&&localExpanded);
  $('viewer').hidden=!showViewer;
  $('inline').hidden=mode==='inline';
  $('local').textContent=localExpanded?'收起预览':'展开预览';
  $('local').setAttribute('aria-expanded',String(localExpanded));
  if(showViewer&&wasHidden)syncTabSlider(false);
}

function applyMode(value){
  if(!['inline','fullscreen'].includes(value))return;
  if(value==='inline'&&mode!==value)localExpanded=false;
  mode=value;
  for(const entry of modeWaiters){if(entry.target===mode){clearTimeout(entry.timer);modeWaiters.delete(entry);entry.resolve(true);}}
  syncViewerVisibility();
  renderTab(false);
}

function applyHostContext(ctx){
  if(!ctx||typeof ctx!=='object')return;
  if(ctx.displayMode)applyMode(ctx.displayMode);
  if(ctx.theme==='dark'||ctx.theme==='light'){
    document.documentElement.dataset.theme=ctx.theme;
    document.documentElement.style.colorScheme=ctx.theme;
  }
  const vars=ctx.styles?.variables;
  if(vars&&typeof vars==='object'){
    for(const [name,val] of Object.entries(vars)){
      if((name.startsWith('--color-')||name.startsWith('--font-'))&&typeof val==='string'){
        document.documentElement.style.setProperty(name,val);
      }
    }
  }
}

function readMeta(result){const candidates=[result?._meta,result?.mcp_tool_result?._meta,result?.call_tool_result?._meta,result];return candidates.find(value=>value?.panel_detail||value?.panel_card)||{};}
const escapeHTML=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function inline(value){return escapeHTML(String(value??'').replace(/\[([^\]\n]+)\]\(file:\/\/[^)\n]+\)/g,'$1')).replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');}

// Local text-only Markdown: no raw HTML, external resources or executable links.
function markdown(value){let out='',list=false,code=false,buffer=[],table=false;const closeList=()=>{if(list){out+='</ul>';list=false;}};const closeTable=()=>{if(table){out+='</tbody></table>';table=false;}};for(const line of String(value||'').split('\n')){if(/^```/.test(line)){closeList();closeTable();if(code){out+='<pre tabindex="0">'+escapeHTML(buffer.join('\n'))+'</pre>';buffer=[];}code=!code;continue;}if(code){buffer.push(line);continue;}if(/^\s*\|.*\|\s*$/.test(line)){closeList();if(/^\s*\|[\s:|\-]+\|\s*$/.test(line))continue;if(!table){out+='<table><tbody>';table=true;}out+='<tr>'+line.trim().slice(1,-1).split('|').map(c=>'<td>'+inline(c.trim())+'</td>').join('')+'</tr>';continue;}closeTable();const heading=line.match(/^#{1,6}\s+(.+)/);if(heading){closeList();out+='<h3>'+inline(heading[1])+'</h3>';continue;}const bullet=line.match(/^\s*(?:[-*]|\d+[.)、])\s+(.+)/);if(bullet){if(!list){out+='<ul>';list=true;}out+='<li>'+inline(bullet[1])+'</li>';continue;}closeList();if(line.trim())out+='<p>'+inline(line)+'</p>';}closeList();closeTable();if(code)out+='<pre tabindex="0">'+escapeHTML(buffer.join('\n'))+'</pre>';return out;}
function jsonValue(text){try{return JSON.parse(String(text).trim().replace(/^```(?:json)?\s*/i,'').replace(/\s*```$/,''));}catch{return null;}}

const fieldNames={states:'任务状态',reviews:'检查状态',review:'检查说明',actions:'操作按钮',buttons:'操作按钮',queued:'排队中',running:'进行中',completed_unreviewed:'待检查',accepted:'检查通过',rejected:'未通过检查',failed_or_unverified:'需核对',cancelled:'已取消',label:'名称',description:'说明',pending:'待检查',refresh:'刷新',expand:'展开结果',details:'查看详情',title:'标题',summary:'摘要',conclusion:'结论',overview:'概览',findings:'发现',recommendations:'建议',files:'文件',issues:'问题',result:'结果'};

function structured(value,level=0){if(value===null)return '<p>未提供</p>';if(typeof value!=='object')return '<p>'+inline(String(value))+'</p>';if(Array.isArray(value))return '<ul>'+value.map(v=>'<li>'+structured(v,level+1)+'</li>').join('')+'</ul>';return Object.entries(value).map(([key,v])=>{const title=fieldNames[key]||key;if(v&&typeof v==='object'&&!Array.isArray(v)&&typeof v.label==='string'&&typeof v.description==='string')return '<h4>'+inline(v.label)+'</h4><p>'+inline(v.description)+'</p>';return '<'+(level===0?'h3':'h4')+'>'+inline(title)+'</'+(level===0?'h3':'h4')+'>'+structured(v,level+1);}).join('');}
function deliveryHTML(text){const parsed=jsonValue(text);return parsed&&typeof parsed==='object'?structured(parsed):markdown(text);}

function clip(value,n=120){const text=String(value||'').replace(/\s+/g,' ').trim();return text.length>n?text.slice(0,n)+'…':text;}

// 优先提取人类可读结论句，其次提取首条要点，最后回退到中文化结构统计
function deliverySummary(text){
  const parsed=jsonValue(text);
  if(parsed&&typeof parsed==='object'&&!Array.isArray(parsed)){
    for(const key of ['summary','conclusion','overview','result','message','title']){
      if(typeof parsed[key]==='string'&&parsed[key].trim())return clip(parsed[key],92);
    }
    for(const key of ['findings','recommendations','issues']){
      const arr=parsed[key];
      if(Array.isArray(arr)&&arr.length){
        const head=arr[0];
        const sample=typeof head==='string'?head:(head?.summary||head?.title||head?.label||head?.description||'');
        if(sample)return clip('共 '+arr.length+' 项'+(fieldNames[key]||'要点')+'：'+sample,92);
      }
    }
    const parts=Object.entries(parsed).filter(([,v])=>v&&typeof v==='object').map(([k,v])=>(Array.isArray(v)?v.length:Object.keys(v).length)+' 项'+(fieldNames[k]||'内容'));
    return parts.length?'已返回 '+parts.slice(0,3).join('、'):'已返回结构化结果';
  }
  const clean=String(text||'').replace(/^```[\s\S]*?```/gm,'');
  const first=clean.split('\n').map(v=>v.replace(/^\s*(?:#{1,6}|[-*]|\d+[.)、])\s*/,'').replace(/[*`]/g,'').trim()).find(v=>v&&!/^[-|:\s]+$/.test(v));
  return clip(first||'内容已返回',92);
}

function taskURL(){try{const u=new URL(card?.task_view_url);return u.protocol==='http:'&&u.hostname==='127.0.0.1'&&/^\d+$/.test(u.port)&&u.pathname==='/'&&!u.username&&!u.password&&u.searchParams.get('job')===card?.job_id?u:null;}catch{return null;}}
function deliveryURL(file){const base=taskURL(),job=card?.job_id;const path=file?.attachment_url,expected='/api/jobs/'+encodeURIComponent(job)+'/deliveries/'+encodeURIComponent(String(file?.id));if(!base||typeof path!=='string'||path.split('?')[0]!==expected)return null;try{const u=new URL(path,base.origin),params=[...u.searchParams];if(u.origin!==base.origin||u.pathname!==expected||u.username||u.password||path.includes('#')||u.hash||params.length>1||params.some(([key,value])=>key!=='ticket'||!value)||u.search&&params.length!==1)return null;return u.href;}catch{return null;}}
function deliveryFiles(){return Array.isArray(detail?.deliverables)?detail.deliverables:Array.isArray(card?.deliverables)?card.deliverables:[];}
function fileSize(value){return Number.isFinite(value)&&value>=0?(value<1024?value+' B':value<1048576?(value/1024).toFixed(1)+' KB':(value/1048576).toFixed(1)+' MB'):'大小未记录';}
function fileSummary(files){const outputs=files.filter(f=>f.source!=='trusted_project_tool');if(!outputs.length)return '已保存 '+files.length+' 份来源与工具记录';const names=[...new Set(outputs.map(f=>String(f.name||'未命名文件').split('/').pop()))];return '已保存 '+outputs.length+' 个交付文件 · '+clip(names.slice(0,3).join('、')+(names.length>3?'等':''),92);}
function fileHTML(){
 const files=deliveryFiles(),outputs=files.filter(f=>f.source!=='trusted_project_tool'),records=files.filter(f=>f.source==='trusted_project_tool');if(!files.length)return '';
 const list=items=>'<ul class="criteria">'+items.map(file=>{const url=deliveryURL(file);return '<li>'+escapeHTML(file.name||'未命名文件')+' · '+escapeHTML(fileSize(file.bytes))+(url?' · <a data-delivery-link href="'+escapeHTML(url)+'" target="_blank" rel="noopener noreferrer">查看 / 下载</a>':'（链接暂不可用，可查看完整记录）')+'</li>';}).join('')+'</ul>';
 return (outputs.length?(outputs.length>4?'<details data-details-key="delivery-files"><summary>交付文件 · '+outputs.length+' · 查看全部</summary>'+list(outputs)+'</details>':'<h2>交付文件 · '+outputs.length+'</h2>'+list(outputs)):'')+(records.length?'<details data-details-key="source-records"><summary>来源与工具记录 · '+records.length+'</summary>'+list(records)+'</details>':'');
}
function workspaceHTML(){const w=detail?.workspace||card?.workspace;if(w?.mode!=='workspace')return '';const rows=[['可读取目录',w.read_roots],['可写入目录',w.write_roots],['交付目录',w.output_directory]];return '<h2>任务工作区</h2><div class="prose">'+rows.map(([label,value])=>'<h3>'+label+'</h3><p>'+escapeHTML(Array.isArray(value)?value.join('\n')||'无':value||'未记录').replace(/\n/g,'<br>')+'</p>').join('')+'</div><details data-details-key="workspace-cmd"><summary>命令与工作区详情</summary><pre class="raw" tabindex="0">'+escapeHTML(JSON.stringify({working_directory:w.working_directory,commands:w.commands,worktree:w.worktree,enforcement:w.enforcement},null,2))+'</pre></details>';}

function taskText(){if(detail?.submission?.prompt)return detail.submission.prompt;const raw=detail?.prompt||'';const match=raw.match(/(?:^|\n)TASK:\n([\s\S]*?)(?=\n(?:OUTPUT CONTRACT|ACCEPTANCE|REFERENCES):|$)/);return match?match[1].trim():raw;}
function response(){return detail?.report?.response||card?.response||'';}
function criteria(){return detail?.acceptance?.criteria||detail?.submission?.acceptance||card?.criteria||[];}
const REVIEW_LABELS={accepted:'已采纳',accepted_with_changes:'修订后采纳',partially_accepted:'部分采纳',revision_requested:'待修改',rejected:'未采纳'};
function reviewed(){return Object.hasOwn(REVIEW_LABELS,card?.review);}
function reviewTone(){return ['accepted','accepted_with_changes'].includes(card?.review)?'done':card?.review==='rejected'?'error':reviewed()?'pending':'';}
function complete(){return ['completed','completed_unreviewed','success'].includes(card?.status);}
function progressObj(){return detail?.progress||card?.progress||{};}
function hasErrorState(){const p=progressObj();return ERROR_STATES.has(card?.status)||ERROR_STATES.has(p.stage);}
function errorReason(){return String(card?.error||detail?.state?.error||'').trim();}

function reviewVerdict(){
  if(reviewed())return REVIEW_LABELS[card.review];
  if(complete()||response())return '待 Codex 检查';
  if(hasErrorState())return '尚无结果可核对';
  return '待返回后由 Codex 核对';
}

function reviewLineText(){
  const rawNote=(detail?.acceptance?.note||card?.review_note||'').trim();
  if(reviewed())return REVIEW_LABELS[card.review]+' · '+(rawNote?clip(rawNote,66):'具体内容见「检查」');
  if(complete()||response())return '待检查 · 等待 Codex 对照要求核对';
  if(hasErrorState())return '尚无有效结果可供核对';
  return '待返回后由 Codex 核对';
}

function statusBadge(){
  if(loadError&&!card)return {text:'读取失败',tone:'error'};
  if(reviewed())return {text:REVIEW_LABELS[card.review],tone:reviewTone()};
  if(hasErrorState())return {text:'运行需核对',tone:'error'};
  if(complete())return {text:'已返回 · 待检查',tone:'pending'};
  const p=progressObj();
  if(card?.status==='running'){
    if(p.no_recent_stream_activity)return {text:'暂未收到新动态',tone:'neutral'};
    const stageBadge={initializing:'正在启动',awaiting_response:'等待响应',receiving_response:'正在接收回复',tool_running:'处理材料中',finalizing:'整理结果中',waiting_permission:'等待权限确认',waiting_input:'等待补充信息'};
    return {text:stageBadge[p.stage]||'进行中',tone:'running'};
  }
  if(card?.status==='queued'||p.stage==='queued')return {text:'排队中',tone:'neutral'};
  if(['cancelled','interrupted','cancelling'].includes(card?.status))return {text:card.status==='cancelling'?'正在取消':'已取消',tone:'neutral'};
  return {text:'需关注',tone:'pending'};
}

// Keep disclosure, keyboard focus and scroll positions during read-only updates.
function captureViewState(root){
 const details=[...root.querySelectorAll('details')];
 const key=d=>d.dataset.detailsKey||d.querySelector('summary')?.textContent||String(details.indexOf(d));
 const locate=el=>{const owner=el.closest('details'),scope=owner&&root.contains(owner)?owner:root;return {owner:scope===root?null:key(scope),id:el.id||null,key:el.dataset.focusKey||null,tag:el.tagName.toLowerCase(),index:[...scope.querySelectorAll(el.tagName.toLowerCase())].indexOf(el)};};
 const active=document.activeElement;
 return {open:details.map(d=>[key(d),d.open]),focus:root.contains(active)?locate(active):null,scroll:[...root.querySelectorAll('pre, .prose table, table')].map(el=>({target:locate(el),top:el.scrollTop,left:el.scrollLeft}))};
}
function restoreViewState(root,state){
 const details=[...root.querySelectorAll('details')],key=d=>d.dataset.detailsKey||d.querySelector('summary')?.textContent||String(details.indexOf(d));
 for(const [name,open]of state.open){const el=details.find(d=>key(d)===name);if(el)el.open=open;}
 const find=target=>{const scope=target.owner?details.find(d=>key(d)===target.owner):root;if(!scope)return null;const nodes=[...scope.querySelectorAll(target.tag)];return (target.id&&nodes.find(el=>el.id===target.id))||(target.key&&nodes.find(el=>el.dataset.focusKey===target.key))||nodes[target.index];};
 for(const saved of state.scroll){const el=find(saved.target);if(el){el.scrollTop=saved.top;el.scrollLeft=saved.left;}}
 if(state.focus){const el=find(state.focus);if(el&&typeof el.focus==='function')el.focus({preventScroll:true});}
}

function activityHTML(){
  const a=progressObj().activity||{},events=Array.isArray(a.recent_events)?a.recent_events.slice(-12):[],children=Array.isArray(a.children)?a.children:[];
  const isRunning=card?.status==='running',isErr=hasErrorState(),isDone=complete()||Boolean(response());
  const isSeeded=seededJobId===card?.job_id;
  let headline='';
  if(isRunning)headline='当前动态：'+(a.current_action||'等待下一条动态');
  else if(isErr||['cancelled','interrupted'].includes(card?.status))headline=a.current_action?'中断前最后记录：'+a.current_action:'执行已中止';
  else if(isDone)headline=a.current_action?'完成前最后记录：'+a.current_action:'执行已完成';
  else headline=a.current_action||'等待下一条动态';

  let html='<div class="live-activity"><p><strong>'+escapeHTML(headline)+'</strong></p><p class="muted">最近有效活动：'+escapeHTML(formatTime(a.last_effective_activity_at))+'</p>';
  if(isRunning&&a.waiting_for_new_events)html+='<p class="muted">自动更新中；没有新事件时保留最近记录。</p>';
  if(children.length)html+='<h3>子任务</h3><ul class="criteria">'+children.map(c=>'<li>'+escapeHTML(c.role)+' · '+escapeHTML(c.short_id)+' · '+escapeHTML(c.status)+'<br>'+escapeHTML(c.last_action)+'</li>').join('')+'</ul>';
  if(isRunning&&a.public_response_preview){
    html+='<h3>正在回复</h3><pre class="raw" data-focus-key="public-response" tabindex="0">'+escapeHTML(a.public_response_preview)+'</pre>'+(a.public_response_truncated?'<p class="muted">显示最近一段公开回复，完整结果返回后可在结果页查看。</p>':'');
  }else if(!isRunning&&!isDone&&a.public_response_preview){
    html+='<h3>中断前已记录的回复片段</h3><pre class="raw" data-focus-key="public-response" tabindex="0">'+escapeHTML(a.public_response_preview)+'</pre>';
  }
  if(events.length){
    html+='<details data-details-key="recent-events"><summary>最近动态 · '+events.length+'</summary><ol class="activity-events">'+events.map(e=>{
      const eKey=String(e.id||(e.action+':'+(e.at||'')));
      const isNew=isSeeded&&!seenEventIds.has(eKey);
      seenEventIds.add(eKey);
      const stateLabel=e.state==='running'?(isRunning?'进行中':isDone?'已记录':'未完成'):(({done:'已完成',error:'工具报告错误',observed:'已观察到'})[e.state]||'已观察到');
      return '<li'+(isNew?' class="is-new"':'')+'>'+escapeHTML(e.action)+' · '+escapeHTML(stateLabel)+(e.at?' · '+escapeHTML(formatTime(e.at,true)):'')+'</li>';
    }).join('')+'</ol></details>';
  }
  seededJobId=card?.job_id;
  return html+'</div>';
}

function progressText(){
  const p=progressObj(),errMsg=errorReason();
  if(complete())return reviewed()?'Gemini 已返回内容':'Gemini 已返回内容，等待 Codex 核对';
  const names={queued:'已提交，等待 Gemini 开始执行',initializing:'正在启动 Gemini',awaiting_response:'已启动，等待 Gemini 返回内容',receiving_response:'正在接收 Gemini 的回复',tool_running:'正在处理所提供的材料',finalizing:'Gemini 已返回，正在整理结果',waiting_permission:'等待权限确认',waiting_input:'等待补充信息',incomplete_run:'运行状态未完成，需核对已有记录',process_missing:'执行进程已结束，最终结果待确认',timeout:'等待超时结束，结果是否完整需核对',failed:'Gemini 返回错误',failed_or_unverified:'执行异常或结果尚未核实',bridge_error:'任务连接出现问题',cancelled:'任务已取消',interrupted:'任务已中断',cancelling:'正在取消任务'};
  if(hasErrorState())return errMsg||names[p.stage]||names[card?.status]||'执行异常，请结合记录核对';
  if(['cancelled','interrupted','cancelling'].includes(card?.status))return names[card.status]||'任务已取消';
  if(card?.status==='queued'||p.stage==='queued')return names.queued;
  let text=(card?.status==='running'&&p.activity?.current_action)||names[p.stage]||names[card?.status]||'等待明确的执行状态';
  if(p.no_recent_stream_activity&&card?.status==='running')text+='；较长时间未收到新事件，尚无法确认远端是否仍在处理，可稍后刷新';
  return text;
}

function pickDefaultTab(){
  const p=progressObj();
  if(['rejected','partially_accepted','revision_requested'].includes(card?.review))return 'review';
  if(hasErrorState()||p.no_recent_stream_activity||p.requires_attention)return 'progress';
  if(complete()||Boolean(response()))return 'delivery';
  return ['queued','running','cancelling'].includes(card?.status)?'progress':'submission';
}

function formatTime(time,short=false){
  if(typeof time!=='number')return '未记录';
  const d=new Date(time*1000);
  return short?d.toLocaleTimeString('zh-CN',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'}):d.toLocaleString('zh-CN',{hour12:false});
}

function syncButtons(){
  for(const name of ['open','inline','refresh','local']){
    const btn=$(name);
    if(!btn)continue;
    btn.disabled=!connected||(name==='refresh'?!card&&!loadError:!card);
    if(busy)btn.setAttribute('aria-disabled','true');
    else btn.removeAttribute('aria-disabled');
  }
}

function renderFailedWithoutCard(){
  const badge=statusBadge();
  $('status').textContent=badge.text;$('status').dataset.tone=badge.tone;
  $('title').textContent='无法读取任务记录';
  $('summary').textContent=loadError+'（只读查看，未调用模型）';
  const ov=mainEl.querySelector('.overview');if(ov)ov.hidden=true;
  document.body.dataset.attention='true';
  if(connected)$('choose').hidden=false;
  syncButtons();size();
}

function receive(result){
  if(result?.structuredContent?.needs_job_id){$('choose').hidden=false;$('title').textContent='查看已有任务';note('输入编号即可查看已有任务。');}
  const meta=readMeta(result);
  const nextCard=result?.structuredContent?.card||meta.panel_card;
  const nextDetail=meta.panel_detail;
  if(nextCard&&card&&nextCard.job_id===card.job_id&&Number.isFinite(nextCard.updated_at)&&Number.isFinite(card.updated_at)&&nextCard.updated_at<card.updated_at)return;
  if(nextCard?.job_id&&card?.job_id!==nextCard.job_id)detail=null;
  if(nextCard){card=nextCard;loadError='';}
  if(nextDetail)detail=nextDetail;
  if(!card){if(loadError)renderFailedWithoutCard();else syncButtons();return;}

  if(document.body.dataset.ready!=='true')document.body.dataset.ready='true';

  const ov=mainEl.querySelector('.overview');if(ov)ov.hidden=false;
  $('choose').hidden=true;

  if(card.job_id&&card.job_id!==lastJobId){
    lastJobId=card.job_id;
    userPickedTab=false;
    proseExpanded=false;
    seenEventIds.clear();
    seededJobId=null;
    note('');
  }
  if(!userPickedTab)tab=pickDefaultTab();

  $('title').textContent=(card.title||'Gemini 任务').replace(/^(?:operator|scout|reader|researcher|analyst|builder|reviewer)\s*[:：]\s*/i,'');
  renderModelHeader(card);
  const summaryText=taskText();
  $('summary').textContent=/^(?:你是|You are\b|上一次 ACP)/i.test(summaryText)?'':clip(summaryText||'任务说明可在任务页查看。',96);

  const badge=statusBadge();
  $('status').textContent=badge.text;$('status').dataset.tone=badge.tone;

  const p=progressObj(),hasText=Boolean(response()),checks=criteria(),files=deliveryFiles();
  const inputCount=Array.isArray(detail?.inputs)?detail.inputs.length:(card.input_count||0);
  const metaParts=[];
  if(inputCount)metaParts.push(inputCount+' 份材料');
  if(checks.length)metaParts.push(checks.length+' 项要求');
  if(p.activity?.last_effective_activity_at)metaParts.push('最近活动 '+formatTime(p.activity.last_effective_activity_at,true));
  else if(card.updated_at)metaParts.push('更新于 '+formatTime(card.updated_at,true));
  if($('meta')){$('meta').textContent=metaParts.join(' · ');$('meta').hidden=!metaParts.length;}

  $('progress-line').textContent=progressText();
  $('progress-line').dataset.tone=hasErrorState()?'error':'';

  $('delivery-line').textContent=files.length?fileSummary(files):hasText?deliverySummary(response()):(hasErrorState()?'未收到有效返回内容':'等待返回');
  $('delivery-line').dataset.empty=String(!hasText&&!files.length);

  $('review-line').textContent=reviewLineText();
  $('review-line').dataset.empty=String(!reviewed()&&!complete()&&!hasText);
  $('review-line').dataset.tone=reviewTone();

  document.body.dataset.attention=String(Boolean(hasErrorState()||p.requires_attention||p.no_recent_stream_activity||['rejected','partially_accepted','revision_requested'].includes(card.review)));

  let url=null;try{const value=new URL(card.task_view_url);if(value.protocol==='http:'&&value.hostname==='127.0.0.1'&&/^\d+$/.test(value.port)&&value.pathname==='/'&&!value.username&&!value.password&&value.searchParams.get('job')===card.job_id)url=value.href;}catch{}
  if(url){$('link').href=url;$('link').hidden=false;$('url').value=url;}else{$('link').removeAttribute('href');$('link').hidden=true;$('url').value='';}

  syncButtons();
  const execution=executionConfiguration();
  const runtime=[['任务编号',card.job_id],[execution.orchestrated?'已报告组合选项':'模型',card.observed_model||'尚未独立记录'],[execution.orchestrated?'请求组合':'请求模型',execution.label],['思考配置',execution.fixed?'系统固定':({low:'低',medium:'中',high:'高',minimal:'最小'})[card.requested_effort]||card.requested_effort],['创建时间',formatTime(card.created_at)],['记录更新',formatTime(card.updated_at)]];
  if(execution.orchestrated)runtime.push(['模型记录范围','所选组合选项；未逐步追踪内部规划、执行阶段及其实际后端模型']);
  if(errorReason())runtime.push(['异常原因',errorReason()]);
  $('runtime').innerHTML=runtime.map(([k,v])=>'<dt>'+escapeHTML(k)+'</dt><dd>'+escapeHTML(v||'未记录')+'</dd>').join('');
  $('raw').textContent=JSON.stringify({state:detail?.state||card,progress:detail?.progress||card?.progress,acceptance:detail?.acceptance,warnings:detail?.warnings},null,2);
  syncViewerVisibility();
  renderTab(false);
}

function renderTab(animateSwitch=false){
  if(!card){size();return;}
  document.body.dataset.activeTab=tab;
  let html='';const data=detail||{},p=progressObj(),text=response(),checks=criteria();
  if(tab==='submission'){
    html=executionConfigurationHTML()+'<h2>交给 Gemini 做什么</h2><div class="prose">'+(taskText()?markdown(taskText()):'<p class="empty">尚未记录任务说明。</p>')+'</div>';
    const inputs=Array.isArray(data.inputs)?data.inputs:[];
    html+='<h2>提供的材料</h2>'+(inputs.length?'<ul class="criteria">'+inputs.map(v=>'<li>'+escapeHTML(v.name||'附件')+'</li>').join('')+'</ul>':'<p class="empty">'+(card?.input_count?'共 '+escapeHTML(card.input_count)+' 份材料，具体文件名尚未读取。':'没有单独的附件。')+'</p>');
    html+=workspaceHTML();
    if(checks.length)html+='<h2>任务要求</h2><ul class="criteria">'+checks.map(v=>'<li>'+escapeHTML(v)+'</li>').join('')+'</ul>';
  }else if(tab==='progress'){
    const done=complete()||Boolean(text),err=hasErrorState(),isPendingReview=done&&!reviewed();
    const step2Sub=err?(p.activity?.current_action?'中断前动作：'+p.activity.current_action+' · 请核对下方记录':'请结合下方运行记录确认异常原因。'):p.no_recent_stream_activity&&card?.status==='running'?'暂未收到新动态，还无法确认 Gemini 是否仍在处理。':done?'已有返回记录，可在「结果」页查看。':'根据已收到的动态更新。';
    html='<h2>目前进展</h2><ol class="steps"><li class="done">任务已提交<span>'+escapeHTML(formatTime(card?.created_at))+'</span></li><li class="'+(err?'error':done?'done':'current')+'">'+escapeHTML(progressText())+'<span>'+escapeHTML(step2Sub)+'</span></li><li class="'+(done?'done':err?'error':'')+'">'+(text?'已收到返回内容':err?'未收到完整结果':'等待返回结果')+'</li><li class="'+(reviewTone()||(isPendingReview?'pending':''))+'">'+escapeHTML(reviewVerdict())+'</li></ol>';
    html+=activityHTML();
    if(p.requires_attention||err){
      const errMsg=errorReason();
      html+='<p class="callout error">'+(errMsg?'异常说明：'+escapeHTML(errMsg)+'。':'')+'此任务状态需核对，请结合已有结果或下方原始记录确认。</p>';
    }
  }else if(tab==='delivery'){
    html+=fileHTML();
    if(text&&!reviewed())html+='<p class="callout pending">Gemini 已交回结果，等待 Codex 对照任务要求核对。</p>';
    else if(text&&reviewed())html+='<p class="callout '+reviewTone()+'">'+escapeHTML(REVIEW_LABELS[card.review])+' · 采用内容与后续处理见「检查」。</p>';
    const isLong=text.length>480||text.split('\n').length>10;
    html+='<h2>Gemini 结果</h2>'+(text?'<div class="prose'+(isLong&&!proseExpanded?' is-clamped':'')+'">'+deliveryHTML(text)+'</div>'+(isLong?'<button type="button" class="quiet prose-toggle" data-prose-toggle>'+(proseExpanded?'收起长文 ↑':'展开全部内容 ↓')+'</button>':''):deliveryFiles().length?'<p class="muted">文件已保存，本次没有附加文字说明。</p>':'<p class="empty">尚未返回结果。当前状态：'+escapeHTML(progressText())+'。</p>');
    if(card?.response_truncated&&!data.report?.response)html+='<p class="muted">当前为部分结果预览，完整内容可在下方浏览器记录链接中查看。</p>';
    if(text)html+='<details data-details-key="raw-response"><summary>查看 Gemini 返回原文</summary><pre class="raw" tabindex="0">'+escapeHTML(text)+'</pre></details>';
    const images=data.generated_images||[];
    if(images.length)html+='<h2>生成的图片</h2><p>'+images.length+' 张图片已保存，可在完整记录中查看和下载。</p>';
  }else{
    const reviewNote=reviewed()?(data.acceptance?.note||card?.review_note):'';
    const tone=reviewTone();
    html='<h2>Codex 检查结论</h2><p class="review-result"'+(tone?' data-tone="'+tone+'"':'')+'>'+escapeHTML(reviewVerdict())+'</p>'+(reviewNote?'<div class="prose">'+markdown(reviewNote)+'</div>':'<p class="empty">'+(reviewed()?'已记录检查结论，但未附具体核对说明。':'尚未保存检查结论；Gemini 返回结果后，由 Codex 对照下方要求核对。')+'</p>');
    const review=data.acceptance||{};
    for(const [label,items] of [['已采纳的内容',review.accepted_items||card?.accepted_items],['还需处理',review.issues||card?.review_issues],['修订说明',review.changes||card?.review_changes]]){
      if(Array.isArray(items)&&items.length)html+='<h2>'+label+'</h2><ul class="criteria">'+items.map(item=>'<li>'+escapeHTML(item)+'</li>').join('')+'</ul>';
    }
    const history=review.history||card?.review_history||[];
    if(history.length>1)html+='<details data-details-key="review-history"><summary>检查记录 · '+history.length+' 次</summary>'+history.map(entry=>'<h3>'+escapeHTML(REVIEW_LABELS[entry.verdict]||'已记录')+' · '+escapeHTML(formatTime(entry.reviewed_at))+'</h3><p>'+escapeHTML(entry.note)+'</p>').join('')+'</details>';
    html+=executionConfigurationHTML();
    if(executionConfiguration().orchestrated)html+='<p class="muted">模型记录表示所选组合选项，未逐步追踪内部规划、执行阶段及其实际后端模型。</p>';
    if(checks.length)html+='<h2>检查依据</h2><p class="muted">'+(reviewed()?'以下是任务开始时约定的要求，具体核对结论见上方说明。':'待 Codex 对照核对的任务要求。')+'</p><ul class="criteria">'+checks.map(v=>'<li>'+escapeHTML(v)+'</li>').join('')+'</ul>';
    const date=reviewed()?(data.acceptance?.reviewed_at||card?.reviewed_at):null;
    if(date)html+='<p class="muted">检查记录于 '+escapeHTML(formatTime(date))+'</p>';
  }
  if(renderedContent!==html){const previous=captureViewState($('content'));$('content').innerHTML=html;renderedContent=html;restoreViewState($('content'),previous);}
  if(animateSwitch){
    const panel=$('content');
    delete panel.dataset.switching;
    void panel.offsetWidth;
    panel.dataset.switching='true';
    clearTimeout(switchTimer);
    switchTimer=setTimeout(()=>{delete panel.dataset.switching;},160);
  }
  for(const b of mainEl.querySelectorAll('[data-tab]')){b.setAttribute('aria-selected',String(b.dataset.tab===tab));b.tabIndex=b.dataset.tab===tab?0:-1;}
  $('content').setAttribute('aria-labelledby','tab-'+tab);
  syncTabSlider(animateSwitch);
  size();
}

async function display(target){
  if(!connected)return;
  const reqId=++displaySeq;
  if(target==='inline')localExpanded=false;
  busy=true;syncButtons();note(target==='fullscreen'?'正在打开详情…':'正在收起详情…');
  try{
    let result;
    if(typeof window.openai?.requestDisplayMode==='function')result=await window.openai.requestDisplayMode({mode:target});
    else result=await request('ui/request-display-mode',{mode:target});
    if(reqId!==displaySeq)return;
    const returned=typeof result==='string'?result:result?.mode;
    applyMode(returned);
    if(mode!==target&&!await waitForMode(target)){
      if(reqId!==displaySeq)return;
      if(target==='fullscreen'){localExpanded=true;syncViewerVisibility();renderTab(false);note('宿主未切换右侧详情，已在卡片内展开。');}
      else note('当前宿主保持右侧详情，请用面板的关闭按钮收起。');
      return;
    }
    note('');
  }catch(error){
    if(reqId!==displaySeq)return;
    if(target==='fullscreen'){localExpanded=true;syncViewerVisibility();renderTab(false);note('右侧详情暂时无法打开（'+error.message+'），已在卡片内展开预览。');}
    else note(error.message,'error');
  }finally{
    if(reqId===displaySeq){busy=false;syncButtons();}
  }
}

async function refreshJob({silent=false}={}){
  const targetJob=card?.job_id||$('choose-job').value.trim();
  if(!connected||!targetJob||(busy||polling))return;
  if(silent)polling=true;
  else{busy=true;$('refresh').textContent='刷新中…';syncButtons();}
  try{
    const result=await request('tools/call',{name:'bridge_status',arguments:{job_id:targetJob}});
    if(result?.isError)throw new Error('任务记录读取失败。');
    if(!readMeta(result).panel_card)throw new Error('完整记录暂时无法读取，已保留原显示。');
    if(card?.job_id&&card.job_id!==targetJob)return;
    loadError='';receive(result);
    if(!silent)note('已更新 '+formatTime(Math.floor(Date.now()/1000),true));
  }catch(error){
    loadError=error.message||'任务记录读取失败。';
    document.body.dataset.attention='true';
    if(!card)renderFailedWithoutCard();
    else if(!silent)note('刷新未成功，仍显示上次记录：'+loadError,'error');
  }finally{
    if(silent)polling=false;
    else{busy=false;$('refresh').textContent='刷新';syncButtons();}
  }
}

$('open').addEventListener('click',()=>display('fullscreen'));
$('inline').addEventListener('click',()=>display('inline'));
$('local').addEventListener('click',()=>{if(!connected||busy)return;localExpanded=!localExpanded;syncViewerVisibility();renderTab(false);});
$('refresh').addEventListener('click',()=>refreshJob({silent:false}));
for(const button of mainEl.querySelectorAll('[data-tab]'))button.addEventListener('click',()=>{const changed=tab!==button.dataset.tab;userPickedTab=true;tab=button.dataset.tab;renderTab(changed);});
$('content').addEventListener('click',async event=>{
  const toggle=event.target.closest('[data-prose-toggle]');
  if(toggle){proseExpanded=!proseExpanded;renderTab(false);return;}
  const link=event.target.closest('[data-delivery-link]');
  if(!link)return;
  event.preventDefault();
  const file=deliveryFiles().find(file=>deliveryURL(file)===link.href);
  const url=file&&deliveryURL(file);
  if(!url)return;
  if(!connected){note('请通过完整记录中的文件链接查看。');return;}
  try{const result=await request('ui/open-link',{url});if(result?.isError)throw new Error();}catch{note('宿主未能打开文件，请使用完整记录链接查看或下载。','error');}
});
$('url').addEventListener('click',()=>$('url').select());

window.addEventListener('message',event=>{
  if(event.source!==parent||event.data?.jsonrpc!=='2.0')return;
  const message=event.data;
  if(message.id!==undefined&&pending.has(message.id)&&!message.method){const p=pending.get(message.id);clearTimeout(p.timer);pending.delete(message.id);if(message.error)p.reject(new Error(message.error.message||'当前客户端暂不支持此操作。'));else p.resolve(message.result);return;}
  if(message.method==='ui/notifications/tool-result')receive(message.params);
  if(message.method==='ui/notifications/host-context-changed')applyHostContext(message.params);
  if(message.method==='ping'&&message.id!==undefined)parent.postMessage({jsonrpc:'2.0',id:message.id,result:{}},'*');
  if(message.method==='ui/resource-teardown'){connected=false;resizeObserver.disconnect();clearInterval(pollTimer);for(const entry of modeWaiters){clearTimeout(entry.timer);entry.resolve(false);}modeWaiters.clear();for(const p of pending.values()){clearTimeout(p.timer);p.reject(new Error('视图已关闭'));}pending.clear();if(message.id!==undefined)parent.postMessage({jsonrpc:'2.0',id:message.id,result:{}},'*');}
});

if(parent!==window)request('ui/initialize',{appInfo:{name:'Gemini Bridge task details',version:'0.2.0'},appCapabilities:{availableDisplayModes:['inline','fullscreen']},protocolVersion:'2026-01-26'}).then(result=>{
  if(result.protocolVersion!=='2026-01-26')throw new Error('客户端协议不兼容');
  connected=true;applyHostContext(result.hostContext);
  syncButtons();
  parent.postMessage({jsonrpc:'2.0',method:'ui/notifications/initialized',params:{}},'*');
  receive(window.openai?.toolResponseMetadata);
  if(window.openai?.toolOutput)receive({structuredContent:window.openai.toolOutput});
}).catch(error=>{loadError=error.message||'初始化连接失败。';renderFailedWithoutCard();note(loadError,'error');});

window.addEventListener('openai:set_globals',event=>{const globals=event.detail?.globals;if(globals?.toolResponseMetadata)receive(globals.toolResponseMetadata);if(globals?.toolOutput)receive({structuredContent:globals.toolOutput});if(globals?.displayMode)applyMode(globals.displayMode);if(globals?.theme)applyHostContext({theme:globals.theme});});

$('choose').addEventListener('submit',async event=>{
  event.preventDefault();const job=$('choose-job').value.trim();
  if(!/^[A-Za-z0-9-]{1,100}$/.test(job)||!connected||busy)return;
  busy=true;syncButtons();
  try{
    const result=await request('tools/call',{name:'bridge_status',arguments:{job_id:job}});
    if(result?.isError)throw new Error('未能读取该任务记录，请核对编号。');
    if(!readMeta(result).panel_card)throw new Error('完整记录暂时无法读取。');
    loadError='';receive(result);note('已打开任务记录。');
  }catch(error){
    loadError=error.message;if(!card)renderFailedWithoutCard();note(loadError,'error');
  }finally{busy=false;syncButtons();}
});

mainEl.querySelector('.tabs').addEventListener('keydown',event=>{
  if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
  event.preventDefault();userPickedTab=true;
  const buttons=[...mainEl.querySelectorAll('[data-tab]')];
  let index=buttons.findIndex(b=>b.dataset.tab===tab);
  index=event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length;
  const nextTab=buttons[index].dataset.tab;
  const changed=tab!==nextTab;
  tab=nextTab;renderTab(changed);buttons[index].focus();
});

// 静默轮询：不触发按钮禁用闪烁，不夺取键盘焦点
const resizeObserver=new ResizeObserver(()=>{syncTabSlider(false);size();});
resizeObserver.observe(mainEl);
function needsLiveRefresh(){return card&&(['queued','running','cancelling'].includes(card.status)||(complete()&&!['accepted','accepted_with_changes','rejected'].includes(card.review)));}
let lastPollAt=0;
const pollTimer=setInterval(()=>{const now=Date.now(),delay=document.hidden||complete()?15000:2000;if(connected&&needsLiveRefresh()&&!busy&&!polling&&now-lastPollAt>=delay){lastPollAt=now;refreshJob({silent:true});}},2000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&needsLiveRefresh())refreshJob({silent:true});});
