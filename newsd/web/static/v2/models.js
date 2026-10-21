/* 模型 (DEPTAI-216 3b): providers in failover order (drag / ↑↓), enable, test, add / edit with presets,
   options and AI content categories — backed by /api/me/llm/*. Keys never come back from the server. */
(function(){
 const {$,$$,esc,api}=ND,CFG=window.ND_LLM||{presets:[],providers:[]};
 const P=Object.fromEntries(CFG.presets.map(p=>[p.id,p]));
 let list=CFG.providers,editing=null,picked=null;
 const msg=j=>(j&&(j.error||(Array.isArray(j.detail)?j.detail.map(d=>d.msg).join('；'):j.detail)))||'';
 const fail=(j,w)=>ND.toast((w||'操作失败')+(msg(j)?'：'+msg(j):''));
 const keyTxt=x=>!x.needs_key?'无需 Key':x.has_key?'Key ••••'+esc(x.key_last4||''):x.key_unreadable?'Key 无法解密，请重新填写':'未填 Key';
 const stTxt=x=>{const t=x.last_test;if(!t)return '<span class="dot-s"></span> 未测试';return t.ok?`<span class="dot-s ok"></span> 正常 · ${(t.latency_ms/1000).toFixed(1)}s`:`<span class="dot-s err"></span> ${esc((t.error||'失败').slice(0,80))}`};
 async function reload(){const [ok,j]=await api('GET','/api/me/llm/providers');if(ok){list=j.providers;render()}}
 function render(){const act=list.filter(x=>x.enabled&&x.usable);
  $('#count').textContent=list.length+' 个 · '+act.length+' 启用';
  $('#chain').innerHTML=act.length?act.map((x,i)=>`${i?'<span class="arr">失败 →</span>':''}<span class="node ${i?'':'p'}">${i?'备份 '+i+' · ':'主用 · '}${esc(x.name)}</span>`).join(''):'<span class="muted">没有启用的 provider — 日报生成将跳过 AI 摘要</span>';
  $('#list').innerHTML=list.map((x,i)=>{const ai=act.indexOf(x);
   const role=!x.enabled?'<span class="rank backup">已停用</span>':!x.usable?'<span class="rank backup">配置不完整</span>':ai===0?'<span class="rank primary">主用</span>':`<span class="rank backup">备份 ${ai}</span>`;
   return `<article class="card prov ${x.enabled?'':'disabled'}" data-i="${i}" data-prov="${x.id}"><button class="handle" type="button" title="拖动排序" aria-label="拖动 ${esc(x.name)} 调整优先级（空格拿起，↑↓ 移动）">⋮⋮</button><div class="logo-tile" style="--c:${esc(x.color)}">${esc(x.short)}</div>
    <div class="pbody"><h4>${esc(x.name)} ${role}</h4><div class="sub"><span>${esc(x.preset_name)} · ${esc(x.model)}</span><span>${esc(x.base_url||'—')}</span><span>${keyTxt(x)}</span><span data-st>${stTxt(x)}</span></div></div>
    <div class="pctl"><button class="btn icon sm" data-act="up" type="button" aria-label="上移" ${i===0?'disabled':''}>↑</button><button class="btn icon sm" data-act="down" type="button" aria-label="下移" ${i===list.length-1?'disabled':''}>↓</button>
     <button class="btn sm" data-act="test" type="button">测试</button><button class="btn sm" data-act="edit" type="button">编辑</button><button class="btn sm danger" data-act="del" type="button">删除</button>
     <button class="switch" type="button" role="switch" aria-checked="${x.enabled}" data-act="on" aria-label="启用 ${esc(x.name)}"></button></div></article>`}).join('')||'<div class="card muted" style="text-align:center;padding:40px">还没有 provider，点右上角「添加 Provider」开始。</div>'}
 async function order(ids,undo){const [ok,j]=await api('PUT','/api/me/llm/order',{ids});if(!ok){fail(j,'排序失败');return reload()}list=j.providers;render();ND.toast('优先级已更新',undo)}
 function move(fi,ti,withUndo){const ids=list.map(x=>x.id),before=ids.slice();ids.splice(ti,0,ids.splice(fi,1)[0]);order(ids,withUndo?()=>order(before):undefined)}
 $('#list').addEventListener('toggle',async e=>{const b=e.target.closest('[data-act="on"]');if(!b)return;const x=list[+b.closest('.prov').dataset.i],on=b.getAttribute('aria-checked')==='true';
  const [ok,j]=await api('PATCH','/api/me/llm/providers/'+x.id,{enabled:on});if(!ok){fail(j);return reload()}ND.toast((on?'已启用 ':'已停用 ')+x.name);reload()});
 $('#list').addEventListener('click',async e=>{const b=e.target.closest('[data-act]');if(!b||b.dataset.act==='on')return;const card=b.closest('.prov'),i=+card.dataset.i,x=list[i];
  switch(b.dataset.act){
   case 'up':return move(i,i-1,true);
   case 'down':return move(i,i+1,true);
   case 'del':{if(!confirm('删除「'+x.name+'」？保存的 API Key 也会一起删除。'))return;const [ok,j]=await api('DELETE','/api/me/llm/providers/'+x.id);if(!ok)return fail(j);ND.toast('已删除 '+x.name);return reload()}
   case 'test':{card.querySelector('[data-st]').innerHTML='<span class="spin"></span> 测试中…';const [ok,j]=await api('POST',`/api/me/llm/providers/${x.id}/test`);
    if(!ok){fail(j,'测试失败');return reload()}ND.toast(j.ok?'连接成功 · '+x.name:'连接失败：'+(j.error||''));return reload()}
   case 'edit':return openModal(x);}});
 ND.sortable({root:$('#list'),lists:()=>[$('#list')],item:'.prov',handle:'.prov',grip:'.handle',kbd:'.handle',ignore:'button,input,select,a,.switch',tilt:.4,
  label:el=>list[+el.dataset.i].name,listName:()=>'Provider 优先级',
  onDrop:({fromIndex:fi,toIndex:ti,kbd})=>{if(fi!==ti)move(fi,ti,!kbd);return $(`#list .prov[data-i="${ti}"]`)}});
 // options
 const saveOpts=async()=>{const [ok,j]=await api('PUT','/api/me/llm/options',{retries:+$('#retry').value,timeout:+$('#timeout').value,translate_to:$('#lang').value});ok?ND.toast('已保存：重试 '+j.retries+' 次 · 超时 '+j.timeout+' 秒'):fail(j)};
 ['#retry','#timeout','#lang'].forEach(s=>$(s).addEventListener('change',saveOpts));
 // AI content categories
 $('#saveProfs').onclick=async()=>{const picks=$$('#profs [data-p]').filter(b=>b.getAttribute('aria-checked')==='true').map(b=>({name:b.dataset.p,threshold:parseFloat($(`#profs [data-thr="${b.dataset.p}"]`).value)}));
  const [ok,j]=await api('PUT','/api/me/profiles',picks);ok?ND.toast('已保存 '+picks.length+' 个类别'):fail(j)};
 // add / edit modal
 function pick(id,keepName){picked=id;const p=P[id]||{models:[],base_url:''};$$('.preset').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.p===id)));
  if(!keepName)$('#fName').value=p.name||'';$('#fUrl').value=p.base_url||'';$('#fUrl').placeholder=p.base_url?'':'https://your-gateway.example.com/v1';
  $('#fModelSel').innerHTML='<option value="">— 选择常用模型 —</option>'+p.models.map(m=>`<option>${esc(m)}</option>`).join('');
  $('#fModel').value=p.models[0]||'';$('#fKey').placeholder=p.needs_key===false?'本地无需 Key，可留空':(p.key_hint||'API Key');$('#test').className='testbox'}
 $('#presets').addEventListener('click',e=>{const b=e.target.closest('.preset');if(b)pick(b.dataset.p,false)});
 $('#fModelSel').onchange=e=>{if(e.target.value)$('#fModel').value=e.target.value};
 $('#eye').onclick=()=>{const k=$('#fKey'),s=k.type==='password';k.type=s?'text':'password';$('#eye').textContent=s?'隐藏':'显示'};
 function openModal(x){editing=x||null;$('#mTitle').textContent=x?'编辑 Provider':'添加 Provider';$('#presetBlock').hidden=!!x;$('#onRow').hidden=!!x;
  $('#test').className='testbox';$('#fKey').value='';$('#fKey').type='password';$('#eye').textContent='显示';
  if(x){pick(x.preset,true);$('#fName').value=x.name;$('#fUrl').value=x.base_url||'';$('#fModel').value=x.model;
   $('#fKey').placeholder=x.has_key?'留空表示不修改（当前 ••••'+(x.key_last4||'')+'）':(x.needs_key?'API Key':'本地无需 Key，可留空')}
  else{picked=null;$$('.preset').forEach(b=>b.setAttribute('aria-pressed','false'));['fName','fUrl','fModel'].forEach(i=>$('#'+i).value='');$('#fModelSel').innerHTML='<option value="">— 先选择预设 —</option>';$('#fOn').setAttribute('aria-checked','true')}
  $('#add').classList.add('open');setTimeout(()=>(x?$('#fName'):$('.preset')).focus(),50)}
 $('#addBtn').addEventListener('click',()=>setTimeout(()=>openModal(null)));
 const baseOf=()=>{const v=$('#fUrl').value.trim();return v===((P[picked]||{}).base_url||'')?'':v};
 $('#testBtn').onclick=async()=>{const t=$('#test');if(!picked||!$('#fModel').value.trim()){t.className='testbox show err';t.textContent='✕ 请先选择预设并填写模型';return}
  t.className='testbox show run';t.innerHTML='<span class="spin"></span>正在测试连接…';
  const b={preset:picked,model:$('#fModel').value.trim(),base_url:baseOf()};const k=$('#fKey').value.trim();if(k)b.api_key=k;if(editing)b.provider_id=editing.id;
  const [ok,j]=await api('POST','/api/me/llm/providers/test',b);
  if(ok&&j.ok){t.className='testbox show ok';t.textContent='✓ 连接成功 · '+(j.latency_ms/1000).toFixed(1)+'s · 模型 '+b.model+' 可用'}else{t.className='testbox show err';t.textContent='✕ '+(msg(j)||'连接失败')}};
 $('#saveBtn').onclick=async()=>{const name=$('#fName').value.trim(),model=$('#fModel').value.trim();if(!picked||!model){ND.toast('请选择预设并填写模型');return}
  const b={preset:picked,model,name,base_url:baseOf()};const k=$('#fKey').value.trim();if(k)b.api_key=k;
  if(!editing)b.enabled=$('#fOn').getAttribute('aria-checked')==='true';
  const [ok,j]=editing?await api('PATCH','/api/me/llm/providers/'+editing.id,b):await api('POST','/api/me/llm/providers',b);
  if(!ok)return fail(j,'保存失败');$('#add').classList.remove('open');ND.toast('已保存 '+(j.name||name));reload()};
 render();
})();
