/* 订阅源 (DEPTAI-216 3b): categories → zones → sources, backed by /api/me/zones. Every change is saved at once;
   drags update the page first and reload from the server if the save fails. Drag code: dnd.js (ND.sortable). */
(function(){
 const {$,$$,esc,api}=ND;
 let D=window.ND_ZONES||{cats:[]};
 const qs=new URLSearchParams(location.search);
 let cat=qs.get('cat')||((D.cats.find(c=>c.zones.length)||D.cats[0]||{}).key),dcat=cat,edit=null,pick=null;
 const C=k=>D.cats.find(c=>c.key===k)||{zones:[],catalog:[]};
 const cnt=k=>C(k).zones.reduce((a,z)=>a+z.sources.length,0);
 async function load(){const [ok,j]=await api('GET','/api/me/zones');if(ok){D=j;all()}}
 async function save(m,u,b,msg,undo){const [ok,j]=await api(m,u,b);if(!ok){ND.error(j,'保存失败');await load();return false}if(msg)ND.toast(msg,undo);return j}
 function cats(){$('#cats').innerHTML=D.cats.map(c=>`<button role="tab" type="button" aria-selected="${c.key===cat}" data-c="${c.key}" title="拖动可调整类别顺序（键盘：Alt + ↑ / ↓）"><i class="ci ci-${c.key}">${esc(c.icon)}</i>${esc(c.name)}<small>${cnt(c.key)}</small></button>`).join('')}
 function pane(){const c=C(cat),zs=c.zones;
  $('#pane').innerHTML=`<div class="row sp wrap-r" style="margin-bottom:14px"><div><b style="font-size:18px">${esc(c.name)}</b><div class="muted" style="font-size:13px">${esc(c.desc)} · ${zs.length} 个分区 · ${cnt(cat)} 个源</div></div>
   <div class="row"><button class="btn" data-a="newsec" type="button">＋ 新分区</button><button class="btn primary" data-a="add" type="button">＋ 添加 ${esc(c.name)} 源</button></div></div>
   <p class="dnd-hint">按住分区标题拖动可整体排序（拖动时分区会收成标题条）；按住一行源可拖到别的分区。键盘：聚焦 <kbd>⋮⋮</kbd> / <kbd>⠿</kbd> 按 <kbd>空格</kbd> 拿起，<kbd>↑</kbd><kbd>↓</kbd> 移动，<kbd>空格</kbd> 放下，<kbd>Esc</kbd> 取消。</p>`+
  (zs.length?'':`<div class="card muted" style="text-align:center;padding:34px">这个类别还没有分区。点「＋ 新分区」或「添加源」开始。${c.catalog.length?'':'<br><small>源目录里暂时没有这个类别的源。</small>'}</div>`)+
  zs.map((z,si)=>`<section class="sect${z.enabled?'':' off'}" data-si="${si}" data-zid="${z.id}"><header><button class="grip" type="button" aria-label="拖动分区「${esc(z.name)}」排序（空格拿起，↑↓ 移动）" title="拖动分区排序">⋮⋮</button><h3>${esc(z.name)}</h3>${z.special?'<span class="tag">特别关注 · 不受配额限制</span>':''}<span class="tag plain">${z.sources.length}</span><span style="flex:1"></span>
    <button class="btn icon sm" data-a="up" aria-label="分区上移" ${si?'':'disabled'} type="button">↑</button><button class="btn icon sm" data-a="down" aria-label="分区下移" ${si<zs.length-1?'':'disabled'} type="button">↓</button>
    <button class="btn sm ghost" data-a="pin" type="button">${z.special?'取消特别关注':'设为特别关注'}</button><button class="btn sm ghost" data-a="ren" type="button">重命名</button><button class="btn sm ghost danger" data-a="delsec" type="button">删除</button>
    <button class="switch" type="button" role="switch" aria-checked="${z.enabled}" data-a="secon" aria-label="启用分区 ${esc(z.name)}"></button></header>
    ${z.sources.map((x,ii)=>`<div class="srow${x.available?'':' gone'}" data-ii="${ii}" data-key="${esc(x.key)}"><button class="grip2" type="button" aria-label="拖动 ${esc(x.name)}，可换分区（空格拿起，↑↓ 移动）" title="拖动到别的分区">⠿</button><div><b>${esc(x.name)}</b><small>${esc(x.desc)}${x.available?'':' · 管理员已停用这个源'}</small></div><span class="w">权重 ${x.weight}</span><div class="row" style="gap:6px"><button class="btn sm ghost" data-a="try" type="button">最近</button><button class="btn sm ghost" data-a="edit" type="button">编辑</button><button class="btn sm ghost danger" data-a="del" type="button" aria-label="删除 ${esc(x.name)}">✕</button><button class="switch" type="button" role="switch" aria-checked="${x.enabled&&z.enabled}" ${z.enabled?'':'disabled'} data-a="on" aria-label="启用 ${esc(x.name)}"></button></div></div>`).join('')||'<div class="empty-s">空分区 — 把源拖到这里，或点「添加源」。</div>'}</section>`).join('');}
 const all=()=>{cats();pane()};
 const zone=si=>C(cat).zones[si];
 $('#cats').addEventListener('click',e=>{const b=e.target.closest('[data-c]');if(b){cat=b.dataset.c;history.replaceState(null,'','?cat='+cat);all()}});
 async function moveZone(fi,ti,msg){const zs=C(cat).zones,z=zs.splice(fi,1)[0];zs.splice(ti,0,z);all();
  const ok=await save('PUT','/api/me/zones/order?category='+cat,{ids:zs.map(x=>x.id)},msg?`「${z.name}」移到第 ${ti+1} 位`:'',msg?()=>moveZone(ti,fi,false):undefined);return ok}
 $('#pane').addEventListener('toggle',async e=>{const b=e.target.closest('[data-a]');if(!b)return;const sec=b.closest('.sect'),z=zone(+sec.dataset.si),on=b.getAttribute('aria-checked')==='true';
  if(b.dataset.a==='secon'){if(await save('PATCH','/api/me/zones/'+z.id,{enabled:on},(on?'已启用':'已停用')+'分区「'+z.name+'」'+(on?'':'：其中的源暂停抓取')))load()}
  else if(b.dataset.a==='on'){const x=z.sources[+b.closest('.srow').dataset.ii];if(await save('PATCH','/api/me/subscriptions/'+x.key,{enabled:on},(on?'已启用 ':'已停用 ')+x.name))x.enabled=on}});
 $('#pane').addEventListener('click',async e=>{const b=e.target.closest('[data-a]');if(!b||b.classList.contains('switch'))return;const a=b.dataset.a,sec=b.closest('.sect'),si=sec?+sec.dataset.si:-1,z=si>=0?zone(si):null,row=b.closest('.srow'),x=row&&z?z.sources[+row.dataset.ii]:null;
  switch(a){
   case 'newsec':return openZone();
   case 'add':return openDlg(cat);
   case 'up':return moveZone(si,si-1,true);
   case 'down':return moveZone(si,si+1,true);
   case 'pin':if(await save('PATCH','/api/me/zones/'+z.id,{special:!z.special},!z.special?'「'+z.name+'」设为特别关注：每条都推，不受配额限制':'已取消特别关注'))load();return;
   case 'ren':{const h=sec.querySelector('h3'),i=document.createElement('input');i.className='rn';i.value=z.name;i.maxLength=64;i.setAttribute('aria-label','分区名称');h.replaceWith(i);i.focus();i.select();
    let done=false;const fin=async keep=>{if(done)return;done=true;const v=i.value.trim();if(keep&&v&&v!==z.name){if(await save('PATCH','/api/me/zones/'+z.id,{name:v},'已重命名为「'+v+'」'))z.name=v}all()};
    i.onblur=()=>fin(true);i.onkeydown=ev=>{if(ev.key==='Enter'){ev.preventDefault();fin(true)}if(ev.key==='Escape'){ev.stopPropagation();fin(false)}};return}
   case 'delsec':if(z.sources.length&&!confirm(`删除分区「${z.name}」并取消订阅其中 ${z.sources.length} 个源？`))return;if(await save('DELETE','/api/me/zones/'+z.id,undefined,'已删除分区「'+z.name+'」'))load();return;
   case 'del':{const at=+row.dataset.ii;if(await save('DELETE','/api/me/subscriptions/'+x.key,undefined,'已取消订阅 '+x.name,async()=>{if(await save('POST',`/api/me/zones/${z.id}/sources`,{key:x.key,weight:x.weight,position:at},'已恢复 '+x.name))load()}))load();return}
   case 'try':return recent(x.key,x.name);
   case 'edit':return openDlg(cat,z,x);}});
 async function recent(key,name){const [ok,j]=await api('GET','/api/me/sources/recent?key='+encodeURIComponent(key));if(!ok)return ND.error(j);
  ND.toast(j.items.length?name+' 最近：'+j.items.map(i=>i.title).join(' / ').slice(0,160):name+'：池子里还没有这个源的条目（下次采集后会有）')}
 // drag & drop: categories, whole zones, sources across zones (pointer or keyboard)
 const P=$('#pane'),snap=()=>JSON.stringify(D),restore=j=>{D=JSON.parse(j);all()};
 const secEl=si=>P.querySelector(`.sect[data-si="${si}"]`);
 ND.sortable({root:$('#cats'),lists:()=>[$('#cats')],item:'[data-c]',handle:'[data-c]',kbd:'[data-c]',altKeys:true,tilt:0,
  label:el=>C(el.dataset.c).name,listName:()=>'类别',
  onDrop:({el,fromIndex:fi,toIndex:ti})=>{const [c]=D.cats.splice(fi,1);D.cats.splice(ti,0,c);cats();
   save('PUT','/api/me/source-cats',{order:D.cats.map(c=>c.key)},'类别顺序已更新：'+D.cats.map(c=>c.name).join(' · '));return $(`#cats [data-c="${c.key}"]`)}});
 ND.sortable({root:P,lists:()=>[P],item:'.sect',handle:'.sect>header',grip:'.grip',kbd:'.grip',ignore:'button,input,select,a,.switch',tilt:.6,
  label:el=>'分区「'+zone(+el.dataset.si).name+'」',listName:()=>C(cat).name,snapshot:snap,restore:j=>{restore(j);return null},
  onStart:()=>P.classList.add('zones-compact'),onEnd:()=>P.classList.remove('zones-compact'),
  onDrop:({fromIndex:fi,toIndex:ti,kbd})=>{moveZone(fi,ti,!kbd);return secEl(ti)}});
 ND.sortable({root:P,lists:()=>$$('.sect',P),item:'.srow',handle:'.srow',grip:'.grip2',kbd:'.grip2',ignore:'button,input,select,a,.switch',cross:true,
  label:el=>zone(+el.closest('.sect').dataset.si).sources[+el.dataset.ii].name,listName:l=>zone(+l.dataset.si).name,snapshot:snap,restore:j=>{restore(j);return null},
  onDrop:({from,fromIndex:fi,to,toIndex:ti,kbd})=>{const fs=+from.dataset.si,ts=+to.dataset.si,F=zone(fs),T=zone(ts),x=F.sources.splice(fi,1)[0];T.sources.splice(ti,0,x);all();
   (async()=>{if(!await save('PUT',`/api/me/zones/${T.id}/sources`,{keys:T.sources.map(s=>s.key)}))return;
    if(fs!==ts){if(!await save('PUT',`/api/me/zones/${F.id}/sources`,{keys:F.sources.map(s=>s.key)}))return;
     if(!kbd)ND.toast(`${x.name} → 「${T.name}」`,async()=>{T.sources.splice(T.sources.indexOf(x),1);F.sources.splice(fi,0,x);all();
      if(await save('PUT',`/api/me/zones/${F.id}/sources`,{keys:F.sources.map(s=>s.key)}))save('PUT',`/api/me/zones/${T.id}/sources`,{keys:T.sources.map(s=>s.key)})})}})();
   return secEl(ts).querySelector(`.srow[data-ii="${ti}"]`)}});
 // new zone
 function openZone(){$('#zName').value='';$('#zSpecial').setAttribute('aria-checked','false');$('#zT').textContent='新建「'+C(cat).name+'」分区';$('#zdlg').classList.add('open');setTimeout(()=>$('#zName').focus(),50)}
 $('#zSave').onclick=async()=>{const n=$('#zName').value.trim();if(!n)return ND.toast('请填写分区名称');
  if(await save('POST','/api/me/zones',{category:cat,name:n,special:$('#zSpecial').getAttribute('aria-checked')==='true'},'已新建分区「'+n+'」')){$('#zdlg').classList.remove('open');load()}};
 $('#zName').addEventListener('keydown',e=>{if(e.key==='Enter')$('#zSave').click()});
 // add / edit dialog
 function list(){const q=$('#dQ').value.trim().toLowerCase(),items=C(dcat).catalog.filter(s=>!q||(s.name+' '+s.key+' '+s.desc).toLowerCase().includes(q));
  $('#dList').innerHTML=items.slice(0,80).map(s=>`<button type="button" role="option" class="cpick${pick===s.key?' on':''}" aria-selected="${pick===s.key}" data-k="${esc(s.key)}"><b>${esc(s.name)}</b><small>${esc(s.desc)}</small></button>`).join('')||`<p class="muted" style="font-size:13px;margin:8px 0">${C(dcat).catalog.length?'没有匹配的源。':'这个类别的源都已经订阅了。'}</p>`}
 function fields(){$$('#dCats button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.c===dcat)));
  const zs=C(dcat).zones;$('#dSec').innerHTML=zs.map(z=>`<option value="${z.id}">${esc(z.name)}${z.special?'（特别关注）':''}</option>`).join('')||'<option value="">（还没有分区，将新建「默认」）</option>';$('#dTest').className='testbox';list()}
 $('#dCats').innerHTML=D.cats.map(c=>`<button type="button" data-c="${c.key}" aria-pressed="false"><i class="ci ci-${c.key}">${esc(c.icon)}</i>${esc(c.name)}</button>`).join('');
 $('#dCats').addEventListener('click',e=>{const b=e.target.closest('[data-c]');if(b&&!edit){dcat=b.dataset.c;pick=null;$('#dQ').value='';fields()}});
 $('#dList').addEventListener('click',e=>{const b=e.target.closest('[data-k]');if(b){pick=b.dataset.k;list()}});
 $('#dQ').addEventListener('input',list);
 function openDlg(c,z,x){dcat=c;edit=x?{z,x}:null;pick=x?x.key:null;$('#dQ').value='';fields();
  $('#dT').textContent=edit?'编辑源':'添加源';$('#dSave').textContent=edit?'保存':'添加';$('#dStep1').hidden=!!edit;$('#dPick').hidden=!!edit;$('#dEditName').hidden=!edit;
  $('#dL2').hidden=!!edit;$('#dL3').textContent=edit?'分区':'3 · 放进分区';
  if(edit){$('#dName').textContent=x.name;$('#dDesc').textContent=x.desc;$('#dSec').value=z.id;$('#dW').value=x.weight}else $('#dW').value='0';
  $('#dNewSec').value='';$('#dlg').classList.add('open');setTimeout(()=>(edit?$('#dSec'):$('#dQ')).focus(),50)}
 $('#addSrc').onclick=()=>openDlg(cat);
 $('#dTry').onclick=async()=>{const t=$('#dTest');if(!pick){t.className='testbox show err';t.textContent='✕ 先在目录里选一个源';return}
  t.className='testbox show run';t.innerHTML='<span class="spin"></span>读取中…';const [ok,j]=await api('GET','/api/me/sources/recent?key='+encodeURIComponent(pick));
  if(!ok){t.className='testbox show err';t.textContent='✕ '+(j.error||'读取失败');return}
  t.className='testbox show ok';t.innerHTML=j.items.length?'✓ 池子里最近 '+j.items.length+' 条<br>· '+j.items.map(i=>esc(i.title)).join('<br>· '):'池子里还没有这个源的条目；订阅后下次采集就会抓取。'};
 $('#dSave').onclick=async()=>{if(!pick)return ND.toast('请先选一个源');let zid=$('#dSec').value;const ns=$('#dNewSec').value.trim(),w=+$('#dW').value;
  if(ns||!zid){const j=await save('POST','/api/me/zones',{category:dcat,name:ns||'默认'});if(!j)return;zid=j.id}
  let ok;if(edit){ok=await save('PATCH','/api/me/subscriptions/'+edit.x.key,{zone_id:+zid,weight:w},'已保存 '+edit.x.name)}
  else{const s=C(dcat).catalog.find(s=>s.key===pick);ok=await save('POST',`/api/me/zones/${zid}/sources`,{key:pick,weight:w},'已添加 '+(s?s.name:pick))}
  if(ok){$('#dlg').classList.remove('open');cat=dcat;await load()}};
 $('#applyDefault').onclick=async()=>{if(await save('POST','/api/me/subscriptions/apply-template',undefined,'已套用推荐订阅'))load()};
 all();
})();
