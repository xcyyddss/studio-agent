'use strict';
let token='',current=null,selectionGeneration=0,displayed=null,activeRefresh=null,listGeneration=0,errorGeneration=0;
const presented=new Set(),drafts=new Map(),snapshots=new Map();let questionEditors=new Map();
const $=id=>document.getElementById(id);
const names={superseded:'已由新调用替代',sleeping:'休眠',inspection_only:'只读检查点',active:'推进中',delivered:'已交付',needs_attention:'需要处理',leased:'准备调用',result_pending:'返回待处理',ready:'等待运行',running:'运行中',paused:'已暂停',waiting:'等待信息',blocked:'需要处理',quiescent:'已静止',completed:'已交付',stalled:'暂时停滞',succeeded:'已返回',returned:'已返回',pending:'待处理',uncertain:'执行结果不明',failed:'执行失败',applied:'已处理',acknowledged:'已处理',acked:'已登记',consulting:'议会讨论中',returned:'已回传',cancelled:'已取消'};
const reasons={'scoped dependencies remain; no fresh event requires another model call':'正在稳定等待已有依赖，新信息到达后会继续。','durable role jobs or semantic duties are pending':'仍有角色调用或待处理的认知工作，系统正在推进。','a preserved role result requires explicit retry or correction':'一份已保存的返回需要重试或修正，请查看执行记录。','specific unanswered questions remain; no answer is simulated':'正在等待具体问题的回答。','explicit external tool requests await adapter-provided results':'等待外部操作的真实结果。','no runnable intent; no parent-authored delivery or quiescence is established':'当前没有可执行工作，也尚未形成交付或静止判断，需要处理。','parent requested continue without executable work; explicit next action required':'父议题希望继续，但没有给出可执行的下一步，需要处理。'};
Object.assign(names,{quarantined:'已隔离，可重试',degraded:'部分批次待恢复',operational:'操作进展'});
function workText(t,s){
  const parts=[];
  if(t.phase?.name)parts.push('当前阶段：'+t.phase.name,'交付：'+t.phase.deliverable,'验收：'+t.phase.acceptance);
  const knowledge=array(s.working_knowledge?.[t.id]).filter(x=>x.status!=='retired');
  if(knowledge.length)parts.push('当前保留认识：'+knowledge.length+' 项（含条件或争议，完整版本见状态快照）');
  for(const delta of array(t.last_delta))if(delta.source_job)parts.push('最近登记 '+delta.source_job+'：认识变化 '+array(delta.knowledge_changes).length+' 项，义务变更 '+array(delta.obligation_changes).length+' 项，处理输入 '+array(delta.input_ids).length+' 项');
  if(array(t.wait_dependencies).length)parts.push('恢复条件：'+array(t.wait_dependencies).map(x=>x.id+'（'+(names[x.state]||x.state)+'）').join('；')+' 的状态发生变化');
  return parts.length?'\n'+parts.join('\n'):'';
}
function title(x){const s=String(x||'').split('\n')[0];return s.length>76?s.slice(0,76)+'…':s;}
function text(x){return typeof x==='string'?x:JSON.stringify(x,null,2)}
function elem(tag,value,cls){const e=document.createElement(tag);if(value!==undefined)e.textContent=text(value);if(cls)e.className=cls;return e;}
function err(e){const generation=++errorGeneration;$('error').textContent=e.message||String(e);$('error').hidden=false;setTimeout(()=>{if(generation===errorGeneration)$('error').hidden=true},15000)}
async function api(path,body){const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json','X-Studio-Token':token},body:body===undefined?undefined:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.error||r.statusText);return d;}
async function list(){const generation=++listGeneration;const d=await api('/api/runs');if(generation!==listGeneration)return d.runs;$('runs').replaceChildren();for(const r of d.runs){const b=elem('button',undefined,r.id===current?'selected':'');b.append(elem('div',title(r.goal||r.id)),elem('small',(r.checkpoint_read_only?'只读检查点 · ':'')+r.id));b.onclick=()=>select(r.id);$('runs').append(b)}return d.runs;}
function feedbackText(topic){const batch=topic.feedback_wait;if(!batch)return '';const labels={pending:'等待回传',ready:'回传齐备',blocked:'受阻',consumed:'已承接',superseded:'版本已替代'};const parts=array(batch.waves).flatMap(w=>array(w.participants).filter(p=>!['consumed','superseded'].includes(p.status)).map(p=>p.topic+'：'+(labels[p.status]||p.status)));return parts.length?'\n反馈批次：'+parts.join('；'):''}
function item(title,body,meta){const d=elem('div');d.append(elem('div',title,'item-title'));if(body)d.append(elem('div',body,'item-body'));if(meta)d.append(elem('div',meta,'item-meta'));return d;}
function array(v){return Array.isArray(v)?v:v&&typeof v==='object'?Object.entries(v).map(([id,x])=>typeof x==='object'?{id,...x}:{id,value:x}):[];}
function submissionId(draft,values){const signature=JSON.stringify(values);if(draft.submission?.signature!==signature)draft.submission={signature,id:crypto.randomUUID()};return draft.submission.id;}
const selected=scope=>!!scope&&scope.id===current&&scope.generation===selectionGeneration;
const shown=scope=>selected(scope)&&displayed?.id===scope.id&&displayed?.generation===scope.generation;
function runDraft(id){if(!drafts.has(id))drafts.set(id,{input:{values:{text:'',correction:false,topic_id:'ROOT',profile_scope:'collaboration'},version:0,pending:false},questions:new Map()});return drafts.get(id)}
function questionDraft(id,qid){const all=runDraft(id).questions;if(!all.has(qid))all.set(qid,{values:{text:'',profile_scope:'collaboration'},version:0,pending:false});return all.get(qid)}
function storeDraft(draft,values,edited=false){if(edited||JSON.stringify(values)!==JSON.stringify(draft.values))draft.version++;draft.values=values}
function rememberInput(edited=false){if(!displayed)return;storeDraft(runDraft(displayed.id).input,{text:$('input-text').value,correction:$('correction').checked,topic_id:$('input-topic').value,profile_scope:$('profile-scope').value},edited)}
function rememberDrafts(){if(!displayed)return;rememberInput();for(const [qid,e] of questionEditors)storeDraft(questionDraft(displayed.id,qid),{text:e.a.value,profile_scope:e.scope.value})}
function controls(disabled){for(const control of [$('continue'),$('pause'),$('checkpoint'),$('fork'),...$('input').querySelectorAll('input,textarea,select,button'),...$('scheduling').querySelectorAll('input,button')])control.disabled=disabled}
function restoreInput(){const draft=runDraft(displayed.id).input,v=draft.values;setValue($('input-text'),v.text);$('correction').checked=v.correction;setValue($('input-topic'),v.topic_id);setValue($('profile-scope'),v.profile_scope);$('input').querySelector('button').disabled=displayed.readOnly||draft.pending}
function setValue(control,value){if(control.value!==value)control.value=value}
// Keep native controls (focus, caret, IME and file selection) alive between polls.
function syncOptions(select,entries){
  const wanted=new Set(entries.map(([value])=>value));
  for(const option of [...select.children])if(!wanted.has(option.value))option.remove();
  const existing=new Map([...select.children].map(option=>[option.value,option]));
  for(const [value,label] of entries){let option=existing.get(value);if(!option){option=elem('option');option.value=value;select.append(option)}if(option.textContent!==label)option.textContent=label}
}
function select(id){rememberDrafts();if(displayed)runDraft(displayed.id).input.composing=false;current=id;selectionGeneration++;displayed=null;questionEditors=new Map();controls(true);$('welcome').hidden=true;$('workspace').hidden=true;$('title').textContent='正在载入 '+id;$('error').hidden=true;const scope={id,generation:selectionGeneration};list().catch(e=>{if(selected(scope))err(e)});return refresh()}
function refresh(){
  if(!current)return Promise.resolve();
  const scope={id:current,generation:selectionGeneration};
  if(activeRefresh&&selected(activeRefresh.scope)){activeRefresh.queued=true;return activeRefresh.promise}
  const task={scope,queued:false};activeRefresh=task;
  task.promise=(async()=>{do{task.queued=false;try{const d=await api('/api/runs/'+encodeURIComponent(scope.id));if(!selected(scope))break;if(d.id&&d.id!==scope.id)throw Error('运行状态身份不匹配');render(d,scope)}catch(e){if(selected(scope))err(e)}}while(task.queued&&selected(scope))})().finally(()=>{if(activeRefresh===task)activeRefresh=null});
  return task.promise;
}
function render(d,scope){if(!selected(scope))return;const firstView=!shown(scope);rememberDrafts();displayed={...scope,readOnly:!!d.checkpoint_read_only};const run=scope.id;if(firstView)runDraft(run).input.composing=false;const s=d.workflow||d;const q=d.queue||{};const jobs=array(d.jobs||q.jobs);controls(!!d.checkpoint_read_only);$('fork').disabled=false;restoreInput();$('title').textContent=title(s.goal||run);$('goal-detail').textContent=s.goal||run;$('state').textContent=names[d.state||s.state||s.status]||d.state||s.state||s.status||'已创建';$('activity').textContent=d.checkpoint_read_only?'只读恢复副本，不运行模型':d.runner_active?'最近有调度活动':jobs.some(j=>['running','leased'].includes(j.status||j.state))?'调度器未活动，已有调用仍在运行':'当前没有在途调用';$('raw').textContent=JSON.stringify(d,null,2);
if(typeof renderSandbox==='function')renderSandbox(d,scope);
const setting=runDraft(run).scheduling||(runDraft(run).scheduling={dirty:false,pending:false,value:6});
if(!setting.dirty)setting.value=d.scheduling?.max_workers||6;if(firstView||!setting.dirty)setValue($('workers'),String(setting.value));
$('scheduling').querySelector('button').disabled=!!d.checkpoint_read_only||setting.pending;
$('workers-status').textContent='已保存上限 '+(d.scheduling?.max_workers||6)+' · 只影响本目标后续派发，不中断进行中的调用。';
const topics=array(s.topics),questions=array(s.questions).filter(x=>!['answered','resolved','cancelled','proposed','linked'].includes(x.status||x.state)),experience=array(s.experience||s.learning||s.learned_materials);
$('metrics').replaceChildren(...[[topics.length,'独立议题'],[jobs.filter(x=>['running','leased','dispatched'].includes(x.status||x.state)).length,'进行中的调用'],[questions.length,'待答问题'],[d.checkpoint_read_only?'—':s.experience_count??s.experience?.learning_count??experience.length,d.checkpoint_read_only?'经验未在摘要统计':'经验条目']].map(([n,l])=>{const e=elem('div',undefined,'metric');e.append(elem('strong',n),elem('span',l));return e}));
$('topic-count').textContent=topics.length+' 个';$('topics').replaceChildren(...topics.map(t=>item(title(t.q||t.question||t.title||t.id),(t.summary||t.ans||t.answer||t.reason||'')+workText(t,s)+feedbackText(t),`${t.id||''} · ${names[t.status||t.state]||t.status||t.state||'处理中'}`)));if(!topics.length)$('topics').append(elem('p','正在准备议会输入。','muted'));
const deliveries=array(s.deliveries||s.delivery||s.results);$('deliveries').replaceChildren(...deliveries.map(x=>item(x.title||x.id||'交付',x.summary||x.ans||x.content||x,(x.state==='partial'?'阶段交付 · 仍有依赖等待':x.state==='delivered'?'正式交付':x.state==='quiescent'?'静止':x.status||x.state||''))));if(!deliveries.length)$('deliveries').append(elem('p','尚未形成交付，生成的内容仍在加工中。','muted'));
const published=new Set(deliveries.flatMap(x=>x.paths||[]).map(p=>p.replace(/^artifacts\//,'')));const fileLink=f=>{const a=elem('a',f.path);a.href='/api/runs/'+encodeURIComponent(run)+'/artifact?path='+encodeURIComponent(f.path);a.target='_blank';a.rel='noopener';return a};const allFiles=array(d.artifacts),currentFiles=allFiles.filter(f=>published.has(f.path)),historyFiles=allFiles.filter(f=>!published.has(f.path));$('files').replaceChildren(...currentFiles.map(fileLink));if(historyFiles.length){const history=elem('details');history.append(elem('summary','过程产物与历史版本（'+historyFiles.length+'）'),...historyFiles.map(fileLink));$('files').append(history)}
renderQuestions(questions,scope,!!d.checkpoint_read_only);
const receiptNames={preparing:'正在整理与复核上下文',received:'已保存，等待处理',processing:'议题正在处理',failed:'处理失败，回答已保存',followup:'已承接，仍有后续工作',processed:'已明确处理'};
const receipts=$('input-receipts');if(receipts)receipts.replaceChildren(...array(s.input_status).map(x=>{const q=array(s.questions).find(q=>q.id===x.question_id);const e=item(x.topic_id+' · '+(receiptNames[x.state]||x.state), (q?'原问题：'+(q.text||q.question||q.prompt||q.id)+'\n':'')+(['coordination','internal_response'].includes(x.kind)?'议题交接：':'你的回答：')+x.text,x.effect||'尚未形成处理反馈');for(const j of array(x.failed_jobs)){e.append(elem('p',text(j.error)));if(!d.checkpoint_read_only){const b=elem('button','重试处理');b.onclick=()=>act('retry',{job_id:j.job_id,reason:'重试已保存回答的处理'},scope);e.append(b)}}return e}));
const recovery=$('recovery');if(recovery){const rows=array(s.diagnostics).filter(x=>x.status==='blocked');recovery.replaceChildren(...rows.map(x=>item(x.stage||x.kind,x.error,'原始材料和已提交回答仍保留。')));const m=s.context_memory;if(m){const counts={};for(const u of array(m.units))counts[u.state]=(counts[u.state]||0)+1;recovery.append(item('上下文记忆',text(counts),'按完整输入容量触发；无收益保留原文，摘要须独立覆盖复核。'));const calls={};for(const j of jobs){const role=j.kind||j.role||'unknown';calls[role]=(calls[role]||0)+1}recovery.append(item('调用用途统计',text(calls),'调用数量不等于议题数量；执行重试次数见下方记录。'));if(m.policy)recovery.append(item('上下文容量',Number(m.policy.model_context_tokens).toLocaleString()+' token · CLI 显式请求窗口','预检另计输出、平台、图像余量，并检查 CLI 单次输入上限 1,048,576 字符；先做可回查的整条目投影，必需内容装不下才整理。'))}if(rows.length&&!d.checkpoint_read_only){const b=elem('button','重新检查并恢复调度');b.onclick=()=>act('recover',{},scope);recovery.append(b)}if(s.automatic_retries_pending)recovery.append(elem('p','网络调用失败后正在退避，稍后自动重试。'))}

if(!d.checkpoint_read_only&&!document.hidden){for(const x of questions){const key=run+'/'+x.id;if(!presented.has(key)){presented.add(key);api('/api/runs/'+encodeURIComponent(run)+'/presented',{question_id:x.id}).catch(e=>{presented.delete(key);if(shown(scope))err(e)});}}}
if(typeof renderAttachments==='function')renderAttachments(d,displayed);
const webResults=$('web-results');if(webResults)webResults.replaceChildren(...array(s.user_inputs).filter(x=>x.kind==='tool_result'&&x.value?.receipt?.adapter==='codex_native_web').map(x=>{const p=x.value.payload||{};const e=item(x.topic_id+' · '+x.id,p.summary||x.value.error||'未取得结果',p.limitations||'原生记录与模型归纳分别保存；来源可达不代表事实已验证。');for(const source of array(p.sources)){const a=elem('a',source.title||source.url);try{const u=new URL(source.url);if(!['https:','http:'].includes(u.protocol))continue;a.href=u.href}catch{continue}a.target='_blank';a.rel='noopener noreferrer';e.append(a,elem('small',(source.captured_at||'')+' · '+source.provenance),elem('p',source.findings||''))}return e}));
const user=s.user_collaboration;$('user-state').textContent=user?'已处理 '+user.review_count+' 次协作观察 · '+user.pending_count+' 条待处理 · '+array(user.quarantined_assets).length+' 项提议隔离 · '+(names[user.state]||user.state):'此停点未初始化用户协作职责';$('user-assets').replaceChildren(...array(s.user_assets).filter(x=>x.status!=='retracted').map(x=>item(x.kind+' · '+x.id,x.content,'条件：'+x.conditions+' | '+x.basis+' | '+x.status)));$('maintenance').textContent=JSON.stringify({diagnostics:s.diagnostics||[],memory:s.memory||{},tools:s.tool_requests||[],web_research:s.web_research||[],question_coordination:array(s.questions).filter(q=>['proposed','linked','cancelled','resolved'].includes(q.state)),question_handoffs:s.question_handoffs||[],user_material_deliveries:s.user_material_deliveries||[],internal_dependencies:s.internal_requests||[],user_recovery:s.user_collaboration?.quarantined_batches||[]},null,2);
const table=elem('table'),head=elem('tr');['调用 / 职责','状态','尝试','操作'].forEach(x=>head.append(elem('th',x)));table.append(head);for(const j of jobs){const tr=elem('tr');tr.append(elem('td',(j.job_id||j.id)+'\n'+(j.kind||j.role||'')),elem('td',(names[j.status]||j.status||'')+(j.semantic_error?'\n'+text(j.semantic_error):'')),elem('td',j.attempt_count??j.attempts??''));const td=elem('td');if(!d.checkpoint_read_only&&['failed','uncertain','quarantined'].includes(j.status)){const b=elem('button','查看并重试','small-action');b.onclick=()=>{if(shown(scope)&&confirm('原调用的记录会保留；结果不明时再次调用可能重复消耗。确认重新尝试？'))act('retry',{job_id:j.job_id||j.id,reason:'用户在工作台确认重试',allow_uncertain:j.status==='uncertain'},scope);};td.append(b)}tr.append(td);table.append(tr)}$('jobs').replaceChildren(table);
const notice=d.error||s.blocked_reason||s.state_reason||s.reason||'';$('notice').hidden=!notice;$('notice').textContent=reasons[notice]||text(notice);
$('workspace').hidden=false;
}
function createQuestionEditor(x,scope){
  const run=scope.id,root=elem('div'),info=elem('div');root.append(info);
  const form=elem('form',undefined,'question-form'),a=elem('textarea'),b=elem('button','提交回答'),permission=elem('select');a.required=true;
  for(const [v,t] of [['collaboration','可用于本任务的协作建议'],['profile','仅供用户协作议题理解'],['none','不用于画像与引导学习']]){const o=elem('option',t);o.value=v;permission.append(o)}
  const draft=questionDraft(run,x.id);draft.composing=false;a.value=draft.values.text;permission.value=draft.values.profile_scope;
  const live=()=>shown(scope)&&questionEditors.get(x.id)?.a===a;
  const remember=edited=>{if(live())storeDraft(draft,{text:a.value,profile_scope:permission.value},edited)};
  a.oninput=()=>remember(true);permission.onchange=()=>remember(true);
  a.addEventListener('compositionstart',()=>{if(live()){draft.composing=true;remember(true)}});
  a.addEventListener('compositionend',()=>{draft.composing=false;remember(true)});
  form.append(a,permission,b);
  form.onsubmit=async ev=>{
    ev.preventDefault();if(!live()||displayed.readOnly||draft.pending||draft.composing)return;remember(false);
    const version=draft.version,values={...draft.values};draft.pending=true;b.disabled=true;
    let ok=false,attachment={};
    try{attachment=typeof attachmentRequest==='function'?await attachmentRequest(run,x.id,values):{};if(shown(scope))ok=await act('input',{...values,question_id:x.id,submission_id:submissionId(draft,values),...attachment},scope)}catch(e){if(shown(scope))err(e)}
    if(ok)draft.submission=null;
    if(ok&&typeof acknowledgeAttachment==='function')acknowledgeAttachment(run,x.id,attachment.submission_id);
    if(displayed?.id===run){const editor=questionEditors.get(x.id);if(editor)storeDraft(draft,{text:editor.a.value,profile_scope:editor.scope.value})}
    if(ok&&draft.version===version){draft.values={...draft.values,text:''};draft.version++;if(displayed?.id===run){const editor=questionEditors.get(x.id);if(editor)editor.a.value=''}}
    draft.pending=false;if(displayed?.id===run){const editor=questionEditors.get(x.id);if(editor){editor.b.disabled=displayed.readOnly;editor.picker?.update()}}
  };
  return {root,info,form,a,scope:permission,b};
}
function renderQuestions(questions,scope,readOnly){
  const container=$('questions'),wanted=new Set(questions.map(x=>x.id));
  for(const [id,editor] of questionEditors)if(!wanted.has(id)){editor.root.remove();questionEditors.delete(id)}
  for(const x of questions){
    let editor=questionEditors.get(x.id);
    if(!editor){editor=createQuestionEditor(x,scope);questionEditors.set(x.id,editor)}
    editor.info.replaceChildren(item(x.question||x.text||x.prompt||x.id,x.purpose||x.why||x.reason||'',x.topic||x.topic_id||''));
    if(typeof fileLinkPair==='function')for(const file of array(x.attachments))editor.info.append(fileLinkPair(scope.id,file));
    editor.b.disabled=readOnly||questionDraft(scope.id,x.id).pending;
    if(readOnly)editor.form.remove();else if(editor.form.parentElement!==editor.root)editor.root.append(editor.form);
    // Existing cards keep their position; moving a focused ancestor also drops focus.
    if(editor.root.parentElement!==container)container.append(editor.root);
  }
  const roots=new Set([...questionEditors.values()].map(editor=>editor.root));
  for(const child of [...container.children])if(!roots.has(child))child.remove();
  if(!questions.length)container.append(elem('p','目前没有待答问题。','muted'));
}
async function act(action,data={},scope=displayed){
  if(!shown(scope)||displayed.readOnly)return false;
  try{await api('/api/runs/'+encodeURIComponent(scope.id)+'/'+action,data);if(selected(scope))refresh();return true}
  catch(e){if(shown(scope))err(e);return false}
}
$('new').onclick=()=>{rememberDrafts();selectionGeneration++;current=null;displayed=null;questionEditors=new Map();controls(true);$('welcome').hidden=false;$('workspace').hidden=true;$('title').textContent='让一个目标开始流动';$('error').hidden=true;$('goal').focus();list().catch(e=>{if(!current)err(e)})};
$('create').onsubmit=async ev=>{ev.preventDefault();const b=ev.target.querySelector('button'),generation=selectionGeneration;b.disabled=true;try{const r=await api('/api/runs',{goal:$('goal').value,start:true,sandbox_tests:$('sandbox-tests').checked,web_search:$('web-search')?.value||'live',max_workers:Number($('create-workers').value)});if(generation===selectionGeneration)await select(r.id);else list().catch(()=>{})}catch(e){if(generation===selectionGeneration)err(e)}finally{b.disabled=false}};
for(const id of ['input-text','correction','input-topic','profile-scope']){const control=$(id);control.oninput=()=>rememberInput(true);control.onchange=()=>rememberInput(true)}
$('input-text').addEventListener('compositionstart',()=>{if(displayed){runDraft(displayed.id).input.composing=true;rememberInput(true)}});
$('input-text').addEventListener('compositionend',()=>{if(displayed){runDraft(displayed.id).input.composing=false;rememberInput(true)}});
$('input').onsubmit=async ev=>{
  ev.preventDefault();const scope=displayed;if(!shown(scope)||scope.readOnly)return;
  rememberInput();const draft=runDraft(scope.id).input;if(draft.pending||draft.composing)return;
  const version=draft.version,values={...draft.values};draft.pending=true;$('input').querySelector('button').disabled=true;
  const ok=await act('input',{...values,submission_id:submissionId(draft,values)},scope);
  if(ok)draft.submission=null;
  if(displayed?.id===scope.id)rememberInput();
  if(ok&&draft.version===version){draft.values={...draft.values,text:''};draft.version++;if(displayed?.id===scope.id)$('input-text').value=''}
  draft.pending=false;if(displayed?.id===scope.id)$('input').querySelector('button').disabled=displayed.readOnly;
};
$('continue').onclick=()=>act('start');$('pause').onclick=()=>act('pause');$('refresh').onclick=refresh;
(async()=>{const generation=selectionGeneration;try{const session=await api('/api/session');token=session.token;if(document.activeElement!==$('create-workers'))$('create-workers').value=session.default_workers||6;const runs=await list();if(generation===selectionGeneration&&!current&&runs.length)await select((runs.find(r=>!r.checkpoint_read_only)||runs[0]).id);}catch(e){if(generation===selectionGeneration)err(e)}})();setInterval(refresh,3000);

$('checkpoint').onclick=async()=>{const scope=displayed;if(!shown(scope)||scope.readOnly)return;try{const r=await api('/api/runs/'+encodeURIComponent(scope.id)+'/export',{});snapshots.set(scope.id,r.snapshot_id);if(shown(scope))alert('已保存停点：'+r.snapshot_id+'。原运行保持独立。')}catch(e){if(shown(scope))err(e)}};
$('fork').onclick=async()=>{const scope=displayed;if(!shown(scope))return;const snapshot=prompt('停点名称',snapshots.get(scope.id)||'');if(!snapshot)return;const id=prompt('新运行ID（英文字母开头，字母/数字/短横线）','fork-'+Date.now());if(!id||!shown(scope))return;try{await api('/api/runs/'+encodeURIComponent(scope.id)+'/fork',{snapshot_id:snapshot,id});if(selected(scope))await select(id);else list().catch(()=>{})}catch(e){if(shown(scope))err(e)}};

$('workers').oninput=()=>{if(!displayed)return;const draft=runDraft(displayed.id).scheduling;draft.dirty=true;draft.value=$('workers').value};
$('scheduling').onsubmit=async ev=>{ev.preventDefault();const scope=displayed;if(!shown(scope)||scope.readOnly)return;const draft=runDraft(scope.id).scheduling;if(draft.pending)return;const value=Number($('workers').value);draft.pending=true;$('scheduling').querySelector('button').disabled=true;const ok=await act('scheduling',{max_workers:value},scope);draft.pending=false;if(ok&&Number(draft.value)===value)draft.dirty=false;if(shown(scope))refresh()};
