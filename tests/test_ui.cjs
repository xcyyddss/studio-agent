// Shipped HTML/CSS/JS in a real Chromium browser. API responses are fixtures;
// no running Studio, user data, Codex call, external network or browser profile.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),cp=require('node:child_process');
const {test,before,after}=require('node:test');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const temp=path.join(root,'.validation','focus-browser-tmp');fs.mkdirSync(temp,{recursive:true});
process.env.TEMP=temp;process.env.TMP=temp;
const revision=process.env.STUDIO_UI_REV;
function asset(name){return revision?cp.execFileSync('git',['show',`${revision}:studio_runtime/static/${name}`],{cwd:root}):fs.readFileSync(path.join(root,'studio_runtime/static',name))}
let browser;
before(async()=>{browser=await chromium.launch({executablePath:process.env.STUDIO_TEST_BROWSER||(process.platform==='win32'?'C:/Program Files/Google/Chrome/Application/chrome.exe':undefined),headless:true})});
after(async()=>{await browser?.close()});
function snapshot(id){
 const file={id:'FILE.'+id,name:'original.txt',version:1,topic_id:'ROOT',origin:'model',bytes:20,state:'published'};
 return {id,scheduling:{max_workers:6},state:'waiting',workflow:{goal:'Goal '+id,topics:[{id:'ROOT',q:'Root'},{id:'CHILD',q:'Child'},{id:'USER'}],questions:[{id:'QUESTION.'+id,question:'Question '+id,topic_id:'ROOT',attachments:[file]}],file_exchange:[file]},artifacts:[],jobs:[]};
}
async function harness(t){
 const context=await browser.newContext(),page=await context.newPage();page.setDefaultTimeout(6000);
 const h={page,context,data:{A:snapshot('A'),B:snapshot('B')},reads:{A:0,B:0},errors:[],posts:[],awaiters:[],waiting:new Set()};
 page.on('pageerror',e=>h.errors.push(e.message));
 await page.route('http://127.0.0.1:41799/**',async route=>{
  const req=route.request(),url=new URL(req.url()),p=url.pathname;
  const json=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
  if(p==='/api/session')return json({token:'fixture-token',default_workers:6});
  if(p==='/api/runs')return json({runs:[{id:'A',goal:'Goal A'},{id:'B',goal:'Goal B'}]});
  const m=p.match(/^\/api\/runs\/([AB])(?:\/(.*))?$/);
  if(m){
   const [,id,action]=m;
   if(!action){h.reads[id]++;const d=structuredClone(h.data[id]);d.workflow.reason='poll '+h.reads[id];return json(d)}
   if(action==='scheduling'){h.data[id].scheduling.max_workers=req.postDataJSON().max_workers;return json(h.data[id].scheduling)}
   if(action==='sandbox-check'){h.sandboxChecks=(h.sandboxChecks||0)+1;h.data[id].sandbox_check={available:true,checked_at:'fixture-time',reason:'fixture successful probe'};return json(h.data[id].sandbox_check)}
   if(action==='retry-tool'){(h.toolRetries??=[]).push({id,...req.postDataJSON()});h.data[id].python_tests[0].retryable=false;h.data[id].python_tests[0].retried_by='TOOL.NEW';return json({new_request_id:'TOOL.NEW'})}
   if(action==='dismiss-tool-recovery'){(h.toolDismissals??=[]).push({id,...req.postDataJSON()});h.data[id].python_tests[0].recovery.state='dismissed';return json({id:'INPUT.DECISION'})}
   if(action==='presented')return json({ok:true});
   if(action==='input'){
    let resolve;const response=new Promise(r=>resolve=r);
    const post={id,body:req.postDataJSON(),reply(body={ok:true},status=200){h.waiting.delete(post);resolve({body,status})}};
    h.waiting.add(post);if(h.awaiters.length)h.awaiters.shift()(post);else h.posts.push(post);
    const r=await response;return json(r.body,r.status).catch(()=>{});
   }
   throw Error('Unexpected fixture action: '+action);
  }
  const name=p==='/'?'index.html':p.slice(1);
  assert.ok(['index.html','app.js','attachments.js','sandbox.js','style.css','favicon.ico'].includes(name),name);
  if(name==='favicon.ico')return route.fulfill({status:404,body:''});
  return route.fulfill({contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html',body:asset(name)});
 });
 h.post=()=>h.posts.length?Promise.resolve(h.posts.shift()):new Promise(r=>h.awaiters.push(r));
 h.refresh=()=>page.evaluate(()=>refresh());
 h.select=async id=>{await page.evaluate(id=>select(id),id);assert.equal(await page.locator('#title').innerText(),'Goal '+id)};
 h.submit=selector=>page.locator(selector).evaluate(form=>form.requestSubmit());
 t.after(async()=>{for(const p of h.waiting)p.reply({error:'fixture teardown'},400);await context.close();assert.deepEqual(h.errors,[])});
 await page.goto('http://127.0.0.1:41799/');await page.waitForFunction(()=>document.getElementById('title').textContent==='Goal A');
 return h;
}
async function capture(page,selector,text='abcdef'){
 const input=page.locator(selector);await input.fill(text);await input.focus();
 return input.evaluateHandle(e=>{e.setSelectionRange(2,4,'backward');return e});
}

test('sandbox health and retry route to the displayed run, preserve failure and show uptake',async t=>{
 const h=await harness(t);
 h.data.A.python_tests=[{id:'TOOL.1',topic_id:'ROOT',state:'answered',outcome:'failed',retryable:true,result_id:'INPUT.1',
   diagnostic:{message:'Native preparation failed',recovery:'Recheck environment'},uptake:{state:'processed',effect:'Failure acknowledged'}}];
 await h.refresh();assert.match(await h.page.locator('#python-tests').innerText(),/Native preparation failed/);
 await h.page.locator('#sandbox-check').click();await h.page.waitForFunction(()=>document.getElementById('sandbox-health').textContent.includes('探针通过'));
 assert.equal(h.sandboxChecks,1);
 await h.page.locator('#python-tests button').click();await h.page.waitForFunction(()=>document.getElementById('python-tests').textContent.includes('TOOL.NEW'));
 assert.deepEqual(h.toolRetries,[{id:'A',request_id:'TOOL.1',reason:'用户在工作台请求恢复隔离测试',allow_uncertain:false}]);
 assert.match(await h.page.locator('#python-tests').innerText(),/Failure acknowledged/);
 h.data.A.checkpoint_read_only=true;await h.refresh();assert.equal(await h.page.locator('#sandbox-check').isDisabled(),true);
 assert.equal(await h.page.locator('#python-tests button').count(),0);
});

test('uncertain sandbox retry requires confirmation and is scoped after switching',async t=>{
 const h=await harness(t);h.data.B.python_tests=[{id:'TOOL.B',topic_id:'CHILD',state:'answered',outcome:'uncertain',retryable:true}];
 await h.select('B');h.page.once('dialog',d=>d.dismiss());await h.page.locator('#python-tests button').click();assert.equal(h.toolRetries,undefined);
 h.page.once('dialog',d=>d.accept());await h.page.locator('#python-tests button').click();
 await h.page.waitForFunction(()=>document.getElementById('python-tests').textContent.includes('TOOL.NEW'));
 assert.equal(h.toolRetries[0].id,'B');assert.equal(h.toolRetries[0].allow_uncertain,true);
});
async function unchanged(handle,text='abcdef'){
 assert.deepEqual(await handle.evaluate(e=>({connected:e.isConnected,focused:document.activeElement===e,text:e.value,start:e.selectionStart,end:e.selectionEnd,direction:e.selectionDirection})),{connected:true,focused:true,text,start:2,end:4,direction:'backward'});
}
test('ending execution recovery requires a reason and does not mark tests passed',async t=>{
 const h=await harness(t);h.data.A.python_tests=[{id:'TOOL.1',topic_id:'ROOT',state:'answered',outcome:'failed',recovery:{state:'awaiting_action'}}];
 await h.refresh();h.page.once('dialog',d=>d.dismiss());await h.page.getByRole('button',{name:'结束恢复等待'}).click();
 assert.equal(h.toolDismissals,undefined);
 h.page.once('dialog',d=>d.accept('使用另一种验证方法'));await h.page.getByRole('button',{name:'结束恢复等待'}).click();
 await h.page.waitForFunction(()=>!document.getElementById('python-tests').textContent.includes('结束恢复等待'));
 assert.deepEqual(h.toolDismissals,[{id:'A',request_id:'TOOL.1',reason:'使用另一种验证方法'}]);
 assert.equal(h.data.A.python_tests[0].outcome,'failed');
});
const qform='#questions .question-form',qtext=qform+' textarea',gform='#file-upload',gtext=gform+' > textarea';

test('question textarea keeps focus and selection through two real 3-second polls while status updates',async t=>{
 const h=await harness(t),p=h.page,handle=await capture(p,qtext),n=h.reads.A;
 await p.waitForFunction(n=>Number(document.getElementById('notice').textContent.split(' ')[1])>=n+2,n,{timeout:8500});
 await unchanged(handle);await p.keyboard.insertText('继续输入');
 assert.equal(await p.locator(qtext).inputValue(),'ab继续输入ef');
});
test('file note, file input and topic/version controls survive polling with real browser File objects',async t=>{
 const h=await harness(t),p=h.page;
 await p.locator(gform+' input[type=file]').setInputFiles({name:'draft.txt',mimeType:'text/plain',buffer:Buffer.from('中文原件')});
 await p.locator(gform+' .attachment-picker select').selectOption('FILE.A');
 const file=await p.locator(gform+' input[type=file]').elementHandle(),target=await p.locator(gform+' > label select').elementHandle();
 const handle=await capture(p,gtext);await h.refresh();await h.refresh();await unchanged(handle);
 assert.equal(await file.evaluate(e=>e.isConnected&&e.files[0].name),'draft.txt');
 assert.equal(await target.evaluate(e=>e.isConnected),true);
 assert.equal(await p.locator(gform+' .attachment-picker select').inputValue(),'FILE.A');
});
test('primary input preserves selection during status refresh',async t=>{
 const h=await harness(t),handle=await capture(h.page,'#input-text');await h.refresh();await h.refresh();await unchanged(handle);
});
test('native Chromium IME composition survives polling in all three editors',async t=>{
 const h=await harness(t),p=h.page,cdp=await h.context.newCDPSession(p);
 for(const selector of [qtext,gtext,'#input-text']){
  const input=p.locator(selector);await input.fill('');await input.focus();const handle=await input.elementHandle();
  await handle.evaluate(e=>{e.ends=0;e.addEventListener('compositionend',()=>e.ends++)});
  await cdp.send('Input.imeSetComposition',{text:'nihao',selectionStart:5,selectionEnd:5});
  await h.refresh();await h.refresh();
  assert.deepEqual(await handle.evaluate(e=>[e.isConnected,document.activeElement===e,e.value,e.ends]),[true,true,'nihao',0]);
  await cdp.send('Input.insertText',{text:'你好'});
  assert.equal(await input.inputValue(),'你好');assert.equal(await handle.evaluate(e=>e.ends),1);
 }
});
test('question insertion, reorder and metadata updates do not move or detach the active question',async t=>{
 const h=await harness(t),p=h.page,handle=await capture(p,qtext);
 h.data.A.workflow.questions.unshift({id:'NEW',question:'New question'});h.data.A.workflow.questions[1].question='Updated question A';
 await h.refresh();await unchanged(handle);assert.match(await p.locator('#questions').innerText(),/Updated question A/);
 assert.equal(await p.locator(qtext).count(),2);
 h.data.A.workflow.questions.reverse();await h.refresh();await unchanged(handle);
 h.data.A.workflow.questions=h.data.A.workflow.questions.filter(q=>q.id==='QUESTION.A');await h.refresh();await unchanged(handle);
 h.data.A.workflow.questions[0].status='answered';await h.refresh();assert.equal(await p.locator(qtext).count(),0);
});
test('failed reply keeps focus, draft, permission and attached file, and retry keeps submission identity',async t=>{
 const h=await harness(t),p=h.page;
 await p.locator(qform+' input[type=file]').setInputFiles({name:'answer.txt',mimeType:'text/plain',buffer:Buffer.from('answer bytes')});
 await p.locator(qform+' > select').selectOption('none');const handle=await capture(p,qtext);
 await h.submit(qform);const first=await h.post();await h.refresh();first.reply({error:'fixture refusal'},400);
 await p.waitForFunction(()=>!document.querySelector('#questions .question-form > button').disabled);await unchanged(handle);
 assert.equal(first.body.profile_scope,'none');assert.equal(first.body.question_id,'QUESTION.A');
 await h.submit(qform);const second=await h.post();assert.equal(second.body.submission_id,first.body.submission_id);second.reply({error:'fixture refusal'},400);
});
test('typing during successful reply submission is not cleared by acknowledgment or polling',async t=>{
 const h=await harness(t),p=h.page;await p.locator(qtext).fill('Submitted answer');await h.submit(qform);const post=await h.post();
 const handle=await capture(p,qtext,'abcdef');await h.refresh();post.reply();
 await p.waitForFunction(()=>!document.querySelector('#questions .question-form > button').disabled);await h.refresh();await unchanged(handle);
 assert.equal(post.body.text,'Submitted answer');
});
test('unchanged successful question submission clears native file state and restores required text',async t=>{
 const h=await harness(t),p=h.page;await p.locator(qform+' input[type=file]').setInputFiles({name:'file.txt',mimeType:'text/plain',buffer:Buffer.from('file only')});
 assert.equal(await p.locator(qtext).evaluate(e=>e.required),false);
 await h.submit(qform);const post=await h.post();post.reply();
 await p.waitForFunction(()=>!document.querySelector('#questions .question-form > button').disabled);
 assert.equal(await p.locator(qform+' input[type=file]').evaluate(e=>e.files.length),0);
 assert.equal(await p.locator(qtext).evaluate(e=>e.required),true);
});
test('general upload keeps newer note and file during acknowledgment; a later unchanged submit clears them',async t=>{
 const h=await harness(t),p=h.page,picker=p.locator(gform+' input[type=file]');
 await picker.setInputFiles({name:'first.txt',mimeType:'text/plain',buffer:Buffer.from('first')});await p.locator(gtext).fill('first note');await h.submit(gform);const first=await h.post();
 await picker.setInputFiles({name:'second.txt',mimeType:'text/plain',buffer:Buffer.from('second')});const handle=await capture(p,gtext);await h.refresh();first.reply();
 await p.waitForFunction(()=>!document.querySelector('#file-upload > button').disabled);await unchanged(handle);
 assert.equal(await picker.evaluate(e=>e.files[0].name),'second.txt');
 await h.submit(gform);const second=await h.post();assert.equal(second.body.text,'abcdef');assert.equal(second.body.attachments[0].name,'second.txt');second.reply();
 await p.waitForFunction(()=>!document.querySelector('#file-upload > button').disabled);
 assert.equal(await p.locator(gtext).inputValue(),'');assert.equal(await picker.evaluate(e=>e.files.length),0);
});
test('composition starting while a post is pending protects the draft before its first input event',async t=>{
 const h=await harness(t),p=h.page;
 for(const [form,selector] of [[qform,qtext],['#input','#input-text'],[gform,gtext]]){
  if(form===gform)await p.locator(gform+' input[type=file]').setInputFiles({name:'draft.txt',mimeType:'text/plain',buffer:Buffer.from('draft')});
  await p.locator(selector).fill('old');await h.submit(form);const post=await h.post();
  await p.locator(selector).dispatchEvent('compositionstart',{data:''});post.reply();
  await p.waitForFunction(s=>!document.querySelector(s+' > button').disabled,form);
  assert.equal(await p.locator(selector).inputValue(),'old',selector);await p.locator(selector).dispatchEvent('compositionend',{data:''});
 }
});
test('run switching isolates drafts and delayed replies; retained detached question handlers cannot submit',async t=>{
 const h=await harness(t),p=h.page;
 await p.locator(qtext).fill('Draft A');await p.locator(gtext).fill('File A');
 await p.locator(gform+' input[type=file]').setInputFiles({name:'A.txt',mimeType:'text/plain',buffer:Buffer.from('A')});
 await p.locator(gform+' .attachment-picker select').selectOption('FILE.A');
 const old=await p.locator(qform).elementHandle();await h.submit(qform);const pending=await h.post();await h.select('B');
 await p.locator(qtext).fill('Draft B');await p.locator(gtext).fill('File B');pending.reply({error:'old A'},400);
 await old.evaluate(e=>e.onsubmit({preventDefault(){}}));await h.refresh();
 assert.equal(await p.locator(qtext).inputValue(),'Draft B');assert.equal(await p.locator(gtext).inputValue(),'File B');assert.equal(await p.locator('#error').isVisible(),false);
 await h.select('A');assert.equal(await p.locator(qtext).inputValue(),'Draft A');assert.equal(await p.locator(gtext).inputValue(),'File A');
 assert.match(await p.locator(gform).innerText(),/A.txt/);assert.equal(await p.locator(gform+' .attachment-picker select').inputValue(),'FILE.A');
 assert.equal(h.posts.length,0);
});
test('read-only snapshots remove input forms and new goal remains editable without polling a previous run',async t=>{
 const h=await harness(t),p=h.page;h.data.B.checkpoint_read_only=true;await h.select('B');
 assert.equal(await p.locator(qform).count(),0);assert.equal(await p.locator(gform).isVisible(),false);assert.equal(await p.locator('#input-text').isDisabled(),true);
 await p.locator('#new').click();const handle=await capture(p,'#goal');const count=h.reads.B;await h.refresh();await unchanged(handle);assert.equal(h.reads.B,count);
});
test('upload acknowledgment after A to B to A clears only the unchanged current draft',async t=>{
 const h=await harness(t),p=h.page;
 for(const edit of [false,true]){
  await p.locator(gform+' input[type=file]').setInputFiles({name:'A.txt',mimeType:'text/plain',buffer:Buffer.from('A')});
  await p.locator(gtext).fill('submitted');await h.submit(gform);const post=await h.post();
  await h.select('B');await h.select('A');if(edit)await p.locator(gtext).fill('new note');
  post.reply();await p.waitForFunction(()=>!document.querySelector('#file-upload > button').disabled);
  assert.equal(await p.locator(gtext).inputValue(),edit?'new note':'');await h.refresh();
  assert.equal(await p.locator(gtext).inputValue(),edit?'new note':'');
 }
});
test('returning from New goal during composition does not leave the previous draft unable to submit',async t=>{
 const h=await harness(t),p=h.page;
 await p.locator('#input-text').fill('draft A');await p.locator('#input-text').dispatchEvent('compositionstart');
 await p.locator('#new').click();await h.select('A');await h.submit('#input');const post=await h.post();
 assert.equal(post.body.text,'draft A');post.reply();
});

test('concurrency input keeps focus and draft across polls and persists its submitted value',async t=>{
 const h=await harness(t),p=h.page;
 assert.equal(await p.locator('#workers').inputValue(),'6');
 await p.locator('#workers').fill('12');await p.locator('#workers').focus();
 const handle=await p.locator('#workers').elementHandle();await h.refresh();await h.refresh();
 assert.equal(await handle.evaluate(e=>e.isConnected&&document.activeElement===e&&e.value==='12'),true);
 await h.submit('#scheduling');await p.waitForFunction(()=>document.getElementById('workers-status').textContent.includes('12'));
 assert.equal(h.data.A.scheduling.max_workers,12);await h.select('B');assert.equal(await p.locator('#workers').inputValue(),'6');
 await h.select('A');assert.equal(await p.locator('#workers').inputValue(),'12');
});
test('switching runs isolates unsaved concurrency drafts and read-only snapshots cannot edit settings',async t=>{
 const h=await harness(t),p=h.page;await p.locator('#workers').fill('14');await h.select('B');
 assert.equal(await p.locator('#workers').inputValue(),'6');await h.select('A');assert.equal(await p.locator('#workers').inputValue(),'14');
 h.data.A.checkpoint_read_only=true;await h.refresh();assert.equal(await p.locator('#workers').isDisabled(),true);
 assert.equal(await p.locator('#scheduling button').isDisabled(),true);
});
test('multiple files keep separate version choices, survive failed submit and retry atomically',async t=>{
 const h=await harness(t),p=h.page;
 h.data.A.workflow.questions[0].attachments.push({id:'FILE.OTHER',name:'other.txt',version:2});
 await h.refresh();
 const picker=p.locator(qform+' input[type=file]');
 assert.equal(await picker.evaluate(e=>e.multiple),true);
 await picker.setInputFiles([
  {name:'first.txt',mimeType:'text/plain',buffer:Buffer.from('第一个文件')},
  {name:'second.txt',mimeType:'text/plain',buffer:Buffer.from('second file')}
 ]);
 const versions=p.locator(qform+' .attachment-picker select');
 await versions.nth(0).selectOption('FILE.A');await versions.nth(1).selectOption('FILE.OTHER');
 await h.refresh();await h.submit(qform);const first=await h.post();
 assert.deepEqual(first.body.attachments.map(f=>[f.name,f.replaces]),[['first.txt','FILE.A'],['second.txt','FILE.OTHER']]);
 assert.equal(Buffer.from(first.body.attachments[0].data_base64,'base64').toString(),'第一个文件');
 first.reply({error:'retry fixture'},400);await p.waitForFunction(()=>!document.querySelector('#questions .question-form > button').disabled);
 assert.equal(await versions.count(),2);await h.submit(qform);const second=await h.post();
 assert.deepEqual(second.body.attachments,first.body.attachments);assert.equal(second.body.submission_id,first.body.submission_id);
 second.reply();await p.waitForFunction(()=>!document.querySelector('#questions .question-form > button').disabled);
 assert.equal(await picker.evaluate(e=>e.files.length),0);assert.equal(await versions.count(),0);
});
test('multiple-file draft survives switching runs; individual removal and excessive selection are safe',async t=>{
 const h=await harness(t),p=h.page,picker=p.locator(gform+' input[type=file]');
 const files=['one.txt','two.txt'].map(name=>({name,mimeType:'text/plain',buffer:Buffer.from(name)}));
 await picker.setInputFiles(files);await h.select('B');await h.select('A');
 assert.equal(await p.locator(gform+' .file-selection').count(),2);
 await p.getByRole('button',{name:'移除 one.txt',exact:true}).click();
 await picker.setInputFiles([0,1,2,3,4].map(n=>({name:n+'.txt',mimeType:'text/plain',buffer:Buffer.from('x')})));
 assert.equal(await p.locator(gform+' .file-selection').count(),1);
 assert.match(await p.locator(gform).innerText(),/two.txt/);
 await h.submit(gform);const post=await h.post();
 assert.deepEqual(post.body.attachments.map(f=>f.name),['two.txt']);post.reply();
});
test('unpublished and reused proposals are not presented as user tasks',async t=>{
 const h=await harness(t),p=h.page;
 h.data.A.workflow.questions.push({id:'INTERNAL',question:'Internal proposal',state:'proposed'},
  {id:'LINKED',question:'Reused existing task',state:'linked'});
 await h.refresh();assert.equal(await p.locator(qform).count(),1);
 assert.doesNotMatch(await p.locator('#questions').innerText(),/Internal proposal|Reused existing task/);
});
test('partial delivery is visibly distinct and does not hide unanswered questions',async t=>{
 const h=await harness(t),p=h.page;
 h.data.A.workflow.deliveries=[{id:'DELIVERY.1',state:'partial',summary:'First usable phase',paths:[],remaining_dependencies:[{id:'H.pending'}]}];
 await h.refresh();
 assert.match(await p.locator('#deliveries').innerText(),/阶段交付.*仍有依赖等待/);
 assert.equal(await p.locator(qform).count(),1);
 h.data.A.workflow.deliveries.push({id:'DELIVERY.2',state:'delivered',summary:'Final result'});
 await h.refresh();assert.match(await p.locator('#deliveries').innerText(),/正式交付/);
});

test('topic shows current phase, retained knowledge, actual delta and wake condition',async t=>{
 const h=await harness(t),p=h.page;
 Object.assign(h.data.A.workflow.topics[0],{
  phase:{name:'资料核对',deliverable:'已知条件报告',acceptance:'保留未知边界'},
  last_delta:{parent:{source_job:'JOB.CURRENT',knowledge_changes:[{id:'K.2'}],obligation_changes:[],input_ids:['INPUT.NEW']}},
  wait_dependencies:[{id:'INTERNAL.7',state:'open'}]
 });
 h.data.A.workflow.working_knowledge={ROOT:{'K.1':{status:'retired'},'K.2':{status:'conditional'}}};
 await h.refresh();const body=await p.locator('#topics').innerText();
 assert.match(body,/当前阶段：资料核对/);assert.match(body,/验收：保留未知边界/);
 assert.match(body,/当前保留认识：1 项/);assert.match(body,/JOB.CURRENT：认识变化 1 项，义务变更 0 项，处理输入 1 项/);
 assert.match(body,/恢复条件：INTERNAL.7/);
});
