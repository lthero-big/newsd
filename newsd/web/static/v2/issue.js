/* 日报 page: calendar of issues, module layout (lead / side / rest), tag filter, fold list, collapse state. */
(function(){
 const {$,$$,api}=ND,I=window.ND_ISSUE||{};
 const cal=$('.ical .calbox');
 if(cal&&window.DDCal)DDCal({calbox:cal,days:()=>I.days||{},sel:I.key,disableEmpty:true,unit:'一期',dotLabel:'日报',countLabel:c=>`，${c} 条`,emptyLabel:'，无日报',allLabel:'往期',subOf:d=>` · ${(I.days||{})[d]} 条`,
  onAll:()=>ND.go('/calendar'),onSelect:s=>{if(!s||s===I.key)return;setTimeout(()=>ND.go('/issue/'+s,s>I.key?'next':'prev'),ND.RM()?0:420)}});
 document.addEventListener('click',e=>{const a=e.target.closest('[data-scroll]');if(!a)return;const t=document.getElementById(a.dataset.scroll);if(t){e.preventDefault();t.scrollIntoView({behavior:ND.RM()?'auto':'smooth'})}});
 const feed=$('#feed');
 // 重新生成: runs in the background on the server; poll its stage, elapsed time and model tokens until it finishes
 const tk=n=>{n=+n||0;return n>=1e6?(n/1e6).toFixed(1).replace(/\.0$/,'')+'M':n>=1e3?(n/1e3).toFixed(1).replace(/\.0$/,'')+'k':String(n)};
 const line=j=>{const u=j.usage||{};let t=(j.stage_label||'')+' · '+Math.round(j.elapsed||0)+'s';if(u.requests)t+=` · ${u.requests} 次请求 · ${tk(u.input_tokens)} 入 / ${tk(u.output_tokens)} 出`;return t};
 let polling=false;
 async function poll(){if(polling)return;polling=true;const b=$('#build'),st=$('#build-status');if(b)b.disabled=true;
  for(;;){const [ok,j]=await api('GET','/api/me/issues/build/status');if(!ok){st.textContent='无法获取生成进度';break}
   if(j.state==='running'){st.innerHTML='<span class="spin"></span>正在生成 · '+ND.esc(line(j));await new Promise(r=>setTimeout(r,1500));continue}
   if(j.state==='done'){st.textContent='✓ 已生成 '+(j.key||'')+' · '+(j.items_out??0)+' 条 · '+line(j)+' · 正在刷新…';setTimeout(()=>location.href='/',1200);break}
   if(j.state==='failed'){st.textContent='✕ 生成失败：'+(j.error||'')+(j.usage&&j.usage.requests?' · '+line(j):'');if(b)b.disabled=false;break}
   if(b)b.disabled=false;break}
  polling=false}
 $('#build')&&$('#build').addEventListener('click',async()=>{const b=$('#build');b.disabled=true;$('#build-status').innerHTML='<span class="spin"></span>正在提交…';
  const [ok,j]=await api('POST','/api/me/issues/build',{background:true});if(!ok){b.disabled=false;$('#build-status').textContent='生成失败：'+(j.error||'');return}poll()});
 if($('#build'))api('GET','/api/me/issues/build/status').then(([ok,j])=>{if(ok&&j.state==='running')poll()});
 if(!feed)return;
 let tag='';const col=new Set(JSON.parse(ND.ls.get('nd-col','[]')||'[]'));
 $$('.item',feed).forEach((c,i)=>c.dataset.ord=i);
 const ok=c=>!c.hidden&&(!tag||c.dataset.group===tag);
 function layout(sec){const ed=$('.ed',sec);if(!ed)return;const cards=$$('.item',ed).sort((a,b)=>a.dataset.ord-b.dataset.ord);
  const open=cards.filter(c=>c.dataset.folded!=='1'&&ok(c)),folded=cards.filter(c=>c.dataset.folded==='1'&&!c.hidden),pool=$('.pool',ed);
  let lead=$('.elead',ed),side=$('.eside',ed),rest=$('.rest',ed),none=$('.allfold',ed);
  if(!lead){ed.insertAdjacentHTML('afterbegin','<div class="elead"></div><div class="eside"></div><div class="rest"></div>');lead=$('.elead',ed);side=$('.eside',ed);rest=$('.rest',ed)}
  if(!none){none=Object.assign(document.createElement('p'),{className:'muted allfold',textContent:'本栏全部收起了。'});ed.prepend(none)}
  none.hidden=!!open.length;
  open.forEach((c,i)=>{c.classList.toggle('lead',i===0);c.classList.toggle('mini',i>0&&i<4);(i===0?lead:i<4?side:rest).append(c)});
  cards.filter(c=>!open.includes(c)).forEach(c=>{c.classList.remove('lead','mini');pool.append(c)});
  const seen=$('.seen',sec),sl=$('.slist',seen);seen.hidden=!folded.length;$('.shbar b',seen).textContent=folded.length;
  sl.innerHTML='';folded.forEach(c=>{const li=document.createElement('li');li.dataset.id=c.dataset.id;const s=document.createElement('span');s.textContent=$('h3',c).textContent.trim();
   const b=document.createElement('button');b.className='btn sm ghost';b.type='button';b.dataset.unfold=c.dataset.id;b.textContent='放回';li.append(s,b);sl.append(li)});
  const n=cards.filter(c=>!c.hidden&&ok(c)).length;$('.mhead .n',sec).textContent=n;
  const mc=$(`#mods [data-m="${sec.dataset.m}"]`);if(mc){$('b',mc).textContent=n;$('.bar i',mc).style.setProperty('--w',(n?Math.round(open.length/n*100):0)+'%')}}
 function all(){$$('.module',feed).forEach(layout);const live=$$('.item',feed).filter(c=>!c.hidden);
  $('#pFold').textContent=live.filter(c=>c.dataset.folded==='1').length;$('#pLike').textContent=live.filter(c=>c.dataset.vote==='1').length;$('#pN').textContent=live.length;
  const f=$('#feature .item');if(f){f.closest('#feature').hidden=f.hidden}}
 $$('.module',feed).forEach(s=>{const k=s.dataset.m;if(col.has(k)){s.classList.add('collapsed');$('.mhead',s).setAttribute('aria-expanded','false')}});
 const save=()=>ND.ls.set('nd-col',JSON.stringify([...col]));
 feed.addEventListener('click',e=>{const h=e.target.closest('.mhead');if(h){const s=h.parentNode,k=s.dataset.m,on=!col.has(k);on?col.add(k):col.delete(k);save();s.classList.toggle('collapsed',on);h.setAttribute('aria-expanded',String(!on));return}
  const sb=e.target.closest('.shbar');if(sb){const l=sb.nextElementSibling,o=l.hidden;l.hidden=!o;sb.setAttribute('aria-expanded',String(o));$('.sop',sb).textContent=o?'收起':'展开';return}
  const u=e.target.closest('[data-unfold]');if(u){const id=u.dataset.unfold;ND.fold(id,false).then(r=>{if(r){const c=$(`#feed .item[data-id="${id}"]`);c&&!ND.RM()&&c.classList.add('fold-in')}})}});
 $('#allFold')&&($('#allFold').onclick=()=>{const ks=$$('.module',feed).map(s=>s.dataset.m),any=ks.some(k=>!col.has(k));ks.forEach(k=>any?col.add(k):col.delete(k));save();
  $$('.module',feed).forEach(s=>{s.classList.toggle('collapsed',any);$('.mhead',s).setAttribute('aria-expanded',String(!any))})});
 $('#tagf')&&$('#tagf').addEventListener('click',e=>{const b=e.target.closest('.chip');if(!b)return;tag=b.dataset.t;all()});
 ND.on(ev=>{if(['fold','del','vote'].includes(ev.type))all()});
})();
