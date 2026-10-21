/* 3c 源健康: counts, problems first (retry / logs / disable), compact per-category cards with a detail table. */
(function(){
 const {$,$$,esc,api}=ND;let D=window.ND_HEALTH,H=D.sources,C=D.cats,open=new URLSearchParams(location.search).get('cat'),f='all';
 const RANK={err:0,warn:1,off:2,ok:3},SN={ok:'正常',warn:'警告',err:'失败',off:'停用'},SC={ok:'var(--ok)',warn:'var(--warn)',err:'var(--danger)',off:'var(--line)'};
 const cls=s=>({warn:'w',err:'e',off:'o'}[s]||''),cn=k=>(C[k]||{name:k}).name,pct=v=>v==null?'—':Math.round(v*100)+'%';
 const spark=h=>`<span class="spark" aria-label="14 天历史">${h.hist.map(x=>`<i class="${x==='n'?'':x}" style="height:${x==='e'?4:x==='w'?10:x==='n'?2:16}px${x==='n'?';opacity:.35':''}"></i>`).join('')}</span>`;
 const when=t=>t?t.slice(5,16).replace('T',' ')+' UTC':'—';
 function sum(){$('#chk').textContent='最近抓取 '+when(D.checked_at);
  $('#sum').innerHTML=['err','warn','ok','off'].map(s=>`<span class="pill"><i style="background:${SC[s]}"></i>${SN[s]} <b>${H.filter(h=>h.st===s).length}</b></span>`).join('')+`<span class="muted mono" style="font-size:12px">共 ${H.length} 个源 · ${new Set(H.map(h=>h.cat)).size} 类</span>`}
 function probs(){const P=H.filter(h=>h.st==='err'||h.st==='warn').sort((a,b)=>RANK[a.st]-RANK[b.st]);$('#pN').textContent=P.length;$('#probBox').hidden=!P.length;
  $('#probs').innerHTML=P.map(h=>`<div class="prob" data-k="${esc(h.key)}"><span class="tag ${h.st==='err'?'err':'warn'}">${SN[h.st]}</span><div style="min-width:0"><b>${esc(h.name)}</b><small>${esc(cn(h.cat))} · ${h.users} 人订阅 · ${esc(h.key)}</small></div><div class="err ${h.st==='warn'?'w':''}" style="min-width:0;overflow-wrap:anywhere">${esc(h.note)} ${spark(h)}</div>
   <div class="row" style="gap:6px"><button class="btn sm" data-a="retry" type="button" ${h.users?'':'disabled title="无人订阅，不抓取"'}>重试</button><a class="btn sm ghost" href="/admin/logs?src=${encodeURIComponent(h.key)}&time=7d&level=INFO,WARN,ERROR">日志</a><button class="btn sm ghost" data-a="off" type="button">停用</button></div></div>`).join('')}
 function grid(){const cats=[...new Set(H.map(h=>h.cat))].map(k=>({k,L:H.filter(h=>h.cat===k)})).map(c=>({...c,bad:c.L.filter(h=>h.st==='err'||h.st==='warn').length,worst:Math.min(...c.L.map(h=>RANK[h.st]))}))
   .sort((a,b)=>a.worst-b.worst||b.L.length-a.L.length).filter(c=>f==='all'||c.bad);
  let html='';cats.forEach(c=>{const L=[...c.L].sort((a,b)=>RANK[a.st]-RANK[b.st]||a.name.localeCompare(b.name)),ok=L.filter(h=>h.st==='ok').length,off=L.filter(h=>h.st==='off').length;
   html+=`<section class="card hcat" tabindex="0" role="button" aria-expanded="${open===c.k}" data-c="${c.k}"><div class="t"><span class="tag plain">${esc(cn(c.k))}</span>${esc((C[c.k]||{}).icon||'')}<small>${L.length} 个</small></div>
    <div class="hdots">${L.map(h=>`<i class="${cls(h.st)}" title="${esc(h.name)} · ${SN[h.st]}"></i>`).join('')}</div>
    <div class="nums"><span><b>${ok}</b> 正常</span>${c.bad?`<span style="color:var(--danger)"><b style="color:inherit">${c.bad}</b> 异常</span>`:''}${off?`<span><b>${off}</b> 停用</span>`:''}<span>24h <b>${L.reduce((a,h)=>a+h.items,0)}</b> 条</span></div></section>`;
   if(open===c.k)html+=`<section class="card hdetail scroll-x"><table><tr><th>源</th><th>状态</th><th>14 天</th><th>24h 条目</th><th>耗时</th><th>订阅</th><th>30 天入选率</th><th>正文抽取</th><th>启用</th><th>说明</th></tr>${L.map(h=>`<tr data-k="${esc(h.key)}"><td><b>${esc(h.name)}</b><div class="muted mono" style="font-size:11px">${esc(h.key)}</div></td><td><span class="st-dot ${h.st==='ok'?'':h.st==='off'?'off':'inv'}"><i ${h.st==='err'?'style="background:var(--danger)"':''}></i>${SN[h.st]}</span></td><td>${spark(h)}</td><td class="mono">${h.items}</td><td class="mono">${h.lat==null?'—':h.lat+'s'}</td><td class="mono">${h.users}</td><td class="mono" title="入选 ${h.selected} / 采集 ${h.collected}">${pct(h.pass_rate)}</td>
    <td><button class="switch" type="button" role="switch" aria-checked="${h.content_extractor}" data-x="ce" aria-label="正文抽取"></button></td><td><button class="switch" type="button" role="switch" aria-checked="${h.enabled}" data-x="en" aria-label="启用"></button></td><td class="muted" style="font-size:12px">${esc(h.note||'—')}${h.last_status?`<div class="mono">${esc(h.last_status)} · ${when(h.last_at)}</div>`:''}</td></tr>`).join('')}</table></section>`});
  $('#grid').innerHTML=html||'<p class="empty">所有类别都正常 🎉</p>'}
 const all=()=>{sum();probs();grid()};
 async function refresh(){const [ok,j]=await api('GET','/api/admin/health');if(ok){D=j;H=j.sources;all()}}
 async function patch(key,body,msg,undo){const [ok,j]=await api('PATCH','/api/admin/sources/'+key,body);if(!ok){ND.error(j);await refresh();return false}await refresh();ND.toast(msg,undo);return true}
 $('#grid').addEventListener('click',e=>{const c=e.target.closest('.hcat');if(c){open=open===c.dataset.c?null:c.dataset.c;history.replaceState(null,'',open?'?cat='+open:location.pathname);grid()}});
 $('#grid').addEventListener('toggle',e=>{const sw=e.target.closest('[data-x]'),k=e.target.closest('tr[data-k]').dataset.k,on=sw.getAttribute('aria-checked')==='true';
  if(sw.dataset.x==='en')patch(k,{enabled:on},on?'已启用':'已停用 · 用户订阅保留，但不再抓取',on?null:()=>patch(k,{enabled:true},'已恢复'));else patch(k,{content_extractor:on},on?'已开启正文抽取':'已关闭正文抽取')});
 $('#grid').addEventListener('keydown',e=>{if((e.key==='Enter'||e.key===' ')&&e.target.matches('.hcat')){e.preventDefault();e.target.click()}});
 $('#hf').addEventListener('click',e=>{const b=e.target.closest('[data-f]');if(!b)return;f=b.dataset.f;$$('#hf [data-f]').forEach(x=>x.setAttribute('aria-selected',x===b));grid()});
 $('#probs').addEventListener('click',async e=>{const b=e.target.closest('[data-a]');if(!b)return;const k=b.closest('.prob').dataset.k,h=H.find(x=>x.key===k);
  if(b.dataset.a==='retry'){b.disabled=true;b.textContent='抓取中…';const [ok,j]=await api('POST','/api/admin/health/retry',{key:k});
   if(!ok){ND.error(j,'重试失败');b.disabled=false;b.textContent='重试';return}await refresh();ND.toast(j.status==='ok'?`${h.name} 抓取成功 · 新增 ${j.new_items} 条`:`${h.name} 仍然失败：${j.status}`)}
  else patch(k,{enabled:false},'已停用 '+h.name,()=>patch(k,{enabled:true},'已恢复 '+h.name))});
 $('#recheck').onclick=async()=>{const [ok,j]=await api('POST','/api/admin/health/recheck');if(!ok)return ND.error(j);ND.toast('已在后台重新抓取所有被订阅的源，稍后刷新查看');setTimeout(refresh,15000)};
 $('#addRss').onclick=()=>{['rId','rName','rUrl'].forEach(k=>$('#'+k).value='');$('#rdlg').classList.add('open');setTimeout(()=>$('#rId').focus(),50)};
 $('#rSave').onclick=async()=>{const [ok,j]=await api('POST','/api/admin/sources',{id:$('#rId').value.trim(),name:$('#rName').value.trim(),url:$('#rUrl').value.trim()});
  if(!ok)return ND.toast('添加失败：'+(j.error||(j.detail&&j.detail[0]&&j.detail[0].msg)||''));$('#rdlg').classList.remove('open');open='rss';await refresh();ND.toast('已添加 '+j.key)};
 all();
})();
