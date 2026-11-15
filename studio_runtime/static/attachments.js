'use strict';
// File objects stay in memory across polling and task changes, never in URLs.
const fileDrafts=new Map();
let uploadEditor=null;
function attachmentDraft(run,key){const id=run+'/'+key;if(!fileDrafts.has(id))fileDrafts.set(id,{files:[],topic:'ROOT',note:'',noteVersion:0,nonce:crypto.randomUUID()});return fileDrafts.get(id)}
function fileLinkPair(run,file){
  const row=elem('div',undefined,'file-entry'),base='/api/runs/'+encodeURIComponent(run)+'/file/'+encodeURIComponent(file.id);
  row.append(elem('span',file.name+' · 版本 '+file.version+' · '+file.id));
  for(const [label,suffix] of [['查看',''],['下载','?download=1']]){const link=elem('a',label);link.href=base+suffix;link.target='_blank';link.rel='noopener noreferrer';row.append(link)}
  for(const p of array(file.extraction?.pages).filter(p=>p.image_sha256)){const link=elem('a','页图 '+p.page);link.href=base+'?page='+p.page;link.target='_blank';link.rel='noopener noreferrer';row.append(link)}
  return row;
}
function filePicker(run,key,bases,onChange){
  const draft=attachmentDraft(run,key),wrap=elem('div',undefined,'attachment-picker'),input=elem('input'),name=elem('small'),rows=elem('div');
  const editors=new Map();let lastSelection=null;
  input.type='file';input.multiple=true;input.setAttribute('aria-label','选择要交给议题的文件');
  wrap.update=(nextBases=bases)=>{
    bases=nextBases;
    if(lastSelection!==draft.files){
      const selected=new DataTransfer();for(const e of draft.files)selected.items.add(e.file);
      input.files=selected.files;lastSelection=draft.files;
    }
    for(const [entry,editor] of editors)if(!draft.files.includes(entry)){editor.row.remove();editors.delete(entry)}
    for(const entry of draft.files){
      if(!editors.has(entry)){
        const row=elem('div',undefined,'file-selection'),label=elem('label',entry.file.name+'：修改哪个版本（可选）'),select=elem('select'),remove=elem('button','移除 '+entry.file.name);
        select.setAttribute('aria-label',entry.file.name+' 的原版本');label.append(select);
        select.onchange=()=>{entry.replaces=select.value;draft.nonce=crypto.randomUUID()};
        remove.type='button';remove.onclick=()=>{draft.files=draft.files.filter(f=>f!==entry);draft.nonce=crypto.randomUUID();wrap.update()};
        row.append(label,remove);rows.append(row);editors.set(entry,{row,select});
      }
      const select=editors.get(entry).select;
      syncOptions(select,[['','新增材料，不替代已有版本'],...bases.map(f=>[f.id,f.name+' · v'+f.version+' · '+f.id])]);
      if(entry.replaces&&!bases.some(f=>f.id===entry.replaces)){entry.replaces='';draft.nonce=crypto.randomUUID()}
      setValue(select,entry.replaces);
    }
    const caption=draft.files.length?'已选择 '+draft.files.length+' 个文件：'+draft.files.map(e=>e.file.name).join('、'):'可一次选择最多 4 个文件，单个不超过 4 MB';
    if(name.textContent!==caption)name.textContent=caption;
    onChange?.();
  };
  input.onchange=()=>{
    const files=Array.from(input.files||[]);if(!files.length)return;
    if(files.length>4||files.some(f=>f.size>4000000)){lastSelection=null;wrap.update();err(files.length>4?'一次最多上传 4 个文件；已保留此前选择':'单个附件不得超过 4 MB；已保留此前选择');return}
    draft.files=files.map(file=>({file,replaces:''}));draft.nonce=crypto.randomUUID();wrap.update();
  };
  const clear=elem('button','移除所选文件');clear.type='button';clear.onclick=()=>{draft.files=[];draft.nonce=crypto.randomUUID();input.value='';wrap.update()};
  wrap.append(input,name,rows,clear);wrap.update();return wrap;
}
async function attachmentRequest(run,key,values={}){
  const draft=attachmentDraft(run,key);if(!draft.files.length)return {};
  const signature=JSON.stringify(values);if(draft.signature!==signature){draft.signature=signature;draft.nonce=crypto.randomUUID()}
  // Snapshot all entries before asynchronous reads. Edits during submission
  // belong to the next draft and must not be acknowledged by this request.
  const entries=draft.files.map(e=>({...e})),nonce=draft.nonce;
  const replacements=entries.map(e=>e.replaces).filter(Boolean);
  if(new Set(replacements).size!==replacements.length)throw Error('同一次提交不能用多个文件替换同一版本');
  const attachments=await Promise.all(entries.map(async({file,replaces})=>{
    const bytes=new Uint8Array(await file.arrayBuffer());let binary='';
    for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));
    return {name:file.name,data_base64:btoa(binary),replaces};
  }));
  return {attachments,submission_id:nonce};
}
function acknowledgeAttachment(run,key,nonce){const d=attachmentDraft(run,key);if(nonce&&d.nonce===nonce){d.files=[];d.nonce=crypto.randomUUID()}}
function renderAttachments(data,scope){
  const run=scope.id,state=data.workflow||data,files=array(state.file_exchange),questions=array(state.questions);
  for(const q of questions){const editor=questionEditors.get(q.id);if(!editor)continue;const form=editor.a.parentElement,refs=array(q.attachments);
    if(scope.readOnly)continue;
    const draft=attachmentDraft(run,q.id);
    const update=()=>{editor.a.required=!draft.files.length};update();
    if(!editor.picker){editor.picker=filePicker(run,q.id,refs,update);form.insertBefore(editor.picker,editor.b)}
    else editor.picker.update(refs);
  }
  const library=$('attachment-library');if(library){library.replaceChildren();const labels={received:'已收到，等待读取',partly_read:'部分文字或页图已送入议题调用',read:'当前版本的内容已全部送入议题调用，等待承接',handled:'已作承接判断',deferred:'暂缓，仍待处理',unreadable:'尚不能解析正文',superseded:'已有修改版，原件保留',published:'议题生成的文件'};
    for(const f of files){const card=elem('div',undefined,'file-record');card.append(fileLinkPair(run,f),elem('small','接收议题：'+f.topic_id+' · '+(labels[f.state]||f.state)+' · '+f.bytes+' 字节'));
      if(f.replaces)card.append(elem('small','修改自：'+f.replaces));
      if(f.origin!=='model')card.append(elem('small',f.deliveries?.length?'附件通知已进入议题调用':'附件通知待投递'),elem('small',f.extraction?.limitations||''));
      if(f.extraction?.page_count){const pages=[...new Set(array(f.reads).filter(r=>r.image_delivered).map(r=>r.page))];card.append(elem('small','共 '+f.extraction.page_count+' 页/图；视觉附件已送达页：'+(pages.join('、')||'无')))}
      const ranges=[...new Map(array(f.reads).map(r=>[JSON.stringify([r.page,r.start,r.end,r.image_delivered,r.state]),r])).values()];
      for(const r of ranges.slice(-8)){const text=r.state==='ready'?((r.page?'第 '+r.page+' 页/图：':'')+'文字字符 '+r.start+'–'+r.end+(r.image_sha256?'；整图'+(r.image_delivered?'已送入调用':'尚无送达凭据'):'')):'读取结果：'+(r.state==='unreadable'?'无法读取':'不支持解析');card.append(elem('small',text))}
      if(ranges.length>8)card.append(elem('small','另有 '+(ranges.length-8)+' 条历史范围记录'));
      const use=array(f.uses).at(-1);if(use)card.append(elem('p',({adopt:'采用',reject:'不采用',defer:'暂缓'}[use.decision]||use.decision)+'：'+use.reason+'；影响：'+use.effect),elem('small',(use.full_content_delivered??use.full_text_delivered)?'全部要求范围已有调用送达证据；不等于正确理解':'未取得完整内容；不表示已读'));
      library.append(card);
    }if(!files.length)library.append(elem('p','尚无登记附件。历史产物仍可在“当前交付”中查看。','muted'));
  }
  const form=$('file-upload');if(!form)return;form.hidden=scope.readOnly;
  if(scope.readOnly){form.replaceChildren();form.onsubmit=null;uploadEditor=null;return}
  if(!uploadEditor||uploadEditor.scope.id!==run||uploadEditor.scope.generation!==scope.generation){
    form.replaceChildren();uploadEditor=createUploadEditor(form,scope,state);
  }
  uploadEditor.update(state);
}

function createUploadEditor(form,scope,state){
  const run=scope.id,draft=attachmentDraft(run,'general'),select=elem('select'),label=elem('label','明确交给议题'),note=elem('textarea'),button=elem('button','上传并交给议题');
  const editor={scope};let files=array(state.file_exchange),composing=false;
  label.append(select);note.rows=2;note.placeholder='这份文件用于什么，修改了什么（可选）';note.value=draft.note;
  note.oninput=()=>{draft.note=note.value;draft.noteVersion++;draft.nonce=crypto.randomUUID()};
  note.addEventListener('compositionstart',()=>{composing=true;draft.noteVersion++});
  note.addEventListener('compositionend',()=>{composing=false;note.oninput()});
  const picker=filePicker(run,'general',files.filter(f=>f.topic_id===draft.topic));
  select.onchange=()=>{draft.topic=select.value;draft.files.forEach(e=>e.replaces='');draft.nonce=crypto.randomUUID();picker.update(files.filter(f=>f.topic_id===draft.topic))};
  editor.update=state=>{
    files=array(state.file_exchange);const topics=array(state.topics).filter(t=>t.id!=='USER');
    syncOptions(select,topics.map(t=>[t.id,t.id+' · '+title(t.q||t.summary||t.id)]));
    if(!topics.some(t=>t.id===draft.topic))draft.topic='ROOT';setValue(select,draft.topic);
    picker.update(files.filter(f=>f.topic_id===draft.topic));button.disabled=!!draft.pending;
  };
  editor.syncDraft=()=>{if(!composing)setValue(note,draft.note);button.disabled=!!draft.pending;picker.update()};
  form.append(label,picker,note,elem('small','原件仅交给所选议题；新版本不会覆盖旧件。支持文本、DOCX 主体文字、单帧图片和 PDF 文字及页图；读取进度与解析限制分别记录。'),button);
  form.onsubmit=async ev=>{ev.preventDefault();if(!shown(scope)||displayed.readOnly||uploadEditor!==editor||draft.pending||composing)return;if(!draft.files.length)return err('请先选择文件');draft.pending=true;button.disabled=true;
    const target=draft.topic,text=draft.note,version=draft.noteVersion;
    try{
      const extra=await attachmentRequest(run,'general',{text,topic_id:target});if(!shown(scope))return;
      const ok=await act('input',{text,topic_id:target,profile_scope:'none',...extra},scope);
      if(ok){acknowledgeAttachment(run,'general',extra.submission_id);if(draft.noteVersion===version){draft.note='';draft.noteVersion++}}
    }catch(e){if(shown(scope))err(e)}finally{draft.pending=false;if(uploadEditor?.scope.id===run&&shown(uploadEditor.scope))uploadEditor.syncDraft()}
  };
  return editor;
}
