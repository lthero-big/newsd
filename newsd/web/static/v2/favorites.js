/* 收藏: server-rendered cards (favorites + liked), client-side filter / sort / day grouping / calendar of favourite days. */
(function(){
 const {$,$$}=ND,grid=$('#favs');let sel=null,sort='fav',C;
 const cards=()=>$$('.item',grid);
 cards().forEach((c,i)=>{c.dataset.ord=i;c.dataset.q=c.textContent.toLowerCase();if(c.dataset.fav!=='1'&&!c.dataset.fday)c.dataset.fday=''});
 const isFav=c=>c.dataset.fav==='1',liked=c=>c.dataset.vote==='1';
 const favDays=()=>{const o={};cards().filter(c=>isFav(c)&&!c.dataset.gone&&c.dataset.fday).forEach(c=>{o[c.dataset.fday]=(o[c.dataset.fday]||0)+1});return o};
 function render(){
  const inc=$('#incLike').getAttribute('aria-checked')==='true',m=$('#fmod').value,t=$('#ftag').value,q=$('#fq').value.trim().toLowerCase();
  $$('.fav-day',grid).forEach(x=>x.remove());
  const all=cards(),vis=all.filter(c=>!c.dataset.gone&&(isFav(c)||(inc&&liked(c)))&&(!m||c.dataset.mod===m)&&(!t||c.dataset.tags.split('|').includes(t)||c.dataset.group===t)&&(!q||c.dataset.q.includes(q))&&(!sel||(isFav(c)&&c.dataset.fday===sel)));
  all.forEach(c=>{c.hidden=!vis.includes(c)});
  vis.sort((a,b)=>sort==='fav'?(isFav(b)-isFav(a))||(a.dataset.ord-b.dataset.ord):(b.dataset.pub||'').localeCompare(a.dataset.pub||'')||(a.dataset.ord-b.dataset.ord));
  let last='';vis.forEach(c=>{const g=sort==='fav'?(isFav(c)?'收藏于 '+c.dataset.fday:'仅点赞'):'发布于 '+(c.dataset.pub||'未知日期');
   if(g!==last){const h=document.createElement('div');h.className='fav-day';h.textContent=g;grid.append(h);last=g}grid.append(c)});
  all.filter(c=>c.hidden).forEach(c=>grid.append(c));
  const e=$('#empty'),any=all.some(c=>isFav(c)&&!c.dataset.gone);e.hidden=!!vis.length;
  e.innerHTML=any?'没有符合筛选的收藏。':'还没有收藏，去 <a href="/" style="color:var(--accent)">最新一期</a> 点卡片上的收藏图标吧。';if(C)C.redraw()}
 if(window.DDCal)C=DDCal({calbox:$('.ical .calbox'),days:favDays,sel:null,disableEmpty:true,unit:'有收藏的日子',dotLabel:'收藏',allSub:'ALL · 收藏',countLabel:c=>`，${c} 个收藏`,emptyLabel:'，无收藏',onSelect:s=>{sel=s;render()}});
 $('#sort').addEventListener('click',e=>{const b=e.target.closest('[data-s]');if(!b)return;sort=b.dataset.s;$$('#sort button').forEach(x=>x.setAttribute('aria-checked',String(x===b)));render()});
 ['fmod','ftag'].forEach(i=>$('#'+i).onchange=render);$('#fq').oninput=render;$('#incLike').addEventListener('toggle',render);
 ND.on(ev=>{if(ev.type==='del'){$$(`.item[data-id="${ev.id}"]`,grid).forEach(c=>{if(ev.deleted)c.dataset.gone='1';else delete c.dataset.gone})}
  if(ev.type==='fav'){const today=new Date().toISOString().slice(0,10);$$(`.item[data-id="${ev.id}"]`,grid).forEach(c=>{if(ev.fav&&!c.dataset.fday)c.dataset.fday=today})}
  if(['del','fav','vote'].includes(ev.type))render()});
 render();
})();
