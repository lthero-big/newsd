/* 3c 用户与分组: groups list, group header, filterable users table with bulk actions, detail drawer,
   group dialog (permissions matrix, default sources, quota), e-mail invites, registration mode. */
(function(){
 const {$,$$,esc,api}=ND,I=window.ND_USERS,q0=new URLSearchParams(location.search),PS=12;
 let U=[],INV=[],G=[],PERMS=I.groups.perms,COLORS=I.groups.colors,g=q0.get('g')||'all',page=0,cur=null;const sel=new Set();
 if(q0.get('status'))$('#uSt').value=q0.get('status');
 const RN={admin:'管理员',user:'成员',readonly:'只读'},STN={active:'正常',invited:'已邀请',disabled:'已停用'};
 const SRC=Object.fromEntries(I.catalog.map(s=>[s.key,s]));
 const gOf=id=>G.find(x=>x.id===+id),ago=t=>{if(!t)return '—';const h=(Date.now()-Date.parse(t+'Z'))/36e5;return h<1?'刚刚':h<24?Math.floor(h)+' 小时前':Math.floor(h/24)+' 天前'};
 const hue=s=>COLORS[[...s].reduce((a,c)=>a+c.charCodeAt(0),0)%COLORS.length];
 const gtag=id=>{const x=gOf(id);return x?`<span class="gtag" style="--gc:${x.color}"><i></i>${esc(x.name)}</span>`:''};
 function load(P,GR){G=GR.groups;U=P.users.map(u=>({...u,kind:'u',name:u.username}));
  INV=P.invites.map(i=>({id:'i'+i.id,iid:i.id,kind:'i',name:(i.email||'').split('@')[0],email:i.email,groups:i.group_id?[i.group_id]:[],role:(gOf(i.group_id)||{role:'user'}).role,status:'invited',last_seen_at:null,created_at:i.created_at,expires_at:i.expires_at}))}
 async function reload(){const [a,P]=await api('GET','/api/admin/people'),[b,GR]=await api('GET','/api/admin/groups');if(a&&b){load(P,GR);all()}else ND.toast('刷新失败')}
 const everyone=()=>U.concat(INV);
 function glist(){const ung=U.filter(u=>!u.groups.length).length,n=id=>U.filter(u=>u.groups.includes(id)).length;
  $('#glist').innerHTML=[['all','全部用户','var(--ink)',U.length],...G.map(x=>[x.id,x.name,x.color,n(x.id)]),['none','未分组','var(--line)',ung]]
   .map(([id,nm,c,k])=>`<button role="tab" type="button" aria-selected="${String(g)===String(id)}" data-g="${id}" style="--gc:${c}"><i></i>${esc(nm)}<small>${k}</small></button>`).join('');
  $('#bG').innerHTML='<option value="">加入分组…</option>'+G.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('');
  $('#iG').innerHTML='<option value="">不加入分组</option>'+G.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('')}
 const permChips=set=>PERMS.map(p=>`<span class="perm ${set.has(p.key)?'on':'off'}">${set.has(p.key)?'✓':'✕'} ${esc(p.name)}</span>`).join('');
 function head(){const x=gOf(g);
  if(x){const srcs=(x.default_sources||[]).map(k=>SRC[k]?SRC[k].name:k);
   $('#ghead').innerHTML=`<div class="ghead"><span style="width:14px;height:14px;border-radius:4px;background:${x.color};margin-top:10px;flex:none"></span><div style="flex:1;min-width:0"><h2>${esc(x.name)} <span class="tag plain">${U.filter(u=>u.groups.includes(x.id)).length} 人</span></h2><div class="muted" style="font-size:13px;margin-top:4px">${esc(x.description||'—')}</div>
    <div style="margin-top:10px">${permChips(new Set(x.perms))}</div>
    <div class="muted" style="font-size:12px;margin-top:10px;overflow-wrap:anywhere">默认角色 <b>${RN[x.role]||x.role}</b> · 默认订阅源 <b>${srcs.length?esc(srcs.slice(0,6).join('、'))+(srcs.length>6?` 等 ${srcs.length} 个`:''):'无'}</b> · 配额 <b>${x.quota?'每日 '+x.quota+' 条':'不限'}</b></div></div><button class="btn sm" id="editG" type="button">编辑分组</button></div>`}
  else $('#ghead').innerHTML=`<div class="ghead"><div><h2>${g==='none'?'未分组':'全部用户'} <span class="tag plain">${g==='none'?U.filter(u=>!u.groups.length).length:U.length} 人</span></h2><div class="muted" style="font-size:13px;margin-top:4px">${g==='none'?'未分组的用户保持默认权限：阅读、管理自己的订阅源、配置模型、推送设置。选中后用「加入分组」批量归组。':'所有用户。点左侧分组看组权限、默认源和配额；已邀请但还没注册的也列在这里。'}</div></div></div>`}
 function list(){const qq=$('#uq').value.trim().toLowerCase(),st=$('#uSt').value,ro=$('#uRole').value,so=$('#uSort').value;
  let L=everyone().filter(u=>(g==='all'||(g==='none'?!u.groups.length:u.groups.includes(+g)))&&(!qq||(u.name+' '+(u.email||'')).toLowerCase().includes(qq))&&(!st||u.status===st)&&(!ro||u.role===ro));
  L.sort((a,b)=>so==='name'?a.name.localeCompare(b.name):so==='created'?String(b.created_at).localeCompare(String(a.created_at)):String(b.last_seen_at||'').localeCompare(String(a.last_seen_at||'')));return L}
 function rows(){const L=list(),pages=Math.max(1,Math.ceil(L.length/PS));page=Math.min(page,pages-1);const P=L.slice(page*PS,page*PS+PS);
  $('#uN').textContent=L.length+' 人';$('#pinfo').textContent=`第 ${page+1} / ${pages} 页 · 每页 ${PS}`;$('#pp').disabled=!page;$('#pn').disabled=page>=pages-1;
  $('#rows').innerHTML=P.map(u=>`<tr class="r${cur&&cur.id===u.id?' sel':''}" data-u="${u.id}"><td>${u.kind==='u'?`<input type="checkbox" aria-label="选择 ${esc(u.name)}" ${sel.has(u.id)?'checked':''}>`:''}</td>
   <td><div class="ucell"><span class="uav" style="--uc:${hue(u.name||'?')}">${esc((u.name||'?').slice(0,2).toUpperCase())}</span><div style="min-width:0"><b>${esc(u.name)}${u.id===I.me?' <span class="tag plain">我</span>':''}</b><small>${esc(u.email||'—')}</small></div></div></td>
   <td>${u.groups.map(gtag).join('')||'<span class="muted" style="font-size:12px">未分组</span>'}</td>
   <td class="c-role">${RN[u.role]||u.role}</td><td><span class="st-dot ${u.status==='invited'?'inv':u.status==='disabled'?'off':''}"><i></i>${STN[u.status]}</span></td><td class="c-last muted mono" style="font-size:12px">${u.kind==='i'?'未注册':ago(u.last_seen_at)}</td></tr>`).join('')||'<tr><td colspan="6" class="empty">没有符合条件的用户</td></tr>';
  const pu=P.filter(u=>u.kind==='u');$('#all').checked=pu.length&&pu.every(u=>sel.has(u.id));$('#bulk').hidden=!sel.size;$('#bN').textContent=`已选 ${sel.size} 人`}
 const all=()=>{glist();head();rows()};
 // drawer
 const dw=$('#drawer');
 async function drawer(u){cur=u;dw.classList.add('open');dw.setAttribute('aria-hidden','false');$('#dwT').textContent=u.name;rows();
  if(u.kind==='i'){$('#dwB').innerHTML=`<dl class="kv"><dt>邮箱</dt><dd>${esc(u.email)}</dd><dt>分组</dt><dd>${u.groups.map(gtag).join('')||'—'}</dd><dt>邀请时间</dt><dd>${esc((u.created_at||'').slice(0,16).replace('T',' '))}</dd><dt>过期</dt><dd>${esc((u.expires_at||'—').slice(0,16).replace('T',' '))}</dd></dl><p class="muted" style="font-size:13px">邀请码只在生成时显示一次；如果对方弄丢了，撤销后重新邀请。</p>`;
   $('#dwF').innerHTML='<span style="flex:1"></span><button class="btn sm danger" data-d="revoke" type="button">撤销邀请</button>';return}
  $('#dwB').innerHTML='<p class="muted">加载中…</p>';$('#dwF').innerHTML='';
  const [ok,d]=await api('GET',`/api/admin/users/${u.id}/detail`);if(!ok||cur!==u){if(!ok)$('#dwB').innerHTML='<p class="muted">加载失败</p>';return}
  const ps=new Set(d.perms);
  $('#dwB').innerHTML=`<div class="ucell" style="margin-bottom:14px"><span class="uav" style="--uc:${hue(u.name)};width:48px;height:48px;font-size:15px">${esc(u.name.slice(0,2).toUpperCase())}</span><div><b style="font-size:16px">${esc(d.username)}</b><small>${esc(d.email||'—')}</small></div></div>
   <dl class="kv"><dt>状态</dt><dd>${d.active?'正常':'已停用'}</dd><dt>角色</dt><dd>${RN[d.role]||d.role}</dd><dt>加入时间</dt><dd>${esc(d.created_at.slice(0,10))}</dd><dt>最近活跃</dt><dd>${ago(d.last_seen_at)}</dd><dt>30 天活跃</dt><dd>${d.active_days_30} 天</dd><dt>收藏 / 删除</dt><dd>${d.favorites} / ${d.trash}</dd><dt>在线会话</dt><dd>${d.sessions}</dd></dl>
   <h4>分组</h4><div>${d.groups.map(gtag).join('')||'<span class="muted">未分组（默认权限）</span>'}</div>
   <h4>生效权限${d.role==='admin'?'（管理员：全部）':d.groups.length?'（各组并集）':'（默认）'}</h4><div>${permChips(ps)}</div>
   <h4>最近活动</h4>${d.activity.length?`<ul class="rel">${d.activity.map(a=>`<li><span class="muted">${esc(a.at.slice(5,16).replace('T',' '))}</span><span>${esc(a.message)}</span></li>`).join('')}</ul>`:'<p class="muted" style="font-size:13px">还没有记录</p>'}`;
  const self=u.id===I.me;
  $('#dwF').innerHTML=`<button class="btn sm" data-d="reset_password" type="button" ${self?'disabled':''}>重置密码</button><button class="btn sm" data-d="logout" type="button">强制下线</button><span style="flex:1"></span>${d.active?`<button class="btn sm danger" data-d="disable" type="button" ${self?'disabled':''}>停用</button>`:'<button class="btn sm" data-d="enable" type="button">启用</button>'}`}
 const closeDw=()=>{dw.classList.remove('open');dw.setAttribute('aria-hidden','true');cur=null;rows()};
 $('#dwX').onclick=closeDw;document.addEventListener('keydown',e=>{if(e.key==='Escape'&&cur&&!$('.overlay.open'))closeDw()});
 async function bulk(ids,action,extra,label){const [ok,j]=await api('POST','/api/admin/users/bulk',{ids,action,...extra});
  if(!ok){ND.error(j);return false}
  if(j.passwords){$('#pList').innerHTML=Object.entries(j.passwords).map(([n,p])=>`<div class="row sp" style="padding:6px 0;border-bottom:1px solid var(--line)"><b>${esc(n)}</b><code class="mono">${esc(p)}</code></div>`).join('');
   $('#pCopy').onclick=()=>{navigator.clipboard?.writeText(Object.entries(j.passwords).map(([n,p])=>n+'\t'+p).join('\n')).then(()=>ND.toast('已复制'),()=>ND.toast('复制失败，请手动选择'))};$('#pdlg').classList.add('open')}
  else ND.toast(label||'已完成');await reload();if(cur&&cur.kind==='u'){const u=U.find(x=>x.id===cur.id);if(u)drawer(u)}return true}
 $('#dwF').addEventListener('click',async e=>{const b=e.target.closest('[data-d]');if(!b||!cur)return;const a=b.dataset.d,u=cur;
  if(a==='revoke'){const [ok,j]=await api('DELETE','/api/admin/invites/'+u.iid);if(ok){ND.toast('已撤销邀请');closeDw();reload()}else ND.error(j);return}
  if(a==='reset_password'&&!confirm(`给 ${u.name} 生成临时密码？对方的所有会话会下线。`))return;
  bulk([u.id],a,{},{logout:'已强制下线 '+u.name,disable:'已停用 '+u.name,enable:'已启用 '+u.name}[a])});
 // list events
 $('#glist').addEventListener('click',e=>{const b=e.target.closest('[data-g]');if(b){g=b.dataset.g;page=0;sel.clear();history.replaceState(null,'','?g='+g);all()}});
 $('#rows').addEventListener('click',e=>{const tr=e.target.closest('tr.r');if(!tr)return;const u=everyone().find(x=>String(x.id)===tr.dataset.u);if(!u)return;
  if(e.target.matches('input[type=checkbox]')){e.target.checked?sel.add(u.id):sel.delete(u.id);rows();return}drawer(u)});
 $('#all').onchange=e=>{list().slice(page*PS,page*PS+PS).filter(u=>u.kind==='u').forEach(u=>e.target.checked?sel.add(u.id):sel.delete(u.id));rows()};
 $('#uq').oninput=()=>{page=0;rows()};['uSt','uRole','uSort'].forEach(k=>$('#'+k).onchange=()=>{page=0;rows()});
 $('#pp').onclick=()=>{page--;rows()};$('#pn').onclick=()=>{page++;rows()};
 const S=()=>[...sel];
 $('#bG').onchange=async e=>{const id=+e.target.value;e.target.value='';if(id)bulk(S(),'add_group',{group_id:id},`已把 ${sel.size} 人加入「${gOf(id).name}」`)};
 $('#bR').onchange=async e=>{const r=e.target.value;e.target.value='';if(r&&confirm(`把 ${sel.size} 人改为${RN[r]}？`))bulk(S(),'role',{role:r},`已改为${RN[r]}`)};
 $('#bulk').addEventListener('click',async e=>{const b=e.target.closest('[data-b]');if(!b)return;const k=b.dataset.b,n=sel.size;
  if(k==='clr'){sel.clear();rows();return}
  if(k==='remove_group'){const x=gOf(g);if(!x)return ND.toast('先在左侧选一个分组');if(await bulk(S(),'remove_group',{group_id:x.id},`已把 ${n} 人移出「${x.name}」`))sel.clear(),rows();return}
  if(k==='reset_password'&&!confirm(`给 ${n} 人生成临时密码？他们的会话会全部下线。`))return;
  if(k==='disable'&&!confirm(`停用 ${n} 人？他们会立刻下线，日报也不再生成。`))return;
  if(await bulk(S(),k,{},{disable:`已停用 ${n} 人`,enable:`已启用 ${n} 人`}[k])&&k!=='reset_password'){sel.clear();rows()}});
 // registration mode
 $('#reg').onchange=async e=>{const r=await fetch('/api/admin/registration',{method:'POST',headers:{'content-type':'application/x-www-form-urlencoded'},body:new URLSearchParams({mode:e.target.value})});ND.toast(r.ok?'注册方式已保存':'保存失败')};
 // group dialog
 let eg=null,ecol=COLORS[0],esrc=[];
 const srcChips=()=>{$('#gSrc').innerHTML=esrc.map(k=>`<span class="chip" style="margin:0 6px 6px 0">${esc(SRC[k]?I.cats[SRC[k].cat]+' · '+SRC[k].name:k)} <button class="linkish" data-rm="${esc(k)}" type="button" aria-label="移除">✕</button></span>`).join('')||'<span class="muted" style="font-size:12px">无</span>'};
 $('#gSrcL').innerHTML=I.catalog.map(s=>`<option value="${esc(s.key)}">${esc(I.cats[s.cat]+' · '+s.name)}</option>`).join('');
 $('#gSrcQ').addEventListener('keydown',e=>{if(e.key!=='Enter')return;e.preventDefault();const v=e.target.value.trim(),s=SRC[v]||I.catalog.find(x=>x.name===v);if(!s)return ND.toast('没有这个源');if(!esrc.includes(s.key))esrc.push(s.key);e.target.value='';srcChips()});
 $('#gSrcQ').addEventListener('change',e=>{const s=SRC[e.target.value.trim()];if(s){if(!esrc.includes(s.key))esrc.push(s.key);e.target.value='';srcChips()}});
 $('#gSrc').addEventListener('click',e=>{const b=e.target.closest('[data-rm]');if(b){esrc=esrc.filter(k=>k!==b.dataset.rm);srcChips()}});
 function gdlg(x){eg=x;$('#gT').textContent=x?'编辑分组 · '+x.name:'新建分组';$('#gName').value=x?x.name:'';$('#gDesc').value=x?x.description:'';$('#gRole').value=x?x.role:'user';$('#gQ').value=x&&x.quota?x.quota:'';ecol=x?x.color:COLORS[4];esrc=x?[...x.default_sources]:[];$('#gDel').hidden=!x;srcChips();
  $('#gCol').innerHTML=COLORS.map(c=>`<button type="button" class="orb" data-c="${c}" aria-label="颜色 ${c}" aria-pressed="${c===ecol}" style="--ob:${c};--oa:${c};${c===ecol?'box-shadow:0 0 0 2px var(--bg),0 0 0 4px var(--ink)':''}"><i></i></button>`).join('');
  const ps=new Set(x?x.perms:['read','src']);$('#gPerm').innerHTML='<tr><th>权限</th><th>允许</th></tr>'+PERMS.map(p=>`<tr><td>${esc(p.name)}${p.admin_only?' <span class="muted" style="font-size:12px">仅管理员角色</span>':''}</td><td><button class="switch" type="button" role="switch" aria-checked="${!p.admin_only&&(p.key==='read'||ps.has(p.key))}" data-p="${p.key}" aria-label="${esc(p.name)}" ${p.admin_only||p.key==='read'?'disabled':''}></button></td></tr>`).join('');$('#gdlg').classList.add('open');setTimeout(()=>$('#gName').focus(),50)}
 $('#gCol').addEventListener('click',e=>{const b=e.target.closest('[data-c]');if(b){ecol=b.dataset.c;$$('#gCol .orb').forEach(o=>{o.style.boxShadow=o===b?'0 0 0 2px var(--bg),0 0 0 4px var(--ink)':'';o.setAttribute('aria-pressed',o===b)})}});
 $('#ghead').addEventListener('click',e=>{if(e.target.id==='editG')gdlg(gOf(g))});$('#newG').onclick=()=>gdlg(null);
 $('#gSave').onclick=async()=>{const name=$('#gName').value.trim();if(!name)return ND.toast('请填写分组名称');
  const perms=$$('#gPerm [data-p]').filter(b=>b.getAttribute('aria-checked')==='true').map(b=>b.dataset.p),q=$('#gQ').value.trim();
  const v={name,description:$('#gDesc').value.trim(),role:$('#gRole').value,color:ecol,perms,default_sources:esrc,quota:q?+q:(eg?0:null)};
  const [ok,j]=await api(eg?'PATCH':'POST',eg?'/api/admin/groups/'+eg.id:'/api/admin/groups',v);if(!ok)return ND.error(j,'保存失败');
  $('#gdlg').classList.remove('open');ND.toast(eg?'已保存分组':'已新建分组「'+name+'」');if(!eg)g=String(j.id);reload()};
 $('#gDel').onclick=async()=>{const x=eg;if(!x||!confirm(`删除分组「${x.name}」？成员保留，只是不再属于这个组。`))return;const [ok,j]=await api('DELETE','/api/admin/groups/'+x.id);if(!ok)return ND.error(j);g='all';$('#gdlg').classList.remove('open');ND.toast('已删除分组，成员保留');reload()};
 // invites
 const link=c=>location.origin+'/register?invite='+encodeURIComponent(c);
 const iReset=()=>{$('#iMail').value='';$('#iBody').hidden=false;$('#iOut').hidden=true;$('#iSend').hidden=false;$('#iG').value=g!=='all'&&g!=='none'?g:''};
 $('#invite').onclick=()=>{iReset();$('#idlg').classList.add('open')};
 async function invite(body){const [ok,j]=await api('POST','/api/admin/invites/batch',{group_id:+$('#iG').value||null,days:+$('#iDays').value,...body});if(!ok){ND.error(j,'邀请失败');return null}return j}
 $('#iSend').onclick=async()=>{const ms=$('#iMail').value.split(/[\s,，;；]+/).filter(Boolean);if(!ms.length)return ND.toast('请填写邮箱');const j=await invite({emails:ms});if(!j)return;
  $('#iBody').hidden=true;$('#iSend').hidden=true;$('#iOut').hidden=false;
  $('#iOut').innerHTML=`<p class="muted" style="margin-top:0;font-size:13px">已生成 ${j.invites.length} 个邀请${j.group?'（注册后加入「'+esc(j.group)+'」）':''}，${j.expires_days} 天内有效。链接<b>只显示这一次</b>，请现在复制发给对方。</p>`+j.invites.map(x=>`<div class="field"><label>${esc(x.email)}</label><input class="input mono" readonly value="${esc(link(x.code))}" onfocus="this.select()"></div>`).join('')+'<button class="btn sm" id="iCopyAll" type="button">全部复制</button>';
  $('#iCopyAll').onclick=()=>navigator.clipboard?.writeText(j.invites.map(x=>x.email+'\t'+link(x.code)).join('\n')).then(()=>ND.toast('已复制'),()=>ND.toast('复制失败，请手动选择'));reload()};
 $('#iLink').onclick=async()=>{const j=await invite({count:1});if(!j)return;const l=link(j.invites[0].code);
  try{await navigator.clipboard.writeText(l);ND.toast('邀请链接已复制（一次性）')}catch(e){$('#iBody').hidden=true;$('#iOut').hidden=false;$('#iOut').innerHTML=`<div class="field"><label>一次性邀请链接</label><input class="input mono" readonly value="${esc(l)}" onfocus="this.select()"></div>`}};
 load(I.people,I.groups);all();
})();
