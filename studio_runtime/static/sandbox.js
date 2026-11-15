'use strict';
const sandboxPending=new Set();
function renderSandbox(data,scope){
  const host=document.getElementById('python-tests');if(!host)return;
  const check=document.getElementById('sandbox-check'),health=document.getElementById('sandbox-health');
  const pendingKey=scope.id+':check';
  check.disabled=!!data.checkpoint_read_only||sandboxPending.has(pendingKey);
  check.onclick=async()=>{if(!shown(scope)||displayed.readOnly||sandboxPending.has(pendingKey))return;
    sandboxPending.add(pendingKey);check.disabled=true;
    try{await act('sandbox-check',{},scope)}finally{sandboxPending.delete(pendingKey);if(shown(scope))refresh()}};
  const status=data.sandbox_check;
  health.textContent=status?(status.available?'固定沙箱探针通过（不代表任务测试通过）':'测试环境未就绪')+' · '+status.checked_at+'\n'+(status.reason||''):'尚未检查；每次实际测试也会先检查环境。';
  host.replaceChildren(...array(data.python_tests).map(t=>{
    const outcome=t.outcome||t.state;
    const card=item(t.id+' · '+t.topic_id+' · '+(names[outcome]||outcome),t.diagnostic?.message||t.error||(t.outcome==='succeeded'?'测试已运行并报告通过':'等待执行返回'),
      (t.retry_of?'重新执行自 '+t.retry_of+'。 ':'')+(t.retried_by?'后继 '+t.retried_by+'。 ':'')+(t.diagnostic?.recovery||''));
    if(t.reported_summary)card.append(elem('p','测试 '+t.reported_summary.tests+'；失败 '+t.reported_summary.failures+'；错误 '+t.reported_summary.errors+'；跳过 '+t.reported_summary.skipped));
    card.append(elem('p',t.result_id?'结果 '+t.result_id+' · '+(t.uptake?.state==='processed'?'议题已明确承接：'+(t.uptake.effect||''):'尚待议题明确承接'):'尚无结果'));
    if(t.recovery?.state==='awaiting_action'){
      card.append(elem('p','待恢复：请修复环境后重新执行；若此测试不再需要，可以说明原因并结束等待。'));
      if(!data.checkpoint_read_only){const b=elem('button','结束恢复等待');
        b.onclick=async()=>{if(!shown(scope)||displayed.readOnly)return;const reason=prompt('说明不再恢复此次执行的原因（不会视为测试通过）：');if(!reason?.trim())return;
          b.disabled=true;try{await act('dismiss-tool-recovery',{request_id:t.id,reason},scope)}finally{if(shown(scope))refresh()}};card.append(b)}
    }
    const details=elem('details'),summary=elem('summary','查看执行诊断与输出');details.append(summary,elem('pre',JSON.stringify({diagnostic:t.diagnostic,stdout:t.stdout,stderr:t.stderr,receipt:t.receipt_path},null,2)));card.append(details);
    if(t.retryable&&!data.checkpoint_read_only){const key=scope.id+':'+t.id,b=elem('button','重新执行测试');b.disabled=sandboxPending.has(key);
      b.onclick=async()=>{if(!shown(scope)||displayed.readOnly||sandboxPending.has(key))return;
        if(t.outcome==='uncertain'&&!confirm('上次执行结果不明，可能已运行部分代码。确认保留原记录并重新执行同版测试？'))return;
        sandboxPending.add(key);b.disabled=true;
        try{await act('retry-tool',{request_id:t.id,reason:'用户在工作台请求恢复隔离测试',allow_uncertain:t.outcome==='uncertain'},scope)}finally{sandboxPending.delete(key);if(shown(scope))refresh()}};card.append(b)}
    return card;
  }));
  if(!array(data.python_tests).length)host.append(elem('p','尚无 Python 测试执行记录。','muted'));
}
