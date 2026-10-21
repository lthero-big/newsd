/* Sortable lists for the demo (no library). Pointer drag: lift → a ghost follows the pointer, a dashed placeholder opens
   the gap, neighbours glide out of the way (FLIP), the target list is highlighted, the page auto-scrolls near the edges,
   the ghost settles into place on drop, Esc cancels. Keyboard: Space / Enter on a grip lifts, ↑ ↓ (or ← →) move one slot
   — across list boundaries when lists are connected — Space / Enter drops, Esc puts it back. Motion is skipped with
   prefers-reduced-motion.
   ND.sortable({ root, lists:()=>[el…], item, handle, grip, ignore, kbd, altKeys, cross, onStart, onEnd, onDrop, snapshot, restore, label })
     onDrop({el, from, fromIndex, to, toIndex, kbd}) → caller updates data, re-renders and returns the moved element. */
(function(){
 const RM=()=>matchMedia('(prefers-reduced-motion: reduce)').matches;
 let live;const say=m=>{if(!live){live=document.createElement('div');live.className='sr';live.setAttribute('aria-live','assertive');document.body.append(live)}live.textContent='';setTimeout(()=>live.textContent=m,40)};
 window.ND=window.ND||{};ND.say=say;
 ND.sortable=function(o){
  const root=o.root,EDGE=72;let st=null,kb=null,swallow=false;
  const isItem=c=>c.matches(o.item)&&!c.classList.contains('dnd-ghost');
  const itemsOf=l=>[...l.children].filter(c=>isItem(c)&&!c.classList.contains('dnd-src'));
  const horiz=l=>{const cs=getComputedStyle(l);return cs.display.includes('flex')&&cs.flexDirection.startsWith('row')};
  const flip=(els,mut)=>{if(RM()){mut();return}const r0=new Map(els.map(e=>[e,e.getBoundingClientRect()]));mut();
   r0.forEach((a,e)=>{if(!e.isConnected)return;const b=e.getBoundingClientRect(),dx=a.left-b.left,dy=a.top-b.top;if(dx||dy)e.animate([{transform:`translate(${dx}px,${dy}px)`},{transform:'none'}],{duration:180,easing:'cubic-bezier(.2,.8,.2,1)'})})};
  const setOver=t=>{o.lists().forEach(l=>l.classList.toggle('dnd-over',l===t))};
  const idxIn=(l,el)=>[...l.children].filter(c=>c===el||(isItem(c)&&!c.classList.contains('dnd-src'))).indexOf(el);

  // ---- pointer ----
  root.addEventListener('pointerdown',e=>{if(e.button!==0||st||kb)return;const h=e.target.closest(o.handle);if(!h||!root.contains(h))return;
   const el=h.closest(o.item);if(!el)return;const ig=o.ignore&&e.target.closest(o.ignore);if(ig&&ig!==el&&!(o.grip&&ig.matches(o.grip)))return;
   if(e.pointerType!=='mouse'&&o.grip&&!e.target.closest(o.grip))return; // touch: only from the grip, so the page still scrolls
   st={el,x0:e.clientX,y0:e.clientY,x:e.clientX,y:e.clientY,on:false};
   document.addEventListener('pointermove',mv);document.addEventListener('pointerup',up);document.addEventListener('pointercancel',cancel);document.addEventListener('keydown',esc,true)});
  function mv(e){if(!st)return;st.x=e.clientX;st.y=e.clientY;if(!st.on){if(Math.hypot(st.x-st.x0,st.y-st.y0)<6)return;begin()}e.preventDefault();place()}
  function begin(){const el=st.el;st.on=true;let r=el.getBoundingClientRect();
   const r0=r;if(o.onStart){o.onStart(el);const r2=el.getBoundingClientRect();if(Math.abs(r2.top-r.top)>1)scrollBy(0,r2.top-r.top);r=el.getBoundingClientRect()}
   st.from=el.parentNode;st.fromIndex=idxIn(st.from,el);st.ox=st.x0-r0.left;st.oy=Math.max(4,Math.min(st.y0-r0.top,r.height-4)); // keep the grabbed spot under the pointer even if the item shrankst.w=r.width;st.h=r.height;
   const g=el.cloneNode(true);g.classList.add('dnd-ghost');g.removeAttribute('id');g.setAttribute('aria-hidden','true');g.style.width=r.width+'px';g.style.height=r.height+'px';
   const ph=document.createElement('div');ph.className='dnd-ph';ph.setAttribute('aria-hidden','true');if(horiz(st.from)){ph.style.width=r.width+'px';ph.style.height=r.height+'px'}else ph.style.height=r.height+'px';
   el.before(ph);el.classList.add('dnd-src');st.from.append(g);st.g=g;st.ph=ph;st.list=st.from;
   st.from.classList.add('dnd-from');document.body.classList.add('dnd-active');setOver(st.from);
   if(!RM())g.animate([{transform:`translate(${r.left}px,${r.top}px)`},{transform:ghostT()}],{duration:140,easing:'ease-out'});
   g.style.transform=ghostT();say((o.label?o.label(el):'')+' 已拿起');tick()}
  const ghostT=()=>`translate(${st.x-st.ox}px,${st.y-st.oy}px) rotate(${o.tilt??1.2}deg) scale(1.02)`;
  function target(){const L=o.lists();let t=null;const u=document.elementFromPoint(st.x,st.y);if(u)t=L.find(l=>l.contains(u))||null;
   if(!o.cross)return st.from;
   if(!t){let best=1e9;L.forEach(l=>{const r=l.getBoundingClientRect(),dx=Math.max(r.left-st.x,0,st.x-r.right),dy=Math.max(r.top-st.y,0,st.y-r.bottom),d=dx*dx+dy*dy;if(d<best&&d<160*160){best=d;t=l}})}
   return t||st.list}
  function place(){if(!st.on)return;st.g.style.transform=ghostT();const t=target(),its=itemsOf(t),hz=horiz(t),p=hz?st.x:st.y;
   let ref=null;for(const it of its){const r=it.getBoundingClientRect();if(p<(hz?r.left+r.width/2:r.top+r.height/2)){ref=it;break}}
   const cur=(()=>{let n=st.ph.nextElementSibling;while(n&&!(isItem(n)&&!n.classList.contains('dnd-src')))n=n.nextElementSibling;return n})();
   if(st.ph.parentNode===t&&cur===ref)return;
   const moving=[...new Set([...itemsOf(st.list),...its])];
   flip(moving,()=>{if(ref)ref.before(st.ph);else{const last=its[its.length-1];last?last.after(st.ph):t.append(st.ph)}if(st.g.parentNode!==t)t.append(st.g)});
   if(t!==st.list){st.list=t;setOver(t);if(o.label)say('放到「'+(o.listName?o.listName(t):'')+'」')}}
  function tick(){if(!st||!st.on)return;const y=st.y;let v=0;if(y<EDGE)v=-Math.ceil((EDGE-y)/5);else if(y>innerHeight-EDGE)v=Math.ceil((y-innerHeight+EDGE)/5);
   if(v){const b=scrollY;scrollBy(0,v);if(scrollY!==b)place()}requestAnimationFrame(tick)}
  function esc(e){if(e.key==='Escape'&&st){e.preventDefault();e.stopPropagation();cancel()}}
  function off(){document.removeEventListener('pointermove',mv);document.removeEventListener('pointerup',up);document.removeEventListener('pointercancel',cancel);document.removeEventListener('keydown',esc,true)}
  function cancel(){if(!st)return;off();if(!st.on){st=null;return}const s=st;
   const its=itemsOf(s.from);const ref=its[s.fromIndex]||null;ref?ref.before(s.ph):(its.length?its[its.length-1].after(s.ph):s.from.append(s.ph));finish(true)}
  function up(){if(!st)return;off();if(!st.on){st=null;return}finish(false)}
  function finish(cancelled){const s=st,r=s.ph.getBoundingClientRect();swallow=true;setTimeout(()=>swallow=false,0);
   const done=()=>{const to=s.ph.parentNode,toIndex=idxIn(to,s.ph);s.g.remove();s.ph.replaceWith(s.el);s.el.classList.remove('dnd-src');
    o.lists().forEach(l=>l.classList.remove('dnd-over','dnd-from'));document.body.classList.remove('dnd-active');st=null;o.onEnd&&o.onEnd();
    let el=s.el;if(!cancelled&&(to!==s.from||toIndex!==s.fromIndex))el=o.onDrop({el:s.el,from:s.from,fromIndex:s.fromIndex,to,toIndex})||s.el;else if(cancelled)say('已取消');
    if(el&&!cancelled){el.classList.remove('dnd-landed');void el.offsetWidth;el.classList.add('dnd-landed');setTimeout(()=>el.classList.remove('dnd-landed'),1000)}};
   if(RM())return done();
   s.g.animate([{transform:s.g.style.transform},{transform:`translate(${r.left}px,${r.top}px)`}],{duration:200,easing:'cubic-bezier(.2,.8,.2,1)'}).finished.then(done,done);s.g.style.transform=`translate(${r.left}px,${r.top}px)`}
  root.addEventListener('click',e=>{if(swallow){e.preventDefault();e.stopPropagation();swallow=false}},true);

  // ---- keyboard ----
  root.addEventListener('keydown',e=>{if(st)return;const g=o.kbd&&e.target.closest(o.kbd);if(!g||!root.contains(g))return;const el=g.closest(o.item);if(!el)return;
   const dirKey={ArrowUp:-1,ArrowLeft:-1,ArrowDown:1,ArrowRight:1}[e.key];
   if(o.altKeys){if(!(e.altKey&&dirKey))return;e.preventDefault();step(el,dirKey,true);return} // e.g. tabs: Alt+↑/↓ reorders directly
   if(e.key===' '||e.key==='Enter'){e.preventDefault();if(!kb){kb={snap:o.snapshot&&o.snapshot()};el.classList.add('dnd-lifted');say((o.label?o.label(el):'')+' 已拿起。上下方向键移动，空格放下，Esc 取消')}else{lift(false);say('已放下')}return}
   if(e.key==='Escape'&&kb){e.preventDefault();const f=o.restore&&o.restore(kb.snap);kb=null;refocus(f);say('已取消，回到原位');return}
   if(kb&&dirKey){e.preventDefault();step(el,dirKey,false)}});
  root.addEventListener('focusout',e=>{if(kb&&!root.contains(e.relatedTarget)&&e.relatedTarget)lift(false)});
  function lift(on){root.querySelectorAll('.dnd-lifted').forEach(x=>x.classList.toggle('dnd-lifted',on));if(!on)kb=null}
  function step(el,d,direct){const L=o.lists(),from=el.parentNode,its=itemsOf(from),i=its.indexOf(el);let to=from,ti=i+d;
   if(ti<0||ti>=its.length){if(!o.cross)return say('已经到头了');const li=L.indexOf(from)+d;if(li<0||li>=L.length)return say('已经到头了');to=L[li];ti=d<0?itemsOf(to).length:0}
   const nel=o.onDrop({el,from,fromIndex:i,to,toIndex:ti,kbd:true})||el;refocus(nel);if(!direct)nel.classList.add('dnd-lifted');
   say((o.listName?'「'+o.listName(nel.parentNode)+'」':'')+'第 '+(itemsOf(nel.parentNode).indexOf(nel)+1)+' 位，共 '+itemsOf(nel.parentNode).length+' 个');
   if(!RM())nel.animate([{transform:`translateY(${d*-8}px)`},{transform:'none'}],{duration:160,easing:'ease-out'})}
  function refocus(el){if(!el)return;const f=o.kbd&&(el.matches(o.kbd)?el:el.querySelector(o.kbd));(f||el).focus({preventScroll:false})}
  return {busy:()=>!!(st&&st.on)||!!kb}};
})();
