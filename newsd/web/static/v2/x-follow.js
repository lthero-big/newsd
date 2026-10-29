/* DEPTAI-267 「X 关注」: add X accounts (@handle) or keywords that Grok Bot reads for you. Only rendered when the
   admin switched the X source on for this user (the server answers 404 otherwise, and the card stays hidden). */
(function(){
 const {$,esc,api}=ND,box=$('#xFollow');if(!box)return;
 function draw(j){$('#xList').innerHTML=j.follows.map(f=>`<span class="tag plain">${esc(f.name)} <button class="linkish" type="button" data-k="${esc(f.key)}" aria-label="移除 ${esc(f.name)}">✕</button></span>`).join(' ')||'<span class="muted">还没有添加。</span>';
  $('#xMeta').textContent=`${j.follows.length} / ${j.limit}`}
 async function load(){const [ok,j]=await api('GET','/api/me/x');if(!ok){box.hidden=true;return}box.hidden=false;draw(j)}
 $('#xAdd').onclick=async()=>{const v=$('#xIn').value.trim();if(!v)return;const [ok,j]=await api('POST','/api/me/x',{value:v});
  if(!ok)return ND.toast('添加失败：'+(j.detail||j.error||''));$('#xIn').value='';draw(j);ND.toast('已添加，Grok Bot 下次抓取时会读取');setTimeout(()=>location.reload(),600)};
 $('#xIn').addEventListener('keydown',e=>{if(e.key==='Enter')$('#xAdd').click()});
 $('#xList').addEventListener('click',async e=>{const b=e.target.closest('[data-k]');if(!b)return;const [ok,j]=await api('DELETE','/api/me/x/'+b.dataset.k);
  if(!ok)return ND.toast('移除失败');draw(j);setTimeout(()=>location.reload(),400)});
 load();
})();
