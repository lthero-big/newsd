/* DEPTAI-267 admin switch for the X source fed by Grok Bot (GET/PUT /api/admin/x-import). */
(function(){
 const {$,api}=ND,box=$('#xAdm');if(!box)return;
 function draw(j){$('#xaOn').setAttribute('aria-checked',String(j.enabled));$('#xaScope').value=j.scope;$('#xaUsers').value=(j.users||[]).join(',');
  $('#xaMax').value=j.max_per_user;$('#xaCap').value=j.daily_cap;$('#xaUsersRow').hidden=j.scope!=='users';
  $('#xaUse').textContent=`今日已用 ${j.usage.calls} / ${j.daily_cap} 次读取，新入库 ${j.usage.items} 条`+(j.exhausted?' · 今日额度已用完，后续抓取会跳过':'');
  $('#xaUse').className=j.exhausted?'dn':'muted'}
 async function load(){const [ok,j]=await api('GET','/api/admin/x-import');if(ok)draw(j)}
 $('#xaOn').onclick=()=>{const b=$('#xaOn');b.setAttribute('aria-checked',String(b.getAttribute('aria-checked')!=='true'))};
 $('#xaScope').onchange=()=>{$('#xaUsersRow').hidden=$('#xaScope').value!=='users'};
 $('#xaSave').onclick=async()=>{const body={enabled:$('#xaOn').getAttribute('aria-checked')==='true',scope:$('#xaScope').value,
   users:$('#xaUsers').value.split(/[,，\s]+/).filter(Boolean).map(Number),max_per_user:+$('#xaMax').value,daily_cap:+$('#xaCap').value};
  const [ok,j]=await api('PUT','/api/admin/x-import',body);if(!ok)return ND.toast('保存失败：'+(j.detail&&JSON.stringify(j.detail)||''));draw(j);ND.toast('X 信息源设置已保存')};
 load();
})();
