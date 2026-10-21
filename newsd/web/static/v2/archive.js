/* 归档: month grid (server-rendered links), day preview from /api/me/issues/{key}, list view, instant search. */
(function(){
 const {$,$$,esc,api}=ND,A=window.ND_ARCH||{},MOD=Object.fromEntries((A.mods||[]).map(m=>[m[0],m[1]])),WK='日一二三四五六';
 const grid=$('#grid'),dayp=$('#dayp');
 async function panel(k){if(!k)return;$$('.d.sel',grid).forEach(d=>d.classList.remove('sel'));const c=$(`.d[data-d="${k}"]`,grid);c&&c.classList.add('sel');
  dayp.setAttribute('aria-busy','true');const [ok,j]=await api('GET','/api/me/issues/'+encodeURIComponent(k));dayp.removeAttribute('aria-busy');
  if(!ok){dayp.innerHTML=`<div class="kicker">${esc(k)}</div><p class="muted">加载失败。</p>`;return}
  const its=(j.items||[]).filter(i=>i.state!=='trash'),dt=/^\d{4}-\d{2}-\d{2}$/.test(k)?new Date(k+'T00:00'):null;
  dayp.innerHTML=`<div class="kicker">${esc(dt?k.slice(5)+' · 周'+WK[dt.getDay()]:k)}</div><h2 style="font:400 28px var(--font-display);margin:10px 0 16px">${its.length} 条 · ${its.filter(i=>i.vote===1).length} 赞 · ${its.filter(i=>i.state==='favorite').length} 收藏</h2>
  <ul class="timeline">${its.slice(0,6).map(i=>`<li><small>${esc((i.published_at||'').slice(11,16))}${i.published_at?' · ':''}${esc(MOD[i.module]||i.module)}</small><br>${esc(i.title_translated||i.title)}</li>`).join('')}</ul>
  <a class="btn primary" href="/issue/${encodeURIComponent(k)}" style="width:100%;justify-content:center">打开当日日报</a>`}
 grid.addEventListener('click',e=>{const c=e.target.closest('.d[data-d]');if(!c||e.metaKey||e.ctrlKey)return;e.preventDefault();panel(c.dataset.d)});
 $('#view').addEventListener('click',e=>{const b=e.target.closest('[data-v]');if(!b)return;grid.hidden=b.dataset.v!=='m';$('#listv').hidden=b.dataset.v==='m'});
 let smod='',st,seq=0;
 $('#sm').addEventListener('click',e=>{const b=e.target.closest('[data-m]');if(b){smod=b.dataset.m;search()}});
 const hl=(t,ws)=>{let h=esc(t);ws.forEach(w=>{h=h.replace(new RegExp(esc(w).replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'gi'),x=>`<mark>${x}</mark>`)});return h};
 async function search(){const q=$('#sq').value.trim(),ws=q.toLowerCase().split(/\s+/).filter(Boolean),n=++seq;
  if(!ws.length&&!smod){$('#sr').innerHTML='';$('#sn').textContent='';return}
  const [ok,j]=await api('GET','/api/me/search?'+new URLSearchParams({q,module:smod}));if(n!==seq)return;
  if(!ok){$('#sn').textContent='搜索失败';return}
  $('#sn').textContent=(j.total>j.items.length?j.items.length+'+':j.items.length)+' 条';
  $('#sr').innerHTML=j.items.map(i=>`<li><small>${esc(i.key)} · ${esc(i.source||'')} · ${esc(MOD[i.module]||i.module)}</small><br><a href="/issue/${encodeURIComponent(i.key)}#i-${i.item_id}">${hl(i.title,ws)}</a><div class="muted" style="font-size:13px">${hl((i.summary||'').slice(0,220),ws)}</div></li>`).join('')||'<li class="muted">没有结果，换个词试试</li>'}
 $('#sq').addEventListener('input',()=>{clearTimeout(st);st=setTimeout(search,80)});
 $('#sq').addEventListener('keydown',e=>{if(e.key==='Escape'){$('#sq').value='';search()}});
 if(A.sel)panel(A.sel);
})();
