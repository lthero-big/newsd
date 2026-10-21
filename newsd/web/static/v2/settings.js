/* 偏好与主题 / 学到的偏好 / 账号 (console). */
(function(){
 const {$,$$,esc,api}=ND,B=document.body;
 const paintFb=()=>$$('#fbm button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.v===B.dataset.fb)));
 $('#fbm').addEventListener('click',async e=>{const b=e.target.closest('[data-v]');if(!b)return;const [ok,j]=await api('PUT','/api/me/ui',{feedback:b.dataset.v});
  if(!ok)return ND.error(j);B.dataset.fb=j.feedback;paintFb();ND.toast(j.feedback==='like'?'已改为：仅 ♥ 赞':'已改为：♥ 赞 + 踩')});
 const paintT=()=>$$('#themes .theme-card').forEach(c=>c.setAttribute('aria-pressed',String(c.dataset.t===document.documentElement.dataset.theme)));
 $('#themes').addEventListener('click',e=>{const b=e.target.closest('[data-t]');if(b)ND.setTheme(b.dataset.t,b)});
 const rmq=matchMedia('(prefers-reduced-motion: reduce)'),rmn=()=>{$('#rmNote').textContent=rmq.matches?'（当前系统已开启「减少动态效果」，翻书已自动关闭）':''};rmn();rmq.addEventListener&&rmq.addEventListener('change',rmn);
 $('#motion').addEventListener('toggle',async()=>{const on=$('#motion').getAttribute('aria-checked')==='true';const [ok,j]=await api('PUT','/api/me/ui',{motion:on?'on':'off'});
  if(!ok){$('#motion').setAttribute('aria-checked',String(!on));return ND.error(j)}B.dataset.motion=j.motion;ND.toast(on?'已开启翻书换页':'已关闭换页动效')});
 ND.on(ev=>{if(ev.type==='theme')paintT()});paintT();paintFb();
 let L=[],lpk='';
 const FN={src:'来源',kw:'关键词',tag:'话题'};
 function lp(){const xs=L.filter(e=>!lpk||e.kind===lpk),mx=Math.max(1,...xs.map(e=>e.score));
  $('#lp').innerHTML=xs.map(e=>{const off=e.forgotten;return `<div class="lp${off?' off':''}"><span class="k">${FN[e.kind]}</span><div><b>${esc(e.value)}</b><small>${e.deletes?'删除 '+e.deletes+' 次':''}${e.deletes&&e.dislikes?' · ':''}${e.dislikes?'踩 '+e.dislikes+' 次':''} · 降权 ${e.score.toFixed(1)}${off?' · 已撤销':''}</small></div><span class="meter" title="降权强度"><i style="--w:${Math.round(e.score/mx*100)}%"></i></span><button class="btn sm ${off?'':'ghost'}" type="button" data-key="${esc(e.key)}" data-off="${off?1:0}">${off?'恢复降权':'撤销'}</button></div>`}).join('')||'<p class="muted" style="font-size:13px">还没有学到任何降权。在日报里删除或踩一些条目试试。</p>'}
 async function load(){const [ok,j]=await api('GET','/api/me/learned');if(ok){L=j.items;lp()}else $('#lp').innerHTML='<p class="muted">加载失败。</p>'}
 const forget=async(keys,un)=>{const [ok,j]=await api('POST','/api/me/learned/'+(un?'unforget':'forget'),{keys});if(!ok){ND.error(j);return false}L=j.items;lp();return true};
 $('#lpF').addEventListener('click',e=>{const b=e.target.closest('[data-k]');if(b){lpk=b.dataset.k;lp()}});
 $('#lp').addEventListener('click',async e=>{const b=e.target.closest('[data-key]');if(!b)return;const k=b.dataset.key,un=b.dataset.off==='1';
  if(await forget([k],un))ND.toast(un?'已恢复这项降权':'已撤销：不再因此降权',()=>forget([k],!un))});
 $('#forgetAll').onclick=async()=>{const keys=L.filter(e=>!e.forgotten).map(e=>e.key);if(!keys.length)return ND.toast('没有可撤销的降权');if(await forget(keys,false))ND.toast('已全部撤销',()=>forget(keys,true))};
 $('#learnOn').addEventListener('toggle',async()=>{const on=$('#learnOn').getAttribute('aria-checked')==='true';const [ok,j]=await api('PUT','/api/me/learned',{enabled:on});
  if(!ok){$('#learnOn').setAttribute('aria-checked',String(!on));return ND.error(j)}ND.toast(on?'已恢复学习':'已暂停学习：删除 / 踩暂不影响排序')});
 load();
 if(location.hash)setTimeout(()=>{const t=document.querySelector(location.hash);t&&t.scrollIntoView()},50);
 const side=()=>{const k={'#learn':'learn','#acct':'acct'}[location.hash]||'settings';$$('.cside a').forEach(a=>a.toggleAttribute('aria-current',a.dataset.k===k))};
 addEventListener('hashchange',side);side();
})();
