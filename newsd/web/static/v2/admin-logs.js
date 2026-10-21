/* 3c 日志与任务: server-side filtered events (level / module / source / run / user / time / query syntax),
   histogram (click a bar = that slice), run timeline (gantt of stages), expandable failure details, drawer,
   live follow (polling), export JSON, copy filter link. All times are UTC. */
(function(){
 const {$,$$,esc,api}=ND,I=window.ND_LOGS,LV=['DEBUG','INFO','WARN','ERROR'],q0=new URLSearchParams(location.search);
 const MODN={scheduler:'调度',fetch:'抓取',compose:'日报',llm:'模型',push:'推送',auth:'登录',admin:'管理',api:'接口'},STAGE={collect:['采集','fetch'],issues:['日报','compose']};
 const F={lv:new Set((q0.get('level')||'INFO,WARN,ERROR').split(',').filter(l=>LV.includes(l))),bucket:null,open:new Set(),items:[],more:false,hist:null,since:null,until:null,levels:{}};
 if(!F.lv.size)F.lv=new Set(['INFO','WARN','ERROR']);
 let runs=I.timeline.runs,facets=null,cur=null,seq=0;
 const fmt=t=>t?t.slice(5,19).replace('T',' '):'—',ms=t=>Date.parse(t+'Z'),iso=n=>new Date(n).toISOString().slice(0,19);
 const opt=(sel,vals,keep)=>{const v=keep||$(sel).value;$(sel).innerHTML=$(sel).options[0].outerHTML+vals.map(([k,n])=>`<option value="${esc(k)}">${esc(n)}</option>`).join('');if(v&&![...$(sel).options].some(o=>o.value===v))$(sel).insertAdjacentHTML('beforeend',`<option value="${esc(v)}">${esc(v)}</option>`);$(sel).value=v||''};
 opt('#fMod',Object.entries(MODN).map(([k,n])=>[k,n+' · '+k]),q0.get('mod'));
 [['fSrc','src'],['fRun','run'],['fUser','user']].forEach(([k,n])=>{const v=q0.get(n);if(v)opt('#'+k,[],v)});
 if(q0.get('time'))$('#fTime').value=q0.get('time');if(q0.get('q'))$('#q').value=q0.get('q');
 const nowUtc=()=>Date.now();
 function range(){const v=$('#fTime').value,n=nowUtc(),d0=new Date();d0.setUTCHours(0,0,0,0);
  if(v==='custom'){const a=$('#tFrom').value,b=$('#tTo').value;return [a?a+':00':iso(n-864e5),b?b+':00':iso(n)]}
  const back={'1h':36e5,'24h':864e5,'7d':7*864e5,'30d':30*864e5}[v];return [iso(v==='today'?d0.getTime():n-back),iso(n)]}
 function params(withBucket){const [a,b]=range(),p=new URLSearchParams();let s=a,u=b;
  if(withBucket&&F.bucket!=null&&F.hist){const st=F.hist.step*1000,base=ms(F.since);s=iso(base+F.bucket*st);u=iso(base+(F.bucket+1)*st)}
  p.set('since',s);p.set('until',u);p.set('level',[...F.lv].join(','));if($('#q').value.trim())p.set('q',$('#q').value.trim());
  [['fMod','module'],['fSrc','source'],['fRun','run'],['fUser','user']].forEach(([k,n])=>{if($('#'+k).value)p.set(n,$('#'+k).value)});return p}
 async function fetchLogs(append){const my=++seq,p=params(true);p.set('limit','150');if(append&&F.items.length)p.set('before',F.items[F.items.length-1].id);
  const [ok,j]=await api('GET','/api/admin/logs?'+p);if(my!==seq)return;if(!ok){ND.error(j,'加载日志失败');return}
  F.items=append?F.items.concat(j.items):j.items;F.more=j.more;F.total=j.total;
  if(F.bucket==null){F.hist=j.hist;F.since=j.since;F.until=j.until;F.levels=j.levels}
  if(!facets||!append){facets=j.facets;opt('#fSrc',facets.sources.map(s=>[s,s]));opt('#fUser',facets.users.map(u=>[u,u]));
   opt('#fRun',facets.runs.map(r=>[String(r.id),`#${r.id} · ${fmt(r.started_at).slice(0,11)} · ${r.trigger}${r.status==='failed'?' ✕':r.status==='partial'?' !':r.status==='running'?' …':''}`]))}
  render()}
 const hl=s=>{let h=esc(s);$('#q').value.trim().split(/\s+/).filter(w=>w&&!/^(run|src|user|mod|level):/i.test(w)).forEach(w=>{const e=esc(w).replace(/[.*+?^${}()|[\]\\]/g,'\\$&');h=h.replace(new RegExp(e,'gi'),x=>`<mark>${x}</mark>`)});return h};
 const hasErr=l=>l.detail&&(l.detail.stack||l.detail.errors);
 function details(l){const d=l.detail||{},rest=Object.fromEntries(Object.entries(d).filter(([k])=>k!=='stack'&&k!=='errors'));
  return `${d.errors?`<pre>${esc(d.errors)}</pre>`:''}${d.stack?`<pre>${esc(d.stack)}</pre>`:''}${Object.keys(rest).length?`<pre>${esc(JSON.stringify(rest,null,2))}</pre>`:''}`}
 function render(){
  $('#lvls').innerHTML=LV.map(l=>`<button class="lvl L-${l}" type="button" aria-pressed="${F.lv.has(l)}" data-lv="${l}"><i></i>${l} <span class="muted">${F.levels[l]||0}</span></button>`).join('');
  const H=F.hist,B=H?H.buckets:[],mx=Math.max(1,...B.map(x=>LV.reduce((s,k)=>s+(x[k]||0),0))),st=H?H.step*1000:0,base=F.since?ms(F.since):0;
  $('#hist').innerHTML=B.map((x,i)=>`<div class="b${F.bucket===i?' on':''}" data-i="${i}" title="${fmt(iso(base+i*st))} – ${fmt(iso(base+(i+1)*st))} · ${LV.map(k=>k+' '+(x[k]||0)).join(' / ')}">${LV.map(k=>`<i class="L-${k}" style="height:${(x[k]||0)/mx*60}px"></i>`).join('')}</div>`).join('');
  $('#histAx').innerHTML=`<span>${fmt(F.since)}</span><span>${F.bucket!=null?'已选时段 · <a href="#" id="clrH" style="color:var(--accent)">清除</a>':'点柱子只看该时段'}</span><span>${fmt(F.until)}</span>`;
  $('#cnt').textContent=F.total+' 条';$('#more').style.display=F.more?'':'none';$('#more').textContent=`加载更多（已显示 ${F.items.length} / ${F.total}）`;
  $('#rows').innerHTML=F.items.map(l=>`<tr class="r ${l.level}${cur&&cur.id===l.id?' sel':''}" data-id="${l.id}"><td>${hasErr(l)?`<button class="xbtn" data-x="${l.id}" aria-expanded="${F.open.has(l.id)}" aria-label="失败详情" type="button">${F.open.has(l.id)?'▾':'▸'}</button>`:''}</td><td class="ts">${fmt(l.at)}</td><td><span class="lv L-${l.level}"><i></i>${l.level}</span></td><td class="c-mod"><span class="mod">${MODN[l.module]||esc(l.module)}</span></td><td class="mono" style="font-size:12px">${l.source?esc(l.source):'<span class="muted">—</span>'}</td><td class="mono" style="font-size:12px">${l.run?`<a href="#" data-run="${l.run}" style="color:var(--accent)">#${l.run}</a>`:'<span class="muted">—</span>'}</td><td class="c-user" style="font-size:12px">${esc(l.user||'system')}</td><td class="msg">${hl(l.message)}</td></tr>`+
   (hasErr(l)&&F.open.has(l.id)?`<tr class="detail"><td colspan="8">${details(l)}<div class="row" style="margin-top:8px"><button class="btn sm" data-open-dw="${l.id}" type="button">在侧栏查看</button></div></td></tr>`:'')).join('')||'<tr><td colspan="8" class="empty">没有符合筛选的日志。试试放宽时间或级别。</td></tr>';
  timeline();syncUrl()}
 function timeline(){const maxD=Math.max(1,...runs.map(r=>r.seconds||((Date.now()-ms(r.started_at))/1000)));
  $('#runs').innerHTML=runs.map(r=>{const t0=ms(r.started_at),dur=r.seconds!=null?r.seconds:(Date.now()-t0)/1000;
   const segs=(r.stages||[]).map(s=>{const a=(ms(s.started_at)-t0)/1000,b=s.finished_at?(ms(s.finished_at)-t0)/1000:dur,[n,mod]=STAGE[s.stage]||[s.stage,''];
    const cls=s.status==='ok'?'ok':s.status==='running'?'run':s.status==='partial'?'warn':'err';
    return `<span class="${cls}" data-stage="${mod}" style="left:${a/maxD*100}%;width:${Math.max(3,(b-a)/maxD*100)}%" title="${esc(n)} · ${Math.round(b-a)}s · ${esc(s.status)}${s.error?' · '+esc(s.error):''}">${esc(n)}</span>`}).join('');
   const tag=r.status==='ok'?'<span class="tag ok">完成</span>':r.status==='partial'?'<span class="tag warn">部分失败</span>':r.status==='running'?'<span class="tag"><span class="spin"></span>运行中</span>':r.status==='interrupted'?'<span class="tag plain">中断</span>':'<span class="tag err">失败</span>';
   const items=(r.users||[]).reduce((a,u)=>a+(u.items_out||0),0);
   return `<div class="run" role="button" tabindex="0" data-run="${r.id}" aria-pressed="${$('#fRun').value===String(r.id)}"><div class="id"><b>#${r.id} · ${fmt(r.started_at).slice(0,11)}</b>${esc(r.issue_key)} · ${r.trigger==='schedule'?'定时':r.trigger==='cron'?'定时接口':r.trigger==='manual'?'手动':esc(r.trigger)}</div>
    <div class="gantt">${segs}</div><div class="st">${tag}<br><span class="muted">${r.users.length} 人 · ${items} 条 · ${Math.round(dur)}s${r.error_count?' · '+r.error_count+' 错误':''}</span></div></div>`}).join('')||'<p class="muted">还没有任务运行记录。worker 到点会自动运行。</p>'}
 function syncUrl(){const p=new URLSearchParams();p.set('level',[...F.lv].join(','));[['fMod','mod'],['fSrc','src'],['fRun','run'],['fUser','user']].forEach(([k,n])=>{if($('#'+k).value)p.set(n,$('#'+k).value)});p.set('time',$('#fTime').value);if($('#q').value.trim())p.set('q',$('#q').value.trim());history.replaceState(null,'','?'+p)}
 const reload=()=>{F.bucket=null;fetchLogs(false)};
 // drawer
 const dw=$('#drawer');
 async function openDw(id){const l=F.items.find(x=>x.id===+id);if(!l)return;cur=l;
  $('#dwLv').innerHTML=`<span class="lv L-${l.level}"><i></i>${l.level}</span>`;$('#dwT').textContent='#'+l.id+' · '+(MODN[l.module]||l.module);
  const body=ctx=>`<p style="font:14px/1.5 var(--font-mono);margin:0 0 12px;overflow-wrap:anywhere">${esc(l.message)}</p><dl class="kv"><dt>时间</dt><dd>${esc((l.at||'').replace('T',' ').slice(0,19))} UTC</dd><dt>模块</dt><dd>${esc(l.module)}</dd><dt>源</dt><dd>${esc(l.source||'—')}</dd><dt>任务</dt><dd>${l.run?'#'+l.run:'—'}</dd><dt>用户</dt><dd>${esc(l.user||'system')}</dd></dl>
   ${Object.keys(l.detail||{}).length?`<h4>详情</h4>${details(l)}`:''}${ctx&&ctx.length>1?`<h4>同一任务前后</h4><ul class="rel">${ctx.map(x=>`<li${x.id===l.id?' style="font-weight:600"':''}><span class="lv L-${x.level}"><i></i>${x.level[0]}</span><span class="muted">${fmt(x.at).slice(6)}</span><span>${esc(x.message)}</span></li>`).join('')}</ul>`:''}`;
  $('#dwB').innerHTML=body(null);$('#dwRun').disabled=!l.run;$('#dwSrc').disabled=!l.source;
  if($('#dwRetry')){$('#dwRetry').textContent=l.source?'重抓此源':l.run?'重新运行':'重跑';$('#dwRetry').disabled=!(l.source||l.run)}
  dw.classList.add('open');dw.setAttribute('aria-hidden','false');$$('#rows tr.r').forEach(t=>t.classList.toggle('sel',+t.dataset.id===l.id));
  if(l.run){const [ok,j]=await api('GET','/api/admin/logs/'+l.id);if(ok&&cur===l)$('#dwB').innerHTML=body(j.context)}}
 const closeDw=()=>{dw.classList.remove('open');dw.setAttribute('aria-hidden','true');cur=null;$$('#rows tr.sel').forEach(t=>t.classList.remove('sel'))};
 const step=d=>{const ids=$$('#rows tr.r').map(t=>+t.dataset.id),i=ids.indexOf(cur?.id);if(ids[i+d])openDw(ids[i+d])};
 $('#dwX').onclick=closeDw;$('#dwPrev').onclick=()=>step(-1);$('#dwNext').onclick=()=>step(1);
 document.addEventListener('keydown',e=>{if(!dw.classList.contains('open')||e.target.matches('input,select,textarea'))return;if(e.key==='Escape')closeDw();if(e.key==='j')step(1);if(e.key==='k')step(-1)});
 $('#dwCopy').onclick=()=>{navigator.clipboard?.writeText(JSON.stringify(cur,null,2)).then(()=>ND.toast('已复制 JSON'),()=>ND.toast('复制失败'))};
 const onlyRun=id=>{opt('#fRun',facets?facets.runs.map(r=>[String(r.id),'#'+r.id]):[],String(id));$('#fRun').value=String(id);$('#fTime').value='7d';reload()};
 $('#dwRun').onclick=()=>{onlyRun(cur.run);ND.toast('只看任务 #'+cur.run)};
 $('#dwSrc').onclick=()=>{opt('#fSrc',facets?facets.sources.map(s=>[s,s]):[],cur.source);$('#fSrc').value=cur.source;reload();ND.toast('只看 '+cur.source)};
 async function runNow(){if(!confirm('现在完整运行一次（采集全部源并给每个用户生成日报）？'))return;const [ok,j]=await api('POST','/api/admin/runs/trigger');if(!ok)return ND.error(j,'无法开始');ND.toast('已开始运行，时间线会自动刷新');pollRuns(true)}
 if($('#dwRetry'))$('#dwRetry').onclick=async()=>{if(cur.source){const b=$('#dwRetry');b.disabled=true;b.textContent='抓取中…';const [ok,j]=await api('POST','/api/admin/health/retry',{key:cur.source});b.disabled=false;b.textContent='重抓此源';if(!ok)return ND.error(j,'重抓失败');ND.toast(j.status==='ok'?`抓取成功 · 新增 ${j.new_items} 条`:'仍然失败：'+j.status);reload()}else if(cur.run)runNow()};
 if($('#runNow'))$('#runNow').onclick=runNow;
 let rt;async function pollRuns(force){clearTimeout(rt);const [ok,j]=await api('GET','/api/admin/timeline');if(ok){runs=j.runs;timeline()}if(force||(ok&&j.running))rt=setTimeout(()=>pollRuns(false),4000)}
 // events
 $('#lvls').addEventListener('click',e=>{const b=e.target.closest('[data-lv]');if(!b)return;const l=b.dataset.lv;F.lv.has(l)?F.lv.delete(l):F.lv.add(l);if(!F.lv.size)F.lv.add(l);fetchLogs(false)});
 $('#hist').addEventListener('click',e=>{const b=e.target.closest('.b');if(!b)return;F.bucket=F.bucket===+b.dataset.i?null:+b.dataset.i;fetchLogs(false)});
 $('#histAx').addEventListener('click',e=>{if(e.target.id==='clrH'){e.preventDefault();reload()}});
 $('#runs').addEventListener('click',e=>{const s=e.target.closest('[data-stage]'),r=e.target.closest('[data-run]');if(!r)return;const id=r.dataset.run;
  if(s&&s.dataset.stage){opt('#fRun',[],id);$('#fRun').value=id;$('#fMod').value=s.dataset.stage;F.lv=new Set(LV);$('#fTime').value='7d'}else{const on=$('#fRun').value===id;opt('#fRun',[],on?'':id);$('#fRun').value=on?'':id;if(!on)$('#fTime').value='7d'}reload()});
 $('#runs').addEventListener('keydown',e=>{if(e.key==='Enter')e.target.closest('[data-run]')?.click()});
 $('#rows').addEventListener('click',e=>{const x=e.target.closest('[data-x]');if(x){const id=+x.dataset.x;F.open.has(id)?F.open.delete(id):F.open.add(id);render();return}
  const rn=e.target.closest('[data-run]');if(rn){e.preventDefault();onlyRun(rn.dataset.run);return}
  const od=e.target.closest('[data-open-dw]');if(od)return openDw(od.dataset.openDw);
  if(e.target.closest('.detail'))return;const tr=e.target.closest('tr.r');if(tr)openDw(tr.dataset.id)});
 ['fMod','fSrc','fRun','fUser'].forEach(k=>$('#'+k).onchange=reload);
 $('#fTime').onchange=()=>{const c=$('#fTime').value==='custom';$('#customRow').hidden=!c;if(c&&!$('#tFrom').value){$('#tFrom').value=iso(Date.now()-864e5).slice(0,16);$('#tTo').value=iso(Date.now()).slice(0,16)}reload()};
 ['tFrom','tTo'].forEach(k=>$('#'+k).onchange=reload);
 let qt;$('#q').oninput=()=>{clearTimeout(qt);qt=setTimeout(reload,250)};
 $('#more').onclick=()=>fetchLogs(true);
 $('#expAll').onclick=()=>{const errs=F.items.filter(hasErr),allOpen=errs.length&&errs.every(l=>F.open.has(l.id));errs.forEach(l=>allOpen?F.open.delete(l.id):F.open.add(l.id));$('#expAll').textContent=allOpen?'展开全部失败详情':'收起全部失败详情';render()};
 $('#exp').onclick=()=>{const blob=new Blob([JSON.stringify(F.items,null,2)],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='newsd-logs.json';a.click();ND.toast('已导出 '+F.items.length+' 条（当前已加载的）')};
 $('#copyLink').onclick=()=>{navigator.clipboard?.writeText(location.href).then(()=>ND.toast('筛选链接已复制'),()=>ND.toast('复制失败'))};
 document.querySelector('.lvls').addEventListener('click',e=>{const v=e.target.closest('[data-view]');if(!v)return;const k=v.dataset.view;['fMod','fSrc','fRun','fUser'].forEach(x=>$('#'+x).value='');$('#q').value='';
  if(k==='err'){F.lv=new Set(['ERROR']);$('#fTime').value='7d'}if(k==='push'){F.lv=new Set(['WARN','ERROR']);$('#fMod').value='push';$('#fTime').value='7d'}if(k==='fo'){F.lv=new Set(LV);$('#q').value='模型切换';$('#fTime').value='7d'}if(k==='reset'){F.lv=new Set(['INFO','WARN','ERROR']);$('#fTime').value='24h'}$('#customRow').hidden=true;reload()});
 let lt;$('#live').addEventListener('toggle',()=>{const on=$('#live').getAttribute('aria-checked')==='true';$('#liveL').innerHTML=on?'<span class="livedot"></span> 实时跟随中（每 5 秒）':'实时跟随';clearInterval(lt);
  if(on){if($('#fTime').value==='custom'){$('#fTime').value='24h';$('#customRow').hidden=true}lt=setInterval(()=>{if(F.bucket==null&&!document.hidden)fetchLogs(false)},5000);reload()}});
 if(I.timeline.running)pollRuns(false);
 reload();
})();
