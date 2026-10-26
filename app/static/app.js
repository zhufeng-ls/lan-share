'use strict';
import {captureDroppedItems, collectDroppedItems} from './upload-tree.mjs';
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const shapes = {
  folder:'<path d="M3 7V5a2 2 0 0 1 2-2h5l3 4h6a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/>',
  copy:'<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
  file:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M8 13h8M8 17h5"/>',
  home:'<path d="m3 10 9-7 9 7v10H3Z"/><path d="M9 20v-7h6v7"/>',
  grid:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  users:'<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6M19 21v-3a6 6 0 0 0-2-4"/>',
  upload:'<path d="M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6"/>',
  download:'<path d="M12 3v13m-5-5 5 5 5-5M4 15v6h16v-6"/>',
  link:'<path d="m10 13 4-4M8 16l-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m2 1 2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0" transform="translate(1 0)"/>',
  edit:'<path d="m15 4 5 5M3 21l5-1L21 7a2 2 0 0 0-5-5L3 15Z"/>',
  search:'<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
  star:'<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9Z"/>',
  refresh:'<path d="M20 7v5h-5M4 17v-5h5M6 5a8 8 0 0 1 14 7M4 12a8 8 0 0 0 14 7"/>',
  shield:'<path d="m12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6Z"/><path d="m8 12 3 3 5-6"/>',
  image:'<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 6-6 4 4 3-3 5 5"/>',
};
const icon = n => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${shapes[n] || shapes.file}</svg>`;
$$('[data-icon]').forEach(el => el.innerHTML = icon(el.dataset.icon));
const state = {me:null, dirs:[], did:'', path:'', view:'files', items:[], search:'', file:'', renderId:0, uploading:false};
let toastTimer, currentUpload, uploadCancelled=false;
let pinUpdatePending = false;
function toast(message) { $('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden = true, 4500); }
async function api(url, options = {}) {
  const headers = {'X-Lan-Share':'1', ...options.headers};
  if (options.json !== undefined) { headers['Content-Type']='application/json'; options.body=JSON.stringify(options.json); delete options.json; }
  const response = await fetch(url, {...options, headers});
  if (!response.ok) {
    let body; try { body = await response.json(); } catch { body = {}; }
    if (response.status === 401) { $('#shell').hidden = true; $('#admin-shell').hidden = true; $('#welcome').hidden = false; }
    const error = new Error(typeof body.detail === 'string' ? body.detail : `操作失败（${response.status}）`); error.status=response.status; throw error;
  }
  return response.json();
}
const endpoint = (kind, did=state.did) => `/api/directories/${encodeURIComponent(did)}/${kind}`;
const date = value => new Date(value*1000).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
const size = value => value < 1024 ? `${value} B` : value < 1048576 ? `${(value/1024).toFixed(1)} KiB` : value < 1073741824 ? `${(value/1048576).toFixed(1)} MiB` : `${(value/1073741824).toFixed(2)} GiB`;
const joinPath = name => state.path ? `${state.path}/${name}` : name;
const leafName = value => value && value !== '.' && value !== '..' && !/[\\/\x00-\x1f]/.test(value);
const isText = name => /\.(txt|md|json|csv|tsv|yaml|yml|toml|ini|conf|cfg|log|py|js|ts|css|html|xml|sh|sql|c|cpp|h|rs|go)$/i.test(name);
function symbol(item) { const type=item.is_dir?'folder': /\.(png|jpe?g|gif|webp|svg|avif)$/i.test(item.name)?'image':isText(item.name)?'text':'file'; return `<span class="file-symbol ${type}">${icon(type==='text'?'file':type)}</span>`; }
function button(id, text, type='secondary', glyph='') { return `<button id="${id}" class="${type}">${glyph?icon(glyph):''}${text}</button>`; }
function safe(action) { return async (...args) => { try { await action(...args); } catch(e) { toast(e.message || '连接失败，请检查服务是否运行'); } }; }
function setURL() { const p=new URLSearchParams(); if(state.view==='files') { p.set('dir',state.did); if(state.path)p.set('path',state.path); if(state.file)p.set('file',state.file); } else p.set('view',state.view); history.replaceState(null,'',`/?${p}`); }
async function metadata() { [state.me,state.dirs]=await Promise.all([api('/api/me'),api('/api/directories')]); sidebar(); }
function sidebar() {
  $('#profile-name').textContent=state.me.user.name; $('#avatar').textContent=Array.from(state.me.user.name)[0]; $('#dir-count').textContent=state.dirs.length;
  $('#pin-list').innerHTML=state.dirs.filter(d=>state.me.pins.includes(d.id)).map(d=>`<button class="pin-button ${state.did===d.id&&state.view==='files'?'active':''}" data-dir="${d.id}" title="${esc(d.name)}">${icon('folder')}<span class="pin-text">${esc(d.name)}</span>${state.me.user.default_id===d.id?'<small>默认</small>':''}</button>`).join('');
  $$('#pin-list button').forEach(b=>b.onclick=safe(()=>navigate('files',b.dataset.dir)));
  $$('[data-view]').forEach(b=>b.classList.toggle('active', b.dataset.view===state.view && (state.view!=='files'||state.did===state.me.user.default_id)));
}
async function navigate(view, did='', path='', file='') { state.view=view; state.did=did||state.me.user.default_id; state.path=path; state.file=file; state.search=''; setURL(); sidebar(); await render(); }
async function start() {
  const adminResponse=await fetch('/api/admin/me');
  if(adminResponse.ok){await showAdmin();return;}
  $('#admin-shell').hidden=true;
  await metadata(); $('#welcome').hidden=true; $('#shell').hidden=false;
  const query=new URLSearchParams(location.search);
  const view=['all','trash','activity'].includes(query.get('view'))?query.get('view'):'files';
  await navigate(view, query.get('dir')||state.me.user.default_id, query.get('path')||'', query.get('file')||'');
}
function resetJoin(name='') {
  $('#join-form').hidden=false;
  $('#join-choice').hidden=true;
  $('#join-choice').innerHTML='';
  $('#join-error').textContent='';
  $('#join-name').value=name;
}
async function submitJoin(body) {
  const result=await api('/api/join',{method:'POST',json:body});
  if(result.status==='admin_password_required')showAdminPassword(body.name);
  else if(result.status==='choose_identity')showJoinChoices(body.name,result.candidates);
  else {await start();resetJoin();}
}
function showJoinChoices(name,candidates) {
  $('#join-form').hidden=true;
  const panel=$('#join-choice');
  panel.hidden=false;
  panel.innerHTML=`<h3>找到已有空间</h3><p>这个名字已被使用。是你自己的空间就继续进入；如果是另一位用户，请返回填写不同名字。</p><div class="identity-list">${candidates.map(candidate=>`<button class="identity-choice" data-user-id="${esc(candidate.id)}"><strong>${esc(candidate.name)}</strong><span>默认目录：${esc(candidate.directory_name||'未设置')}</span><small>创建于 ${esc(new Date(candidate.created*1000).toLocaleString('zh-CN',{hour12:false}))} · ${esc(candidate.id.slice(0,8))}</small><b>继续使用已有空间 →</b></button>`).join('')}</div><button id="join-back" class="text-button wide">返回修改名字</button><p id="choice-error" class="error" role="alert"></p>`;
  const choose=async body=>{
    const buttons=$$('button',panel);buttons.forEach(button=>button.disabled=true);$('#choice-error').textContent='';
    try {await submitJoin(body);}catch(error){if($('#choice-error'))$('#choice-error').textContent=error.message;else $('#join-error').textContent=error.message;}
    finally {buttons.forEach(button=>button.disabled=false);}
  };
  $$('.identity-choice',panel).forEach(button=>button.onclick=()=>choose({name,existing_user_id:button.dataset.userId}));
  $('#join-back').onclick=()=>{resetJoin(name);$('#join-name').focus();};
  $('.identity-choice',panel)?.focus();
}
$('#join-form').onsubmit=async e=>{
  e.preventDefault();const button=$('button',e.target);button.disabled=true;$('#join-error').textContent='';
  try {await submitJoin({name:$('#join-name').value.trim()});}catch(error){$('#join-error').textContent=error.message;}finally{button.disabled=false;}
};
$$('[data-view]').forEach(b=>b.onclick=safe(()=>navigate(b.dataset.view)));
function showAdminPassword(name) {
  $('#join-form').hidden=true;
  const panel=$('#join-choice');panel.hidden=false;
  panel.innerHTML=`<h3>验证账户</h3><p>请输入「${esc(name)}」的密码。</p><form id="admin-login-form"><label for="admin-password">密码</label><input id="admin-password" type="password" autocomplete="current-password" maxlength="256" required><button class="primary wide" type="submit">验证并进入</button><p id="admin-login-error" class="error" role="alert"></p></form><button id="admin-login-back" class="text-button wide">返回修改名字</button>`;
  $('#admin-password').focus();
  $('#admin-login-back').onclick=()=>{resetJoin(name);$('#join-name').focus();};
  $('#admin-login-form').onsubmit=async event=>{
    event.preventDefault();const submit=$('button',event.target);submit.disabled=true;
    try {await api('/api/admin/login',{method:'POST',json:{name,password:$('#admin-password').value}});$('#admin-password').value='';await showAdmin();resetJoin();}
    catch(error){$('#admin-login-error').textContent=error.message;$('#welcome').hidden=false;}
    finally {submit.disabled=false;}
  };
}
let adminView='users';
let adminData=null;
async function showAdmin() {
  const result=await api('/api/admin/state');
  adminData=result;
  $('#welcome').hidden=true;$('#shell').hidden=true;$('#admin-shell').hidden=false;
  history.replaceState(null,'','/?view=admin');
  renderAdmin();
}
const adminActionLabels={disable_user:'停用用户',restore_user:'恢复用户',archive_directory:'目录移入管理回收站',restore_directory:'恢复目录',update_storage:'调整统一空间'};
function renderAdmin() {
  $$('[data-admin-view]').forEach(button=>button.className=button.dataset.adminView===adminView?'primary':'secondary');
  const users=adminData.users.filter(user=>adminView==='recycle'?user.disabled_at:!user.disabled_at);
  const directories=adminData.directories.filter(dir=>adminView==='recycle'?dir.deleted_at:!dir.deleted_at);
  const userRows=users.map(user=>`<div class="event"><span class="event-icon">${icon('users')}</span><div class="event-detail"><p><strong>${esc(user.name)}</strong> <span class="mini-badge">${user.id.slice(0,8)}</span></p><small>创建于 ${new Date(user.created*1000).toLocaleString('zh-CN')} · ${user.directory_count} 个可用目录 · 空间 ${size(user.storage.used_bytes)} / ${size(user.storage.limit_bytes)}${user.disabled_at?' · 已停用':''}</small></div><button class="secondary admin-action" data-kind="user" data-id="${user.id}" data-action="${user.disabled_at?'restore':'disable'}">${user.disabled_at?'恢复用户':'停用用户'}</button></div>`).join('');
  const directoryRows=directories.map(dir=>`<div class="event"><span class="event-icon">${icon('folder')}</span><div class="event-detail"><p><strong>${esc(dir.name)}</strong> <span class="mini-badge">${dir.id.slice(0,8)}</span></p><small>${esc(dir.owner||'大家共有')} · ${dir.entries??'未知'} 个直接子项目${dir.deleted_at?' · 已移入管理回收站':''}</small></div>${dir.id==='public'?'<span class="pill">系统保留</span>':`<button class="secondary admin-action" data-kind="directory" data-id="${dir.id}" data-action="${dir.deleted_at?'restore':'archive'}">${dir.deleted_at?'恢复目录':'移入回收站'}</button>`}</div>`).join('');
  let html='';
  if(adminView==='users')html=userRows;
  else if(adminView==='storage')html=`<form id="storage-settings-form" class="storage-settings"><h2>统一空间分配</h2><p>所有用户使用相同额度，公共目录也使用这一额度。现有用户和新用户同时生效。</p><label for="storage-limit">每个用户的空间上限（GiB）</label><input id="storage-limit" type="number" min="1" max="10240" step="1" required value="${adminData.storage_limit_bytes/1024**3}"><p class="muted">回收站和历史版本计入额度。调低额度不会删除文件；已超额的空间会暂停新增写入，读取和恢复仍可使用。</p><button class="primary" type="submit">保存统一额度</button><p class="error" id="storage-settings-error" role="alert"></p></form>`;
  else if(adminView==='directories')html=directoryRows;
  else if(adminView==='recycle')html=`<h2 class="admin-section-title">已停用用户</h2>${userRows||'<p class="muted admin-empty">暂无已停用用户</p>'}<h2 class="admin-section-title">已移除目录</h2>${directoryRows||'<p class="muted admin-empty">暂无已移除目录</p>'}`;
  else html=adminData.activity.map(item=>`<div class="event"><span class="event-icon">${icon('clock')}</span><div class="event-detail"><p>${adminActionLabels[item.action]||esc(item.action)}：${esc(item.label)}</p><small>${item.target_id.slice(0,8)} · ${new Date(item.created*1000).toLocaleString('zh-CN')}</small></div></div>`).join('');
  $('#admin-content').innerHTML=`<div class="event-list">${html||'<p class="muted admin-empty">暂无记录</p>'}</div>`;
  if($('#storage-settings-form'))$('#storage-settings-form').onsubmit=async event=>{
    event.preventDefault();const submit=$('button',event.target);submit.disabled=true;$('#storage-settings-error').textContent='';
    try{await api('/api/admin/storage',{method:'PUT',json:{limit_gib:Number($('#storage-limit').value)}});await showAdmin();toast('统一空间额度已更新');}
    catch(error){$('#storage-settings-error').textContent=error.message;}
    finally{submit.disabled=false;}
  };
  $$('.admin-action').forEach(button=>button.onclick=safe(async()=>{
    const kind=button.dataset.kind,id=button.dataset.id,action=button.dataset.action;
    const item=(kind==='user'?adminData.users:adminData.directories).find(entry=>entry.id===id);
    const description=kind==='user'?(action==='disable'?`停用「${item.name}」会使其已有登录失效，目录和文件保留。之后可在管理回收站恢复。`:`恢复「${item.name}」后，可以重新填写名字进入；旧登录凭证不会恢复。`):(action==='archive'?`将「${item.name}」移入管理回收站后，所有人都无法通过列表或旧链接访问。原本默认使用它的用户将改为公共目录。文件保留，可恢复。`:`恢复「${item.name}」及原有文件。用户需要自行重新关联或设置默认目录。`);
    const confirmed=await dialog({title:button.textContent,html:`<p>${esc(description)}</p>`,submit:'确认'+button.textContent,danger:action!=='restore',onSubmit:()=>api(`/api/admin/${kind==='user'?'users':'directories'}/${id}/${action}`,{method:'POST'})});
    if(confirmed){await showAdmin();toast('操作已完成');}
  }));
}
$$('[data-admin-view]').forEach(button=>button.onclick=()=>{adminView=button.dataset.adminView;renderAdmin();});
$('#admin-refresh').onclick=safe(showAdmin);
$('#admin-logout').onclick=safe(async()=>{await api('/api/admin/logout',{method:'POST'});adminData=null;$('#admin-shell').hidden=true;$('#welcome').hidden=false;history.replaceState(null,'','/');resetJoin();$('#join-name').focus();});


function dialog({title,html,submit='确定',onSubmit,wide=false,danger=false}) {
  const modal=$('#modal'); modal.classList.toggle('editor-modal',wide); $('#modal-title').textContent=title; $('#modal-body').innerHTML=html; $('#modal-submit').textContent=submit; $('#modal-submit').className=danger?'danger':'primary';$('#modal-error').textContent='';
  return new Promise(resolve=>{
    let busy=false;
    const finish=value=>{modal.close();resolve(value);};
    $('#modal-close').onclick=$('#modal-cancel').onclick=()=>{if(!busy)finish(false);};
    modal.oncancel=e=>{e.preventDefault();if(!busy)finish(false);};
    $('#modal-form').onsubmit=async e=>{e.preventDefault();if(busy)return;busy=true;$('#modal-submit').disabled=true;$('#modal-error').textContent='';try{const result=await onSubmit();finish(result??true);}catch(error){$('#modal-error').textContent=error.message;}finally{busy=false;$('#modal-submit').disabled=false;}};
    modal.showModal();const input=$('input:not([type=checkbox]),textarea,select',modal);if(input){input.focus();if(input.tagName==='INPUT')input.select();}
  });
}
async function ask(title,description,initial='',submit='确定',validate=null) {
  return dialog({title,html:`<p>${esc(description)}</p><label for="prompt-value">名称</label><input id="prompt-value" maxlength="100" required value="${esc(initial)}">`,submit,onSubmit:()=>{const value=$('#prompt-value').value.trim();if(!value)throw Error('名称不能为空');if(validate&&!validate(value))throw Error('名称不能包含斜杠，或使用 . 和 ..');return value;}});
}
const confirmAction = (title,description,submit='确认') => dialog({title,html:`<p>${esc(description)}</p>`,submit,danger:true,onSubmit:()=>true});
async function savePreferences(pins,default_id) { await api('/api/preferences',{method:'PUT',json:{pins,default_id}});await metadata(); }
async function managePins() {
  await metadata();
  const ok=await dialog({title:'管理常用目录',submit:'保存设置',html:`<p>关联目录就是添加常用入口。取消关联不会删除文件，默认目录会自动保留在常用列表中。</p><div class="pin-options">${state.dirs.map(d=>`<label class="check-row"><input type="checkbox" value="${d.id}" ${state.me.pins.includes(d.id)?'checked':''}><span>${esc(d.name)}<small>${esc(d.owner||'大家共有')} · ${d.id.slice(0,6)}</small></span></label>`).join('')}</div><label for="default-dir">打开网站时，默认进入</label><select id="default-dir">${state.dirs.map(d=>`<option value="${d.id}" ${state.me.user.default_id===d.id?'selected':''}>${esc(d.name)} · ${d.id.slice(0,6)}</option>`).join('')}</select>`,onSubmit:async()=>{await savePreferences($$('.pin-options input:checked').map(i=>i.value),$('#default-dir').value);}});
  if(ok){toast('常用目录已更新');await render();}
}
$('#associate').onclick=$('#manage-pins').onclick=safe(managePins);
$('#profile-button').onclick=safe(async()=>{
  const currentName=state.me.user.name;
  let switching=false;
  const pending=dialog({title:'名字与身份设置',html:`<p>修改名字不改变文件位置。已经在不同入口创建了身份？可以切换回以前的空间，现有文件不会被删除。</p><label for="profile-edit-name">我的名字</label><input id="profile-edit-name" maxlength="100" required value="${esc(currentName)}"><button id="switch-identity" type="button" class="secondary wide identity-switch">切换已有身份</button>`,submit:'保存名字',onSubmit:async()=>{
    if(switching)throw Error('正在切换身份，请稍候');
    $('#switch-identity').disabled=true;
    try {await api('/api/profile',{method:'PATCH',json:{name:$('#profile-edit-name').value.trim()}});await metadata();toast('名字已更新');}
    finally {if($('#switch-identity'))$('#switch-identity').disabled=false;}
  }});
  $('#switch-identity').onclick=async()=>{
    if(state.uploading){$('#modal-error').textContent='请等待文件上传完成后再切换身份';return;}
    switching=true;$('#switch-identity').disabled=true;$('#modal-submit').disabled=true;
    try {
      await api('/api/logout',{method:'POST'});
      $('#modal-cancel').click();
      state.renderId++;state.me=null;state.dirs=[];state.items=[];
      history.replaceState(null,'','/');
      $('#shell').hidden=true;$('#welcome').hidden=false;
      resetJoin(currentName);$('#join-name').focus();
    }catch(error){$('#modal-error').textContent=error.message;}
    finally {switching=false;$('#modal-submit').disabled=false;if($('#switch-identity'))$('#switch-identity').disabled=false;}
  };
  await pending;
});

async function render() {
  const requestId=++state.renderId;$('#context-bar').innerHTML='';$('#content').innerHTML='<div class="empty"><p>正在读取共享空间…</p></div>';$('#footer-meta').textContent='';
  try {
    if(state.view==='files')await renderFiles(requestId);
    else if(state.view==='all')await renderAll();
    else if(state.view==='trash')await renderTrash(requestId);
    else await renderActivity(requestId);
  } catch(error) {
    if(requestId!==state.renderId)return;
    $('#content').innerHTML=`<div class="empty"><span class="empty-icon">${icon('folder')}</span><h3>暂时无法打开</h3><p>${esc(error.message)}</p>${button('back-space','返回全部目录')}</div>`;
    $('#back-space').onclick=safe(()=>navigate('all'));
  }
}
function heading(title,description,eyebrow,actions) { $('#page-title').textContent=title;$('#page-description').textContent=description;$('#page-eyebrow').textContent=eyebrow;$('#page-actions').innerHTML=actions;$('#top-label').textContent=state.view==='files'?'文件空间':title; }
async function renderFiles(requestId) {
  const dir=state.dirs.find(d=>d.id===state.did);if(!dir)throw Error('这个目录不存在，请从全部目录中选择');
  heading(dir.name,`${dir.owner?`${dir.owner} 创建`:'大家共有'} · 你和其他人都可以上传、修改和分享`,'SHARED FILES',button('share-dir','分享目录','secondary','link')+button('new-file','新建临时文件','secondary','file')+button('new-folder','新建文件夹','secondary','plus')+button('upload-folder-button','上传文件夹','secondary','folder')+button('upload-button','上传文件','primary','upload'));
  $('#share-dir').onclick=safe(()=>share(state.path));$('#new-folder').onclick=safe(createFolder);$('#new-file').onclick=safe(createTemporaryFile);$('#upload-button').onclick=()=>$('#file-input').click();$('#upload-folder-button').onclick=()=>{if(!('webkitdirectory' in $('#folder-input'))){toast('此浏览器不支持文件夹选择，请使用新版 Chrome、Edge 或 Firefox');return;}$('#folder-input').click();};
  const parts=state.path?state.path.split('/'):[];
  $('#context-bar').innerHTML=`<div class="toolbar"><div class="breadcrumbs"><button data-path="">${icon('folder')} ${esc(dir.name)}</button>${parts.map((part,index)=>`<span>/</span><button data-path="${esc(parts.slice(0,index+1).join('/'))}">${esc(part)}</button>`).join('')}</div><div class="actions"><label class="search-box" aria-label="搜索当前文件夹"><span>${icon('search')}</span><input id="file-search" placeholder="搜索当前文件夹" type="search"></label><button id="refresh-files" class="text-button" title="刷新文件列表" aria-label="刷新文件列表">${icon('refresh')}</button></div></div>`;
  $$('.breadcrumbs button').forEach(b=>b.onclick=safe(()=>navigate('files',state.did,b.dataset.path)));
  $('#refresh-files').onclick=safe(()=>render());$('#file-search').oninput=e=>{state.search=e.target.value;fileRows();};
  const result=await api(`${endpoint('files')}?${new URLSearchParams({path:state.path})}`);if(requestId!==state.renderId)return;
  state.items=result.items;fileRows();$('#footer-meta').textContent=`${state.items.length} 个项目 · 单文件上限 ${size(state.me.max_upload_bytes)}`;
}
function fileRows() {
  const items=state.items.filter(i=>i.name.toLocaleLowerCase().includes(state.search.toLocaleLowerCase()));
  let banner='';if(state.file){const item=state.items.find(i=>i.path===state.file&&!i.is_dir);banner=item?`<div class="share-banner"><p>有人与你分享了 <strong>${esc(item.name)}</strong></p><a href="${endpoint('download')}?${new URLSearchParams({path:item.path})}">下载文件 ↓</a></div>`:'<div class="share-banner"><p>分享的文件已被移动或删除，下面显示当前目录。</p></div>';}
  $('#content').innerHTML=banner+(items.length?`<div class="table-wrap"><table class="file-table"><thead><tr><th>文件名称</th><th>大小</th><th>修改时间</th><th>操作</th></tr></thead><tbody>${items.map((item,index)=>`<tr data-index="${index}"><td><button class="file-name" data-action="open" title="${esc(item.name)}">${symbol(item)}<span class="filename">${esc(item.name)}</span></button></td><td>${item.is_dir?'—':size(item.size)}</td><td>${date(item.modified)}</td><td><div class="row-actions">${!item.is_dir?`<a href="${endpoint('download')}?${new URLSearchParams({path:item.path})}" title="下载 ${esc(item.name)}" aria-label="下载 ${esc(item.name)}">${icon('download')}</a>`:''}${!item.is_dir&&isText(item.name)?`<button data-action="edit" title="编辑 ${esc(item.name)}" aria-label="编辑 ${esc(item.name)}">${icon('file')}</button>`:''}${!item.is_dir?`<button data-action="copy" title="复制到我的目录：${esc(item.name)}" aria-label="复制到我的目录 ${esc(item.name)}">${icon('copy')}</button>`:''}<button data-action="share" title="分享 ${esc(item.name)}" aria-label="分享 ${esc(item.name)}">${icon('link')}</button><button data-action="rename" title="重命名 ${esc(item.name)}" aria-label="重命名 ${esc(item.name)}">${icon('edit')}</button><button class="delete" data-action="delete" title="删除 ${esc(item.name)}" aria-label="删除 ${esc(item.name)}">${icon('trash')}</button></div></td></tr>`).join('')}</tbody></table></div>`:`<div class="empty"><span class="empty-icon">${icon(state.search?'search':'folder')}</span><h3>${state.search?'没有找到匹配文件':'这里，等你放下第一份文件'}</h3><p>${state.search?'试试其他关键词。':'把文件拖到这里，或点击上传。放好之后，就能发给同一局域网里的伙伴。'}</p>${state.search?'':button('empty-upload','上传文件','primary','upload')}</div>`)+`<div class="hint-strip">${icon('shield')}删除的文件和覆盖前的版本，会保留在「回收与历史」中。</div>`;
  if($('#empty-upload'))$('#empty-upload').onclick=()=>$('#file-input').click();
  $$('[data-action]',$('#content')).forEach(b=>b.onclick=safe(async()=>{const item=items[Number(b.closest('tr').dataset.index)];switch(b.dataset.action){case'open':if(item.is_dir)await navigate('files',state.did,item.path);else if(isText(item.name))await editFile(item);else window.location.href=`${endpoint('download')}?${new URLSearchParams({path:item.path})}`;break;case'edit':await editFile(item);break;case'copy':b.disabled=true;try{await copyFile(item);}finally{b.disabled=false;}break;case'share':await share(item.is_dir?item.path:state.path,item.is_dir?'':item.path);break;case'rename':await renameFile(item);break;case'delete':await deleteFile(item);break;}}));
}
async function copyFile(item) {
  const sourceId=state.did;
  toast('正在复制到我的目录…');
  const result=await api(endpoint('copy',sourceId),{method:'POST',json:{path:item.path,version:item.version}});
  toast(`已复制到「${result.directory_name}」：${result.path}`);
  if(state.view==='files'&&state.did===result.directory_id&&!state.path)await render();
}
async function createTemporaryFile() {
  const did=state.did,folder=state.path;
  let initial='临时文件.txt',number=2;
  while(state.items.some(item=>item.name===initial))initial=`临时文件 ${number++}.txt`;
  const created=await dialog({title:'新建临时文件',wide:true,submit:'创建文件',html:`<p>保存到当前目录，所有成员可读写。内容可以留空，文件不会自动删除。</p><label for="new-file-name">文件名</label><input id="new-file-name" required maxlength="100" value="${esc(initial)}"><label for="editor-content">文本内容（可留空）</label><textarea id="editor-content" spellcheck="false" placeholder="临时记录、待办事项或一段代码…"></textarea>`,onSubmit:async()=>{
    const name=$('#new-file-name').value.trim(),content=$('#editor-content').value;
    if(!leafName(name))throw Error('文件名不能为空，不能包含斜杠，或使用 . 和 ..');
    if(!isText(name))throw Error('请使用文本文件扩展名，例如 .txt、.md 或 .json');
    if(new TextEncoder().encode(content).length>2*1024*1024)throw Error('文本内容不能超过 2 MiB');
    const path=folder?`${folder}/${name}`:name;
    try{await api(`${endpoint('upload',did)}?${new URLSearchParams({path,expected_version:''})}`,{method:'PUT',body:content,headers:{'Content-Type':'application/octet-stream'}});}
    catch(error){if(error.status===409)throw Error('已有同名文件或文件夹，请更换文件名；你的内容已保留');throw error;}
    return name;
  }});
  if(created){state.search='';await render();toast(`已创建「${created}」`);}
}
async function createFolder() {const name=await ask('新建文件夹','将在当前目录中创建，所有人都能访问。','','创建',leafName);if(name){await api(endpoint('folders'),{method:'POST',json:{path:joinPath(name)}});await render();toast('文件夹已创建');}}
async function renameFile(item) {const name=await ask('重命名',`为「${item.name}」设置新名称。旧的分享链接可能因此失效。`,item.name,'保存',leafName);if(name&&name!==item.name){await api(endpoint('rename'),{method:'POST',json:{path:item.path,version:item.version,destination:joinPath(name)}});await render();toast('名称已更新');}}
async function deleteFile(item) {if(await confirmAction('移入回收站',`「${item.name}」${item.is_dir?'及其中所有文件':''}将从共享目录中移除，大家都将无法在原位置访问。之后可从回收与历史中恢复。`,'移入回收站')){await api(endpoint('delete'),{method:'POST',json:{path:item.path,version:item.version}});await render();toast('已移入回收站');}}
async function editFile(item) {
  const did=state.did;const result=await api(`${endpoint('text',did)}?${new URLSearchParams({path:item.path})}`);
  const saved=await dialog({title:`编辑 · ${item.name}`,html:`<p>保存时保留旧版本。如果其他人已经修改此文件，会提示冲突并保留你当前的编辑内容。</p><label for="editor-content">文件内容</label><textarea id="editor-content" spellcheck="false">${esc(result.content)}</textarea>`,wide:true,submit:'保存文件',onSubmit:async()=>{await api(`${endpoint('upload',did)}?${new URLSearchParams({path:item.path,expected_version:result.version})}`,{method:'PUT',body:$('#editor-content').value,headers:{'Content-Type':'application/octet-stream'}});}});
  if(saved){await render();toast('已保存，旧版本可在回收与历史中找到');}
}
async function share(path,file='') {
  const base=state.me.share_base_url||location.origin;
  const url=new URL('/',base);
  url.searchParams.set('dir',state.did);
  if(path)url.searchParams.set('path',path);
  if(file)url.searchParams.set('file',file);
  await dialog({title:file?'分享文件':'分享目录',html:`<p>把链接发给同一局域网里的伙伴，对方填写名字后即可打开。也可以直接选中下方链接，手动复制。</p><label for="share-url">分享链接</label><input id="share-url" readonly value="${esc(url.href)}">`,submit:'复制链接',onSubmit:async()=>{
    let copied=false;
    try {
      if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(url.href);copied=true;}
      else {const input=$('#share-url');input.focus();input.select();copied=document.execCommand('copy');}
    }catch{}
    if(!copied)throw Error('浏览器未允许自动复制，请选中链接后手动复制。');
    toast('链接已复制');
  }});
}

async function renderAll() {
  heading('全部目录','每个人的空间都在这里。关联你常用的目录，随时回来。','EXPLORE THE SPACE',button('new-directory','新建共享目录','primary','plus'));
  $('#new-directory').onclick=safe(async()=>{const name=await ask('新建共享目录','适合项目资料、团队文档或任何想一起整理的文件。','','创建');if(name){const result=await api('/api/directories',{method:'POST',json:{name}});await metadata();await navigate('files',result.id);}});
  $('#content').innerHTML=`<div class="directory-grid">${state.dirs.map(d=>{
    const pinned=state.me.pins.includes(d.id);
    const isDefault=state.me.user.default_id===d.id;
    const pinLabel=isDefault?'默认目录，始终收藏':pinned?'取消收藏':'收藏目录';
    return `<article class="directory-card" data-dir="${d.id}"><div class="card-top"><span class="folder-icon">${icon('folder')}</span><button class="pin-toggle ${pinned?'selected':''}" title="${pinLabel}" aria-label="${pinLabel} ${esc(d.name)}" aria-pressed="${pinned}">${icon('star')}</button></div><h3>${esc(d.name)}${isDefault?'<span class="mini-badge">默认</span>':''}</h3><p>${esc(d.owner||'大家共有')} · ${d.id.slice(0,6)}</p><div class="card-bottom"><small>所有人可读写</small><a class="open-dir" href="/?dir=${encodeURIComponent(d.id)}" aria-label="打开目录 ${esc(d.name)}">打开目录 →</a></div></article>`;
  }).join('')}</div>`;
  $$('.directory-card').forEach(card=>{
    const did=card.dataset.dir;
    $('.open-dir',card).onclick=safe(async event=>{
      if(event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;
      event.preventDefault();
      await navigate('files',did);
    });
    $('.pin-toggle',card).onclick=safe(async event=>{
      event.stopPropagation();
      if(pinUpdatePending)return;
      if(state.me.user.default_id===did){toast('默认目录始终收藏；如需取消，请先在常用目录设置中更换默认项');return;}
      const wasPinned=state.me.pins.includes(did);
      const pins=wasPinned?state.me.pins.filter(p=>p!==did):[...state.me.pins,did];
      pinUpdatePending=true;
      const buttons=$$('.pin-toggle');buttons.forEach(button=>button.disabled=true);
      try {
        await savePreferences(pins,state.me.user.default_id);
        if(state.view==='all'){
          await renderAll();
          $$('.directory-card').find(item=>item.dataset.dir===did)?.querySelector('.pin-toggle').focus();
        }
        toast(wasPinned?'已取消收藏':'已收藏，可在左侧常用目录中打开');
      } finally {
        pinUpdatePending=false;
        buttons.forEach(button=>button.disabled=false);
      }
    });
  });
  $('#footer-meta').textContent=`${state.dirs.length} 个共享目录`;
}
async function renderTrash(requestId) {
  heading('回收与历史','删除的文件和覆盖前的版本都保留在这里，可恢复到原处或新路径。','A LITTLE ROOM TO UNDO',button('refresh-trash','刷新','secondary','refresh'));
  $('#refresh-trash').onclick=safe(()=>render());const items=await api('/api/trash');if(requestId!==state.renderId)return;
  $('#content').innerHTML=items.length?`<div class="event-list">${items.map((item,index)=>`<div class="event"><span class="event-icon">${icon(item.reason==='overwrite'?'clock':'trash')}</span><div class="event-detail"><p>${esc(item.path)} <span class="mini-badge">${item.reason==='overwrite'?'覆盖前版本':'已删除'}</span></p><small>${esc(item.directory)} · ${esc(item.actor)} · ${date(item.created)}</small></div><button class="secondary restore-button" data-index="${index}">恢复</button></div>`).join('')}</div>`:`<div class="empty"><span class="empty-icon">${icon('trash')}</span><h3>还没有需要恢复的文件</h3><p>文件被删除或覆盖后，旧内容会出现在这里。</p></div>`;
  $$('.restore-button').forEach(b=>b.onclick=safe(async()=>{const item=items[Number(b.dataset.index)];const restored=await dialog({title:'恢复文件',html:`<p>恢复到「${esc(item.directory)}」。可以修改路径，避免覆盖现有文件；上级文件夹需已存在。</p><label for="restore-path">恢复路径</label><input id="restore-path" required value="${esc(item.path)}">`,submit:'恢复',onSubmit:()=>api(`/api/trash/${item.id}/restore`,{method:'POST',json:{path:$('#restore-path').value}})});if(restored){await render();toast('文件已恢复');}}));$('#footer-meta').textContent=`${items.length} 条保留记录`;
}
const actionLabels={copy:'复制了',upload:'上传了',overwrite:'更新了',delete:'删除了',rename:'重命名了',mkdir:'新建了文件夹',create_directory:'创建了共享目录',restore:'恢复了'};
async function renderActivity(requestId) {
  heading('最近动态','看看共享空间里发生了什么。显示最近 200 条操作记录。','WHAT’S NEW',button('refresh-activity','刷新','secondary','refresh'));
  $('#refresh-activity').onclick=safe(()=>render());const items=await api('/api/activity');if(requestId!==state.renderId)return;
  $('#content').innerHTML=items.length?`<div class="event-list">${items.map(item=>`<div class="event"><span class="event-icon">${icon(item.action==='delete'?'trash':item.action==='upload'?'upload':'clock')}</span><div class="event-detail"><p><strong>${esc(item.actor)}</strong> ${actionLabels[item.action]||esc(item.action)} ${esc(item.path||item.directory)}</p><small>${esc(item.directory)} · 身份 ${item.actor_id.slice(0,6)}</small></div><time>${date(item.created)}</time></div>`).join('')}</div>`:`<div class="empty"><span class="empty-icon">${icon('clock')}</span><h3>共享，从第一份文件开始</h3><p>上传、修改、删除和恢复操作都会记录在这里。</p></div>`;
  $('#footer-meta').textContent=`最近 ${items.length} 条操作`;
}
function sendUpload(file,did,path,expected,onProgress,createDirectories=false) {return new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();currentUpload=xhr;xhr.open('PUT',`${endpoint('upload',did)}?${new URLSearchParams({path,expected_version:expected||'',create_directories:String(createDirectories)})}`);xhr.setRequestHeader('X-Lan-Share','1');xhr.setRequestHeader('Content-Type','application/octet-stream');xhr.upload.onprogress=e=>onProgress(e.lengthComputable?Math.round(e.loaded/e.total*100):0);xhr.onload=()=>{if(xhr.status>=200&&xhr.status<300)resolve();else{let message='上传失败';try{message=JSON.parse(xhr.responseText).detail||message;}catch{}reject(Error(message));}};xhr.onerror=()=>reject(Error('网络中断，文件未完成上传，请重试'));xhr.onabort=()=>reject(Error('已取消上传'));xhr.send(file);});}
function uploadFiles(fileList,folderUpload=false,entries=[]) {
  const files=[...fileList];
  const roots=[...entries].map(entry=>({entry}));
  return uploadSelection(roots.length?(check,onDirectory)=>collectDroppedItems(roots,check,onDirectory):async()=>({files:files.map(file=>({file,relativePath:folderUpload?file.webkitRelativePath:file.name})),directories:[]}),folderUpload);
}
async function uploadSelection(loadSelection,createDirectories=false) {
  if(state.uploading){toast('已有文件正在上传，请稍候');return;}
  if(state.view!=='files')return;
  const did=state.did,basePath=state.path;
  state.uploading=true;uploadCancelled=false;let completed=0,created=0,skipped=0;
  const checkCancelled=()=>{if(uploadCancelled)throw Error('已取消上传');};
  const targetPath=path=>basePath?`${basePath}/${path}`:path;
  try{
    $('#upload-progress').hidden=false;$('#upload-label').textContent='正在读取文件和目录…';$('#upload-progress progress').removeAttribute('value');
    const selection=await loadSelection(checkCancelled,path=>$('#upload-label').textContent=`正在读取目录 · ${path}`);
    checkCancelled();
    const plan=selection.files.map(({file,relativePath})=>{
      if(!relativePath||!relativePath.split('/').every(leafName))throw Error('文件路径无效');
      if(file.size>state.me.max_upload_bytes)throw Error(`${relativePath} 超过单文件上限 ${size(state.me.max_upload_bytes)}`);
      return {file,relativePath,target:targetPath(relativePath)};
    });
    if(!plan.length&&!selection.directories.length){toast('没有读取到可上传的项目；选择空文件夹时可尝试直接拖入');return;}
    const initial=await api(`${endpoint('files',did)}?${new URLSearchParams({path:basePath})}`);
    const total=plan.reduce((sum,item)=>sum+item.file.size,0);
    if(total>initial.storage.available_bytes)throw Error('空间已满，请联系管理员调整空间。');
    for(const directory of selection.directories){
      checkCancelled();$('#upload-label').textContent=`正在建立目录 · ${directory}`;
      const result=await api(endpoint('folders',did),{method:'POST',json:{path:targetPath(directory),parents:true,exist_ok:true}});
      if(result.created)created++;
    }
    for(const [index,item]of plan.entries()){
      checkCancelled();
      const {file,relativePath,target}=item;
      const slash=target.lastIndexOf('/'),parent=slash<0?'':target.slice(0,slash);
      let listing;
      try{listing=await api(`${endpoint('files',did)}?${new URLSearchParams({path:parent})}`);}
      catch(error){if(createDirectories&&error.status===404)listing={items:[]};else throw error;}
      checkCancelled();
      const existing=listing.items.find(i=>i.name===file.name);
      if(existing?.is_dir)throw Error(`${relativePath} 与已有文件夹同名`);
      if(existing&&!await confirmAction('覆盖同名文件？',`「${relativePath}」已存在。覆盖后保留旧版本，可以从回收与历史中恢复。`,'覆盖并保留旧版')){skipped++;continue;}
      checkCancelled();
      $('#upload-label').textContent=`正在上传 ${index+1}/${plan.length} · ${relativePath}`;$('#upload-progress progress').value=0;
      await sendUpload(file,did,target,existing?.version,percent=>$('#upload-progress progress').value=percent,createDirectories);completed++;
    }
    toast(`已上传 ${completed} 个文件${created?`，建立 ${created} 个文件夹`:''}${skipped?`，跳过 ${skipped} 个文件`:''}`);
  }catch(error){toast(`${error.message}；已完成 ${completed} 个文件、${created} 个文件夹，已完成项目保留`);}
  finally{
    state.uploading=false;currentUpload=null;$('#upload-progress').hidden=true;$('#file-input').value='';$('#folder-input').value='';
    if(state.view==='files')await render();
  }
}
$('#file-input').onchange=safe(e=>uploadFiles(e.target.files));
$('#folder-input').onchange=safe(e=>uploadFiles(e.target.files,true,e.target.webkitEntries||[]));
$('#cancel-upload').onclick=()=>{uploadCancelled=true;currentUpload?.abort();};

let dragDepth=0;const dropZone=$('main');
const hasFiles=e=>Array.from(e.dataTransfer.types).includes('Files');
dropZone.addEventListener('dragenter',e=>{if(state.view!=='files'||!hasFiles(e))return;e.preventDefault();dragDepth++;dropZone.classList.add('drop-active');});
dropZone.addEventListener('dragover',e=>{if(state.view==='files'&&hasFiles(e))e.preventDefault();});
dropZone.addEventListener('dragleave',()=>{dragDepth--;if(dragDepth<=0)dropZone.classList.remove('drop-active');});
dropZone.addEventListener('drop',safe(async e=>{
  e.preventDefault();dragDepth=0;dropZone.classList.remove('drop-active');
  if(state.view!=='files')return;
  const roots=captureDroppedItems(e.dataTransfer);
  await uploadSelection((check,onDirectory)=>collectDroppedItems(roots,check,onDirectory),true);
}));
window.addEventListener('beforeunload',e=>{if(state.uploading){e.preventDefault();e.returnValue='';}});
start().catch(error=>{if(error.status===401){$('#welcome').hidden=false;$('#join-name').focus();}else{$('#welcome').hidden=false;$('#join-error').textContent=`无法连接服务：${error.message}`;}});
