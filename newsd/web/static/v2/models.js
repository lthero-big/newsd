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
 // fetch the provider's model list with the form's URL / key (or the saved key when editing)
 $('#fetchModels').onclick=async()=>{const h=$('#modelHint'),b=$('#fetchModels');if(!picked){ND.toast('请先选择预设');return}
  const body={preset:picked,base_url:baseOf()};const k=$('#fKey').value.trim();if(k)body.api_key=k;if(editing)body.provider_id=editing.id;
  b.disabled=true;h.innerHTML='<span class="spin"></span>正在向供应商查询模型…';
  const [ok,j]=await api('POST','/api/me/llm/providers/models',body);b.disabled=false;
  if(!ok||!j.ok){h.textContent='✕ 获取失败：'+(msg(j)||'未知错误')+'（可直接手填模型名）';return}
  if(!j.models.length){h.textContent='供应商没有返回模型，请手填模型名。';return}
  const cur=$('#fModel').value.trim();
  $('#fModelSel').innerHTML=`<option value="">— 供应商模型（${j.total} 个）—</option>`+j.models.map(m=>`<option${m===cur?' selected':''}>${esc(m)}</option>`).join('');
  h.textContent='✓ 已从供应商获取 '+j.total+' 个模型'+(j.total>j.models.length?'（显示前 '+j.models.length+' 个）':'')+'，在下拉里选择即可。'};
 // recent usage
 const tk=n=>{n=+n||0;return n>=1e6?(n/1e6).toFixed(1).replace(/\.0$/,'')+'M':n>=1e3?(n/1e3).toFixed(1).replace(/\.0$/,'')+'k':String(n)};
 const TRIG={manual:'手动',scheduled:'定时',api:'接口'};
 const when=s=>{const d=new Date(s);return isNaN(d)?'':d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})};
 async function usage(){const [ok,j]=await api('GET','/api/me/llm/usage?limit=20');const tb=$('#usageRows');if(!ok){tb.innerHTML='<tr><td colspan="9" class="muted">加载失败</td></tr>';return}
  $('#usageTotal').textContent=j.runs.length?`近 ${j.runs.length} 期 · ${tk(j.total.input_tokens)} 入 / ${tk(j.total.output_tokens)} 出`:'';
  tb.innerHTML=j.runs.map(r=>`<tr><td>${esc(when(r.at))}</td><td class="mono">${esc(r.key||'')}</td><td>${esc(TRIG[r.trigger]||r.trigger||'')}</td><td class="mono" title="${esc(r.chain||'')}">${esc(r.model||'')}</td>
   <td style="text-align:right">${r.requests||0}</td><td style="text-align:right"><b>${tk(r.input_tokens)}</b></td><td style="text-align:right"><b>${tk(r.output_tokens)}</b></td><td style="text-align:right">${r.seconds!=null?r.seconds+'s':''}</td>
   <td>${r.status==='ok'?'✓ '+(r.items_out??'')+' 条':'<span title="'+esc(r.error||'')+'">✕ 失败</span>'}</td></tr>`).join('')||'<tr><td colspan="9" class="muted">还没有用量记录：配置好模型后生成一次日报就会出现在这里。</td></tr>'}
 $('#usageReload').onclick=usage;usage();
 render();
})();
