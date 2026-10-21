/* 回收站: restore (removes the learning signal), purge (keeps it unless 「同时撤销」), bulk select, undo via /redelete. */
(function(){
 const {$,$$,api}=ND,sel=new Set(),list=$('#tlist');
 const rows=()=>$$('.trow',list),live=()=>rows().filter(r=>!r.hidden&&!r.dataset.gone);
 function paint(){const q=$('#tq').value.trim().toLowerCase();
  rows().forEach(r=>{r.hidden=!!r.dataset.gone||(!!q&&!r.dataset.q.includes(q));$('input',r).checked=sel.has(r.dataset.id)});
  [...sel].forEach(id=>{const r=$(`.trow[data-id="${id}"]`,list);if(!r||r.dataset.gone)sel.delete(id)});
  const n=rows().filter(r=>!r.dataset.gone).length;$('#tn').textContent=n+' 条';$('#empty').hidden=!!live().length;$('#restoreAll').disabled=$('#emptyBtn').disabled=!n;
  $('#bulk').hidden=!sel.size;$('#bn').textContent=`已选 ${sel.size} 条`;const v=live();$('#selAll').checked=!!v.length&&v.every(r=>sel.has(r.dataset.id))}
 const gone=(ids,on)=>{ids.forEach(id=>{const r=$(`.trow[data-id="${id}"]`,list);if(r){if(on)r.dataset.gone='1';else delete r.dataset.gone}});ND.paintTrash(on?-ids.length:ids.length);paint()};
 const ids=x=>x.map(Number);
 async function restore(x){const [ok,j]=await api('POST','/api/me/trash/restore',{ids:x==null?null:ids(x)});if(!ok){ND.error(j);return null}gone(j.restored.map(String),true);return j.restored}
 async function redelete(x){const [ok,j]=await api('POST','/api/me/trash/redelete',{ids:ids(x)});if(!ok){ND.error(j);return}gone(x.map(String),false)}
 async function purge(x,forget){const [ok,j]=await api('POST','/api/me/trash/purge',{ids:ids(x),forget});if(!ok){ND.error(j);return false}gone(x,true);return true}
 list.addEventListener('click',async e=>{const r=e.target.closest('.trow');if(!r)return;const id=r.dataset.id;
  if(e.target.matches('input[type=checkbox]')){e.target.checked?sel.add(id):sel.delete(id);paint();return}
  const a=e.target.closest('[data-a]');if(!a)return;
  if(a.dataset.a==='restore'){if(await restore([id]))ND.toast('已恢复到日报，相关降权已撤销',()=>redelete([id]))}
  else if(await purge([id],false))ND.toast('已彻底删除（降权保留）',()=>redelete([id]))});
 $('#selAll').onchange=e=>{live().forEach(r=>e.target.checked?sel.add(r.dataset.id):sel.delete(r.dataset.id));paint()};
 $('#bRestore').onclick=async()=>{const x=[...sel];const done=await restore(x);if(done){sel.clear();paint();ND.toast('已恢复 '+done.length+' 条',()=>redelete(done.map(String)))}};
 let pending=[];const ask=(x,title)=>{pending=x;$('#cT').textContent=title;$('#cB').textContent=`将永久删除 ${x.length} 条，无法恢复。`;$('#cForget').checked=false;$('#confirm').classList.add('open')};
 $('#bPurge').onclick=()=>ask([...sel],'彻底删除所选？');$('#emptyBtn').onclick=()=>ask(rows().filter(r=>!r.dataset.gone).map(r=>r.dataset.id),'清空回收站？');
 $('#cOk').onclick=async()=>{const f=$('#cForget').checked,x=pending;$('#confirm').classList.remove('open');if(await purge(x,f)){sel.clear();paint();ND.toast(`已彻底删除 ${x.length} 条`+(f?'，并撤销了相关降权':''))}};
 $('#restoreAll').onclick=async()=>{const done=await restore(null);if(done)ND.toast('已全部恢复',()=>redelete(done.map(String)))};
 $('#tq').oninput=paint;paint();
})();
