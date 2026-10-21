/* 4b 版本与更新: latest GitHub release, release notes, one-click update (local: git + migrate + restart; docker: updater sidecar). */
(function(){
 const {$,esc,api}=ND,box=$('#updBody'),meta=$('#updMeta');if(!box)return;
 const MODE={docker:'Docker',local:'本机',vercel:'Vercel',none:'已关闭'};
 const err=(j,w)=>ND.toast((w||'操作失败')+'：'+((j&&(j.error||j.detail))||'未知错误'));
 const when=s=>s?s.replace('T',' ').slice(0,16)+' UTC':'—';
 let poll=null,D=null;
 function steps(job){return `<ol class="upd-steps">${(job.steps||[]).map(s=>`<li class="${s.level==='error'?'e':s.level==='warn'?'w':''}"><span class="mono">${esc((s.at||'').slice(11,19))}</span> ${esc(s.msg)}</li>`).join('')}</ol>`}
 function render(){const d=D,job=d.job||{},busy=job.state==='running'||job.state==='restarting';
  meta.textContent=`当前 v${d.current} · ${MODE[d.mode]||d.mode}部署`;
  let h='';
  if(d.has_update)h+=`<div class="row" style="gap:10px;flex-wrap:wrap;align-items:center"><span class="tag ok">有新版本</span><b style="color:var(--ink)">${esc(d.name||d.latest)}</b><span class="mono" style="font-size:12px">${esc(when(d.published_at))}</span>${d.url?`<a href="${esc(d.url)}" target="_blank" rel="noopener">发布说明 ↗</a>`:''}</div>`+
   (d.notes?`<details class="upd-notes"${busy?'':' open'}><summary>更新说明</summary><pre>${esc(d.notes)}</pre></details>`:'')+
   (d.can_apply?`<div class="row" style="gap:8px;margin-top:10px;flex-wrap:wrap"><button class="btn primary sm" id="updGo" type="button"${busy?' disabled':''}>更新到 ${esc(d.latest)}</button><span class="muted" style="font-size:12px">${d.mode==='docker'?'拉取新镜像并依次重建 web、worker，web 启动时自动迁移数据库。':'快进到新版本、备份并迁移数据库，然后自动重启 web 和 worker。'}更新期间页面会短暂不可用。</span></div>`
    :`<p class="muted" style="margin:10px 0 0;font-size:13px">${esc(d.reason)}</p>`);
  else if(d.latest)h+=`<span class="tag plain">已是最新</span> <span style="font-size:13px">最新发布 ${esc(d.latest)}</span>`;
  else if(d.error)h+=`<span class="tag warn">检查失败</span> <span class="mono" style="font-size:12px">${esc(d.error)}</span>`;
  else if(!d.check_enabled)h+='已通过 NEWSD_UPDATE_CHECK=0 关闭版本检查。';
  else h+='还没有发布版本。';
  if(job.state){const st={running:['warn','更新中'],restarting:['warn','正在重启'],done:['ok','更新完成'],failed:['err','更新失败']}[job.state]||['plain',job.state];
   h+=`<div class="upd-job"><div class="row" style="gap:8px;align-items:center"><span class="tag ${st[0]}">${st[1]}</span><span style="font-size:13px">${esc(job.from||'')} → ${esc(job.target||'')} · ${esc(job.by||'')} · ${esc(when(job.started_at))}</span></div>${steps(job)}</div>`}
  h+=`<div class="row" style="gap:8px;margin-top:10px;align-items:center"><button class="btn sm ghost" id="updChk" type="button">检查更新</button><span class="muted mono" style="font-size:12px">上次检查 ${esc(when(d.checked_at))}</span></div>`;
  box.className='';box.innerHTML=h;
  $('#updChk').onclick=async e=>{e.target.disabled=true;const [ok,j]=await api('POST','/api/admin/update/check');e.target.disabled=false;if(!ok)return err(j,'检查失败');D=j;render();ND.toast(j.has_update?`发现新版本 ${j.latest}`:'已是最新')};
  const go=$('#updGo');if(go)go.onclick=async()=>{if(!confirm(`更新到 ${d.latest}？\n\n更新前会自动备份 SQLite 数据库（Postgres 请先自行 pg_dump）。更新期间页面会短暂不可用。`))return;
   go.disabled=true;const [ok,j]=await api('POST','/api/admin/update/apply',{version:d.latest});if(!ok){go.disabled=false;return err(j,'无法更新')}D.job=j;render();watch()};
  if(busy)watch()}
 async function load(){const [ok,j]=await api('GET','/api/admin/update');if(!ok){box.textContent='无法获取版本信息';return}D=j;render();return j}
 function watch(){if(poll)return;poll=setInterval(async()=>{const before=D&&D.current;let j;try{j=await load()}catch(e){return}
  if(!j)return;const s=(j.job||{}).state;if(s!=='running'&&s!=='restarting'){clearInterval(poll);poll=null;if(s==='done'&&before!==j.current)ND.toast(`已更新到 v${j.current}`)}},2500)}
 load();
})();
