/* newsd v2 shell (DEPTAI-216 item 3, built to the accepted demo). Server-rendered pages; this script adds:
   theme orb dock + ☀/☾ toggle (saved to the account), avatar menu, book-turn page transitions on every
   in-site link (off with prefers-reduced-motion or 设置 → 动态效果), toasts with undo, generic widgets
   (switch / seg / single chips / overlays) and the card actions ♥ 赞 / 👎 踩 / 收藏 / 收起 / 删除 (→ 垃圾箱). */
(function(){
 const $=(s,r=document)=>r.querySelector(s),$$=(s,r=document)=>[...r.querySelectorAll(s)];
 const H=document.documentElement,B=document.body,CFG=window.ND_CFG||{};
 const ls={get:(k,d)=>{try{const v=localStorage.getItem(k);return v==null?d:v}catch(e){return d}},set:(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}}};
 const ss={get:k=>{try{return sessionStorage.getItem(k)}catch(e){return null}},set:(k,v)=>{try{v==null?sessionStorage.removeItem(k):sessionStorage.setItem(k,v)}catch(e){}}};
 const RM=()=>matchMedia('(prefers-reduced-motion: reduce)').matches;
 const THEMES=window.ND_THEMES||[],TID=THEMES.map(t=>t[0]);
 const SCH=id=>(THEMES.find(t=>t[0]===id)||[])[4]||'light';
 const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 async function api(method,url,body){
  try{const r=await fetch(url,{method,headers:{'content-type':'application/json'},credentials:'same-origin',body:body===undefined?undefined:JSON.stringify(body)});
   let j={};try{j=await r.json()}catch(e){}return [r.ok,j,r.status]}catch(e){return [false,{error:'网络错误'},0]}}
 const subs=[];
 const ND=window.ND={$,$$,esc,api,RM,CFG,ls,
  on(f){subs.push(f)},emit(ev){subs.forEach(f=>{try{f(ev||{})}catch(e){console.error(e)}})},
  toast(m,undo){let t=$('.toast')||B.appendChild(Object.assign(document.createElement('div'),{className:'toast',role:'status'}));
   t.innerHTML='';t.append(m);if(undo){const b=document.createElement('button');b.type='button';b.textContent='撤销';b.onclick=()=>{undo();t.classList.remove('show')};t.append(b)}
   t.classList.add('show');clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('show'),undo?4200:1900)},
  fb:()=>B.dataset.fb||'both',
  turnOn:()=>!RM()&&(B.dataset.motion||'on')!=='off',
  error(j,what){ND.toast((what||'操作失败')+(j&&j.error?'：'+j.error:''))}};

 // ---------- theme ----------
 ND.setTheme=function(id,from,silent){if(!TID.includes(id)||id===H.dataset.theme)return;
  const go=()=>{H.dataset.theme=id;ls.set(SCH(id)==='dark'?'nd-last-dark':'nd-last-light',id);paintOrbs();paintMode();ND.emit({type:'theme',theme:id})};
  if(!silent&&CFG.user)api('PUT','/api/me/theme',{palette:id});
  if(RM()||!from)return go();
  const r=from.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;
  if(!document.startViewTransition){B.classList.add('tfade');go();setTimeout(()=>B.classList.remove('tfade'),450);return}
  const R=Math.hypot(Math.max(x,innerWidth-x),Math.max(y,innerHeight-y));
  document.startViewTransition(go).ready.then(()=>H.animate({clipPath:[`circle(0px at ${x}px ${y}px)`,`circle(${R}px at ${x}px ${y}px)`]},{duration:520,easing:'cubic-bezier(.65,0,.35,1)',pseudoElement:'::view-transition-new(root)'})).catch(()=>{})};
 ND.toggleMode=function(from){const dark=SCH(H.dataset.theme)==='dark';let id=ls.get(dark?'nd-last-light':'nd-last-dark',dark?'arch-editorial':'taste-moat');
  if(SCH(id)===(dark?'dark':'light'))id=dark?'arch-editorial':'taste-moat';ND.setTheme(id,from);ND.toast(dark?'已切到浅色主题':'已切到深色主题')};
 function paintMode(){const dark=SCH(H.dataset.theme)==='dark';$$('.mode-btn').forEach(b=>{b.dataset.mode=dark?'dark':'light';const t=dark?'切换到浅色主题':'切换到深色主题';b.title=t;b.setAttribute('aria-label',t)})}
 $$('.mode-btn').forEach(b=>b.addEventListener('click',e=>{e.preventDefault();e.stopPropagation();ND.toggleMode(b)}));
 ls.set(SCH(H.dataset.theme)==='dark'?'nd-last-dark':'nd-last-light',H.dataset.theme);paintMode();
 let paintOrbs=()=>{};
 const sw=$('.tsw');
 if(sw){const cur=$('.orb.cur',sw),dock=$('.dock',sw),orbs=$$('.orb',dock);
  orbs.forEach((o,i)=>o.style.setProperty('--d',Math.abs(i-(orbs.length-1)/2)));
  paintOrbs=()=>{const t=H.dataset.theme,o=orbs.find(x=>x.dataset.t===t);orbs.forEach(x=>x.setAttribute('aria-checked',String(x===o)));if(o){cur.setAttribute('style',o.getAttribute('style'));cur.title='主题：'+o.dataset.name}};
  const open=v=>{sw.classList.toggle('open',v);cur.setAttribute('aria-expanded',String(v));if(v){dock.hidden=false;(orbs.find(o=>o.getAttribute('aria-checked')==='true')||orbs[0]).focus({preventScroll:true})}else setTimeout(()=>{if(!sw.classList.contains('open'))dock.hidden=true},260)};
  cur.addEventListener('click',e=>{e.stopPropagation();open(!sw.classList.contains('open'))});
  orbs.forEach(o=>o.addEventListener('click',e=>{e.stopPropagation();ND.setTheme(o.dataset.t,o)}));
  dock.addEventListener('keydown',e=>{const i=orbs.indexOf(document.activeElement);if(e.key==='ArrowRight'||e.key==='ArrowLeft'){e.preventDefault();orbs[(i+(e.key==='ArrowRight'?1:-1)+orbs.length)%orbs.length].focus()}if(e.key==='Escape'){open(false);cur.focus()}});
  document.addEventListener('click',e=>{if(!sw.contains(e.target))open(false)});
  document.addEventListener('keydown',e=>{if((e.key==='t'||e.key==='T')&&!e.target.closest('input,textarea,select,[contenteditable]')&&!e.metaKey&&!e.ctrlKey&&!e.altKey){const i=TID.indexOf(H.dataset.theme);ND.setTheme(TID[(i+1)%TID.length],cur)}});
  paintOrbs()}
 ND.paintOrbs=()=>paintOrbs();

 // ---------- avatar menu ----------
 const um=$('.umenu');if(um){const b=$('button',um),p=$('.umenu-pop',um);
  const set=v=>{if(v===!p.hidden&&!p.classList.contains('out'))return;b.setAttribute('aria-expanded',v);um.classList.toggle('open',v);clearTimeout(p._t);
   if(v){p.classList.remove('out');p.hidden=false}else if(p.hidden)return;else if(RM())p.hidden=true;else{p.classList.add('out');p._t=setTimeout(()=>{p.hidden=true;p.classList.remove('out')},150)}};
  b.addEventListener('click',e=>{e.stopPropagation();set(p.hidden)});document.addEventListener('click',e=>{if(!um.contains(e.target))set(false)});um.addEventListener('keydown',e=>{if(e.key==='Escape'){set(false);b.focus()}})}

 // ---------- reading progress ----------
 const prog=$('.prog');if(prog){const upd=()=>{const h=H.scrollHeight-innerHeight;prog.style.setProperty('--p',h>0?Math.min(1,scrollY/h):0)};addEventListener('scroll',upd,{passive:true});upd()}

 // ---------- book-turn page transition ----------
 const PT_OUT=380,PT_IN=560;
 const ORDER=['welcome','login','register','issue','favorites','calendar','trash','sources','models','settings','learn','acct','admin','users','health','logs'];
 const pageOfPath=p=>{p=p.replace(/\/+$/,'')||'/';if(p==='/'||p.startsWith('/issue'))return 'issue';if(p.startsWith('/admin/'))return p.split('/')[2];return p.slice(1).split('/')[0]};
 function dirOf(u){const a=ORDER.indexOf(B.dataset.page||'issue'),b=ORDER.indexOf(pageOfPath(u.pathname));
  const d1=(location.pathname.match(/\/issue\/([\w.:-]+)/)||[])[1]||B.dataset.key,d2=(u.pathname.match(/\/issue\/([\w.:-]+)/)||[])[1];
  if(d1&&d2&&d1!==d2)return d2>d1?'next':'prev';return b<a?'prev':'next'}
 function ptEl(){const e=$('.console>main')||$('body>main')||$('body>.auth');if(e)e.classList.add('pt-page');return e}
 ptEl();
 ND.go=function(url,dir){const u=new URL(url,location.href);if(!ND.turnOn()||!ptEl()){location.href=u.href;return}
  if(H.classList.contains('pt-out'))return;dir=dir||dirOf(u);ss.set('nd-pt',dir);H.classList.add('pt-out','pt-'+dir);setTimeout(()=>{location.href=u.href},PT_OUT)};
 const pt=ss.get('nd-pt');if(pt){ss.set('nd-pt',null);if(ND.turnOn()&&ptEl()){H.classList.add('pt-in','pt-'+pt);setTimeout(()=>H.classList.remove('pt-in','pt-next','pt-prev'),PT_IN+60)}}
 addEventListener('pageshow',e=>{if(e.persisted)H.classList.remove('pt-out','pt-next','pt-prev')});
 document.addEventListener('click',e=>{const a=e.target.closest('a[href]');if(!a||e.defaultPrevented||e.button||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey||a.target||a.hasAttribute('download'))return;
  const h=a.getAttribute('href');if(!h||h.startsWith('#'))return;let u;try{u=new URL(h,location.href)}catch(x){return}
  if(u.origin!==location.origin||/^\/(api|static|logout)\b/.test(u.pathname))return;
  if(u.pathname===location.pathname&&u.search===location.search&&u.hash)return; // same-page anchor
  e.preventDefault();ND.go(u.href,a.dataset.dir)});

 // ---------- generic widgets ----------
 document.addEventListener('click',e=>{
  const sw2=e.target.closest('.switch');if(sw2&&!sw2.disabled&&sw2.getAttribute('role')==='switch'){sw2.setAttribute('aria-checked',sw2.getAttribute('aria-checked')!=='true');sw2.dispatchEvent(new Event('toggle',{bubbles:true}));return}
  const chip=e.target.closest('.chips.single .chip');if(chip){$$('.chip',chip.parentNode).forEach(c=>c.setAttribute('aria-pressed',c===chip))}
  const sg=e.target.closest('.seg:not(.multi) button');if(sg){$$('button',sg.parentNode).forEach(b=>b.setAttribute('aria-pressed',b===sg))}
  const op=e.target.closest('[data-open]');if(op){const o=$('#'+op.dataset.open);if(o)o.classList.add('open');return}
  if(e.target.closest('[data-close]')||e.target.classList.contains('overlay')){$$('.overlay.open').forEach(o=>o.classList.remove('open'))}
 });
 document.addEventListener('keydown',e=>{if(e.key==='Escape')$$('.overlay.open').forEach(o=>o.classList.remove('open'))});

 // ---------- trash badge ----------
 ND.trashN=+(B.dataset.trash||0);
 ND.paintTrash=d=>{ND.trashN=Math.max(0,ND.trashN+(d||0));$$('.trash-btn .badge').forEach(b=>{b.hidden=!ND.trashN;b.textContent=ND.trashN})};
 ND.paintTrash(0);

 // ---------- card actions ----------
 const pop=b=>{if(!RM())b.animate([{transform:'scale(1)'},{transform:'scale(1.35)'},{transform:'scale(1)'}],{duration:320,easing:'cubic-bezier(.3,1.4,.5,1)'})};
 ND.paintCard=c=>{const v=+(c.dataset.vote||0),fav=c.dataset.fav==='1';
  const l=$('.a-like',c);if(l)l.setAttribute('aria-pressed',String(v===1));
  const dn=$('.a-down',c);if(dn){dn.setAttribute('aria-pressed',String(v===-1));dn.title=v===-1?'已踩（点击取消）':'踩：减少相似推荐';dn.setAttribute('aria-label',dn.title)}
  c.classList.toggle('down',v===-1);const f=$('.a-fav',c);if(f){f.setAttribute('aria-pressed',String(fav));f.title=fav?'取消收藏':'收藏';f.setAttribute('aria-label',f.title)}};
 const sameCards=id=>$$(`.item[data-id="${id}"]`);
 const setAll=(id,k,v)=>sameCards(id).forEach(c=>{c.dataset[k]=v;ND.paintCard(c)});
 async function vote(id,v){const [ok,j]=await api('PUT',`/api/me/items/${id}/vote`,{vote:v});if(!ok){ND.error(j);return false}setAll(id,'vote',v);ND.emit({type:'vote',id,vote:v});return true}
 async function mark(id,state){const [ok,j]=await api('PUT',`/api/me/items/${id}/mark`,{state});if(!ok){ND.error(j);return false}return true}
 ND.fold=async(id,on)=>{const [ok,j]=await api('PUT',`/api/me/items/${id}/fold`,{folded:on});if(!ok){ND.error(j);return false}setAll(id,'folded',on?'1':'0');ND.emit({type:'fold',id,folded:on});return true};
 ND.del=async(id,on,prev)=>{const ok=await mark(id,on?'trash':(prev||null));if(!ok)return false;ND.paintTrash(on?1:-1);
  sameCards(id).forEach(c=>{c.dataset.fav=(!on&&prev==='favorite')?'1':'0';c.hidden=!!on;c.classList.remove('deleting');ND.paintCard(c)});ND.emit({type:'del',id,deleted:on});return true};
 document.addEventListener('click',async e=>{const b=e.target.closest('.acts button');if(!b)return;const c=b.closest('.item[data-id]');if(!c)return;const id=c.dataset.id;e.preventDefault();
  const was=+(c.dataset.vote||0);
  if(b.classList.contains('a-like')){if(await vote(id,was===1?0:1)){ND.toast(was===1?'已取消赞':'已赞：多推相似内容',()=>vote(id,was));pop(b)}}
  else if(b.classList.contains('a-down')){if(await vote(id,was===-1?0:-1))ND.toast(was===-1?'已取消踩':'已踩：相似内容会减少',()=>vote(id,was))}
  else if(b.classList.contains('a-fav')){const on=c.dataset.fav!=='1';if(await mark(id,on?'favorite':null)){setAll(id,'fav',on?'1':'0');ND.emit({type:'fav',id,fav:on});ND.toast(on?'已收藏 · 在「收藏」页查看':'已取消收藏',async()=>{if(await mark(id,on?null:'favorite')){setAll(id,'fav',on?'0':'1');ND.emit({type:'fav',id,fav:!on})}});pop(b)}}
  else if(b.classList.contains('a-fold')){if(await ND.fold(id,true))ND.toast('已收起，放在本栏底部',()=>ND.fold(id,false))}
  else if(b.classList.contains('a-del')){const prev=c.dataset.fav==='1'?'favorite':null,what=c.dataset.what||'';
   const go=async()=>{if(await ND.del(id,true))ND.toast('已移到垃圾箱'+(what?' · 将减少 '+what:''),()=>ND.del(id,false,prev))};
   if(!RM()){c.classList.add('deleting');setTimeout(go,240)}else go()}
 });
 $$('.item[data-id]').forEach(ND.paintCard);
})();
