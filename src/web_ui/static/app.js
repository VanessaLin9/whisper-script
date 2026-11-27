'use strict';
const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const token = params.get('token') || sessionStorage.getItem('meetingDeskToken') || '';
if (params.has('token')) { sessionStorage.setItem('meetingDeskToken', token); history.replaceState(null, '', '/'); }
let meetings = [], selected = null, draft = null, preview = null, previewKind = null;
let processId = null, busy = false, environment = null, editDirty = false;
const progressLabels = {validate_input:'檢查音訊',check_outputs:'檢查輸出',normalize:'準備音訊',transcribe:'Whisper 正在轉錄',validate_artifacts:'檢查逐字稿',cleanup:'整理產物',preclean:'整理逐字稿段落'};
const kindLabels = {raw:'原始稿',prepared:'預清洗稿',corrected:'人工訂正',cleaned:'清洗稿',srt:'時間軸'};
const actionButtons = ['refresh','openImport','welcomeImport','rename','showFolder','process','handoffClean','importJob','importCleaned','review','handoffNotes','importNotes','edit','jobsToggle','refreshJobs','settingsButton','inspect','importAudio','saveRename','saveSettings','saveEdit','submitCleaned','copyFolder'];
function el(tag, value, className) { const node=document.createElement(tag); node.textContent=value == null ? '' : String(value); if(className) node.className=className; return node; }
function notify(message, type='info') { $('noticeText').textContent=message; $('notice').dataset.type=type; $('notice').hidden=!message; }
async function api(path, body) {
  const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'X-Meeting-Desk-Token':token,...(body===undefined?{}:{'Content-Type':'application/json'})},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store'});
  const data=await response.json();
  if(!response.ok||!data.ok) throw new Error(data.error||'操作失敗。');
  return data;
}
const action=(name,fields={})=>api('/api/action',{action:name,...fields});
function syncBusy() {
  document.body.classList.toggle('is-busy',busy||!!processId);
  for(const id of actionButtons) $(id).disabled=busy||!!processId;
  for(const button of document.querySelectorAll('#meetings button,#kinds button')) button.disabled=busy||!!processId;
  for(const input of document.querySelectorAll('input,select,textarea')) input.disabled=busy||!!processId;
  $('cancelProcess').disabled=busy||!processId;
  if(!busy&&!processId) updateActions();
}
async function run(fn) { if(busy||processId) return; busy=true; syncBusy(); try { await fn(); } catch(e) { notify(e.message,'error'); } finally { busy=false; syncBusy(); } }
function openDialog(id) { $(id).showModal(); const first=$(id).querySelector('input:not([disabled]),button:not(.dialog-close)'); first?.focus(); }
function closeDialog(id) { $(id).close(); }
function ask(title,message,{danger=false,yes='確認'}={}) {
  return new Promise(resolve=>{
    const dialog=$('confirmDialog'); $('confirmTitle').textContent=title; $('confirmMessage').textContent=message;
    $('confirmYes').textContent=yes; $('confirmYes').className='button '+(danger?'danger':'primary');
    const finish=value=>{ dialog.close(); resolve(value); };
    $('confirmYes').onclick=()=>finish(true); $('confirmCancel').onclick=()=>finish(false); $('confirmClose').onclick=()=>finish(false);
    dialog.oncancel=event=>{event.preventDefault();finish(false);}; openDialog('confirmDialog');
  });
}
function renderMeetings() {
  const query=$('meetingSearch').value.trim().toLocaleLowerCase();
  $('meetingCount').textContent=String(meetings.length);
  const list=$('meetings'); list.replaceChildren();
  const shown=meetings.filter(m=>(m.title+' '+m.date).toLocaleLowerCase().includes(query));
  if(!shown.length) { list.append(el('p',meetings.length?'沒有符合搜尋的會議。':'還沒有會議。從右上角匯入第一段錄音。','empty-list')); return; }
  for(const meeting of shown) {
    const button=document.createElement('button'); button.type='button'; button.className='meeting-item'; button.dataset.meetingId=meeting.id;
    if(selected?.id===meeting.id) { button.classList.add('selected'); button.setAttribute('aria-current','true'); }
    const top=el('span',null,'meeting-item-top'); top.append(el('strong',meeting.title)); top.append(el('span',meeting.reviewed?'✓':meeting.cleaned?'●':meeting.prepared?'◐':'○','status-dot'));
    button.append(top,el('span',meeting.date+' · '+meeting.status,'meeting-meta'));
    button.addEventListener('click',()=>run(()=>choose(meeting.id)));
    list.append(button);
  }
}
function updateActions() {
  if(!selected) return;
  const m=selected, ready=!!$('profile').value;
  $('process').hidden=!!m.prepared;
  $('process').textContent=m.raw?'整理逐字稿':'轉錄並整理';
  $('cleanActions').hidden=!m.prepared;
  $('handoffClean').hidden=!m.prepared;
  $('handoffClean').disabled=!ready;
  $('importJob').hidden=!m.prepared||!!m.cleaned;
  $('importCleaned').hidden=!m.cleaned;
  $('review').hidden=!m.cleaned||!!m.reviewed;
  $('reviewedBadge').hidden=!m.reviewed;
  $('handoffNotes').hidden=!m.reviewed;
  $('handoffNotes').disabled=!ready;
  $('importNotes').hidden=!m.reviewed;
  $('actionHint').textContent=!m.prepared?'先完成轉錄與預清洗，再進行清洗交接。':!ready?'請先選擇可用的提示詞，才能建立 LLM 工作。':m.cleaned&&!m.reviewed?'請對照原始稿與清洗稿，再確認內容。':m.reviewed?'清洗稿已確認，可以準備或匯入會議記錄工作。':'清洗工作完成後，從 outbox 匯入結果。';
  for(const [id,done] of [['step1',true],['step2',m.prepared],['step3',m.cleaned],['step4',m.reviewed]]) $(id).classList.toggle('done',!!done);
  for(const button of $('kinds').querySelectorAll('button')) {
    const kind=button.dataset.kind; const available=kind==='srt'?m.raw:!!m[kind];
    button.hidden=kind==='corrected'&&!m.corrected;
    button.disabled=!available;
    button.classList.toggle('active',kind===previewKind);
    button.setAttribute('aria-selected',String(kind===previewKind));
    if(!available) button.title='尚未產生'+kindLabels[kind]; else button.removeAttribute('title');
  }
  $('edit').disabled=!preview?.editable;
  if(busy||processId) {
    for(const id of actionButtons) $(id).disabled=true;
    for(const button of $('kinds').querySelectorAll('button')) button.disabled=true;
  }
}
async function choose(id) {
  selected=meetings.find(m=>m.id===id)||null; preview=null; previewKind=null;
  $('welcome').hidden=!!selected; $('detail').hidden=!selected;
  renderMeetings();
  if(!selected) return;
  $('meetingTitle').textContent=selected.title;
  $('meetingStatus').textContent=selected.date+' · '+selected.status;
  $('meetingError').textContent=selected.error||''; $('meetingError').hidden=!selected.error;
  const first=selected.corrected?'corrected':selected.prepared?'prepared':selected.raw?'raw':null;
  updateActions();
  if(first) await loadPreview(first); else { $('preview').textContent='尚未產生稿件。請先轉錄並整理音檔。'; $('previewPath').textContent=''; }
}
async function loadMeetings(preferredId=selected?.id) {
  const data=await action('list'); meetings=data.meetings||[];
  if(data.warnings?.length) notify(data.warnings.join('；'),'info');
  await choose(preferredId);
}
async function loadPreview(kind) {
  if(!selected) return;
  previewKind=kind; preview=null; $('preview').textContent='正在載入稿件…'; $('previewPath').textContent=''; updateActions();
  try { preview=await action('preview',{folder:selected.id,kind}); $('preview').textContent=preview.text||'這份稿件目前沒有文字。'; $('previewPath').textContent=preview.path||''; }
  catch(e) { $('preview').textContent='無法載入'+kindLabels[kind]+'。'; throw e; }
  finally { updateActions(); }
}
async function loadEnvironment() {
  environment=await action('environment'); const s=environment.settings;
  $('outputRoot').value=s.output_root||''; $('whisperRoot').value=s.whisper_root||''; $('model').value=s.model||'base'; $('threads').value=s.threads||'';
  $('checks').replaceChildren(...(environment.checks||[]).map(c=>el('p',(c.ok?'✓ ':'✕ ')+c.name,c.ok?'check-ok':'check-error')));
  const previous=$('profile').value; $('profile').replaceChildren(); const empty=el('option','選擇提示詞'); empty.value=''; $('profile').append(empty);
  for(const profile of environment.profiles||[]) { const option=el('option',profile.label); option.value=profile.key; option.disabled=!profile.available; $('profile').append(option); }
  $('profile').value=[...$('profile').options].some(o=>o.value===previous&&!o.disabled)?previous:'';
  updateActions();
}
const jobStage={clean:'清洗',notes:'會議記錄'};
const jobStatus={queued:'等待處理',running:'處理中',done:'已完成',imported:'已匯入',stale:'內容已變更',failed:'失敗'};
const jobLocation={inbox:'交接匣',archive:'封存'};
async function loadJobs() {
  const data=await action('list_jobs',{include_archive:$('archive').checked});
  $('jobCount').textContent=String((data.jobs||[]).length);
  if($('jobsPanel').hidden) return;
  $('jobs').replaceChildren();
  if(!data.jobs?.length&&!data.invalid?.length) { $('jobs').append(el('p','目前沒有 LLM 工作。完成預清洗並選擇提示詞後，可在會議詳情建立清洗工作。','empty-list')); return; }
  for(const job of data.jobs||[]) {
    const card=el('article',null,'job'); card.append(el('strong',(jobStage[job.stage]||job.stage||'工作')+' · '+(jobStatus[job.status]||job.status||'待處理')));
    for(const [key,label] of [['job_id','工作 ID'],['location','位置'],['meeting_title','會議'],['job_path','工作檔']]) if(job[key]) card.append(el('p',label+'：'+(key==='location'?(jobLocation[job[key]]||job[key]):job[key])));
    $('jobs').append(card);
  }
  for(const job of data.invalid||[]) $('jobs').append(el('p','無效工作：'+job.job_path+' · '+job.error,'inline-error'));
}
async function mutate(name,fields,success,{title='確認操作',message='確定要繼續嗎？',danger=false,refresh=true}={}) {
  if(!await ask(title,message,{danger})) return null;
  const data=await action(name,fields);
  if(refresh) await loadMeetings(data.meeting?.id||selected?.id);
  if(['handoff','import_job','import_cleaned','import_notes','review'].includes(name)) await loadJobs().catch(()=>{});
  notify(data.notes_job_error?'清洗稿已確認，但會議記錄工作未建立：'+data.notes_job_error:success,data.notes_job_error?'info':'success');
  return data;
}
function updateWordCount() { const value=[...$('editFields').querySelectorAll('textarea')].map(e=>e.value).join(''); $('wordCount').textContent=value.replace(/\s/g,'').length+' 字'; }
function editPreview() {
  if(!preview?.editable) return;
  const wrap=$('editFields'); wrap.replaceChildren();
  const srt=previewKind==='srt'; $('editHelp').textContent=srt?'只能修改逐段字幕文字；序號、時間碼與順序固定。':'儲存後會建立人工訂正版本，原始稿與預清洗稿仍可對照。';
  if(srt) for(const cue of preview.cues||[]) { const row=el('div',null,'cue'); row.append(el('p','#'+cue.number+' · '+cue.time)); const field=document.createElement('textarea'); field.value=cue.text; field.dataset.id=cue.id; row.append(field); wrap.append(row); }
  else { const field=document.createElement('textarea'); field.value=preview.text||''; wrap.append(field); }
  editDirty=false; updateWordCount(); openDialog('editDialog');
}
async function saveEdit() {
  if(!selected||!preview) return;
  const fields={folder:selected.id,kind:previewKind,token:preview.token};
  if(previewKind==='srt') fields.cues=[...$('editFields').querySelectorAll('textarea')].map(e=>({id:Number(e.dataset.id),text:e.value}));
  else fields.text=$('editFields').querySelector('textarea').value;
  const data=await mutate('save_edit',fields,'人工訂正已儲存。',{title:'儲存人工訂正',message:'儲存後會保留舊版稿件。確認儲存嗎？'});
  if(data) { editDirty=false; closeDialog('editDialog'); await loadPreview(data.kind==='corrected'?'corrected':previewKind); }
}
async function startProcess() {
  if(!selected||!await ask('開始轉錄','確認開始轉錄與預清洗？')) return;
  const data=await api('/api/process',{action:'process',folder:selected.id}); processId=data.id;
  $('progressBar').hidden=false; $('progress').textContent='準備轉錄…'; syncBusy(); pollProcess();
}
async function pollProcess() {
  if(!processId) return;
  try {
    const data=await api('/api/process/'+processId);
    const latest=data.events?.at(-1); $('progress').textContent=latest?(progressLabels[latest.stage]||latest.message||'處理中…'):'處理中…';
    if(!data.done) { setTimeout(pollProcess,800); return; }
    processId=null; $('progressBar').hidden=true; syncBusy();
    if(!data.result.ok) throw new Error(data.result.error||'轉錄失敗。');
    await loadMeetings(); if(selected?.prepared) await loadPreview('prepared');
    notify('轉錄與預清洗完成。選擇提示詞，即可準備 LLM 交接。','success');
  } catch(e) { processId=null; $('progressBar').hidden=true; syncBusy(); notify(e.message,'error'); await loadMeetings().catch(()=>{}); }
}
function bind(id,fn) { $(id).addEventListener('click',()=>run(fn)); }
$('closeNotice').addEventListener('click',()=>$('notice').hidden=true);
$('meetingSearch').addEventListener('input',renderMeetings);
$('profile').addEventListener('change',updateActions);
$('archive').addEventListener('change',()=>run(loadJobs));
$('editFields').addEventListener('input',()=>{editDirty=true;updateWordCount();});
for(const button of document.querySelectorAll('#kinds button')) button.addEventListener('click',()=>run(()=>loadPreview(button.dataset.kind)));
for(const button of document.querySelectorAll('[data-close]')) button.addEventListener('click',()=>closeDialog(button.dataset.close));
for(const button of document.querySelectorAll('.dialog-close[value="cancel"]')) button.addEventListener('click',()=>{if(button.closest('dialog').id!=='editDialog')button.closest('dialog').close();});
for(const id of ['openImport','welcomeImport']) bind(id,()=>{openDialog('importDialog');});
bind('refresh',async()=>{await loadMeetings();await loadJobs().catch(()=>{});});
bind('jobsToggle',async()=>{const open=$('jobsPanel').hidden; $('jobsPanel').hidden=!open; $('jobsToggle').setAttribute('aria-expanded',String(open)); if(open) await loadJobs();});
bind('refreshJobs',loadJobs);
bind('settingsButton',async()=>{await loadEnvironment();openDialog('settingsDialog');});
bind('rename',()=>{if(!selected)return;$('renameTitle').value=selected.title;openDialog('renameDialog');});
bind('saveRename',async()=>{if(!selected)return; const title=$('renameTitle').value.trim(); if(title===selected.title){closeDialog('renameDialog');return;} const data=await mutate('rename',{folder:selected.id,title,expected_title:selected.title},'會議名稱已更新。',{title:'修改會議名稱',message:'確認儲存新的會議名稱？'});if(data)closeDialog('renameDialog');});
bind('showFolder',()=>{if(!selected)return;$('folderPath').textContent=selected.id;openDialog('pathDialog');});
bind('copyFolder',async()=>{await navigator.clipboard.writeText(selected.id);closeDialog('pathDialog');notify('會議資料夾路徑已複製。','success');});
bind('inspect',async()=>{draft=await action('inspect',{path:$('audioPath').value.trim()});$('importDraft').hidden=false;$('resolvedPath').textContent='已解析路徑：'+draft.path+'（時間來源：'+draft.time_source+'）';$('importTitle').value=draft.title;$('importTime').value=draft.meeting_time;notify('音檔已檢查，請確認名稱與錄音時間。','info');});
$('audioPath').addEventListener('input',()=>{draft=null;$('importDraft').hidden=true;});
bind('importAudio',async()=>{if(!draft)throw new Error('請先檢查音檔。');const data=await mutate('import',{path:draft.path,title:$('importTitle').value,meeting_time:$('importTime').value},'音檔已匯入，現在可以轉錄並整理。',{title:'匯入音檔',message:'確認匯入這段音檔？'});if(data){draft=null;$('importDraft').hidden=true;closeDialog('importDialog');}});
bind('process',startProcess);
$('cancelProcess').addEventListener('click',async()=>{if(!processId)return;if(await ask('取消轉錄','目前的轉錄會停止。確認取消？',{danger:true,yes:'取消轉錄'})) { await api('/api/process/'+processId+'/cancel',{});$('progress').textContent='正在取消轉錄…'; }});
bind('handoffClean',()=>selected&&mutate('handoff',{folder:selected.id,profile:$('profile').value,summary:false},'清洗工作已建立，請依交接路徑完成 outbox 結果。',{title:'建立清洗工作',message:'確認建立 LLM 清洗工作？'}));
bind('importJob',()=>selected&&mutate('import_job',{folder:selected.id},'清洗稿已匯入。請對照預清洗稿，確認內容保真。',{title:'匯入清洗稿',message:'確認匯入 outbox 清洗工作結果？'}));
bind('importCleaned',()=>openDialog('cleanedDialog'));
bind('submitCleaned',async()=>{if(!selected)return;const data=await mutate('import_cleaned',{folder:selected.id,path:$('cleanedPath').value.trim()},'清洗稿已匯入。請重新對照預清洗稿確認內容。',{title:'重新匯入清洗稿',message:'匯入後需要重新確認內容。確定繼續嗎？'});if(data)closeDialog('cleanedDialog');});
bind('review',()=>selected&&mutate('review',{folder:selected.id},'清洗稿已確認，會議記錄工作已自動建立。',{title:'確認清洗稿內容',message:'請先對照原始／預清洗稿：開頭與結尾、議題、實質問答、數字和技術細節仍完整，未新增摘要或捏造內容，不確定的詞彙有標示。長度檢查本身不能代替內容檢查。'}));
bind('handoffNotes',()=>selected&&mutate('handoff',{folder:selected.id,profile:$('profile').value,summary:true},'會議記錄交接工作已建立。',{title:'準備會議記錄交接',message:'確認建立會議記錄工作？'}));
bind('importNotes',()=>selected&&mutate('import_notes',{folder:selected.id},'會議記錄草稿已匯入，尚未發布到 Notion。',{title:'匯入會議記錄',message:'確認匯入會議記錄草稿？此操作不會發布到 Notion。'}));
bind('edit',editPreview);bind('saveEdit',saveEdit);
bind('saveSettings',async()=>{const data=await mutate('settings',{settings:{output_root:$('outputRoot').value,whisper_root:$('whisperRoot').value,model:$('model').value,threads:Number($('threads').value)}},'設定已儲存，會議清單已更新。',{title:'儲存設定',message:'確認儲存設定並切換會議資料根目錄？'});if(data){closeDialog('settingsDialog');await loadEnvironment();}});
$('editDialog').addEventListener('cancel',event=>{if(editDirty){event.preventDefault();ask('放棄修改','尚未儲存的訂正會遺失。確認放棄？',{danger:true,yes:'放棄修改'}).then(ok=>{if(ok){editDirty=false;closeDialog('editDialog');}});}});
$('editDialog').querySelectorAll('button[value="cancel"]').forEach(button=>button.addEventListener('click',event=>{if(!editDirty)return;event.preventDefault();ask('放棄修改','尚未儲存的訂正會遺失。確認放棄？',{danger:true,yes:'放棄修改'}).then(ok=>{if(ok){editDirty=false;closeDialog('editDialog');}});}));
$('editDialog').addEventListener('close',()=>{editDirty=false;});
document.addEventListener('keydown',event=>{const mod=event.metaKey||event.ctrlKey;if(mod&&event.key.toLowerCase()==='o'){event.preventDefault();if(!busy&&!processId)openDialog('importDialog');}else if(mod&&event.key===','){event.preventDefault();if(!busy&&!processId)run(async()=>{await loadEnvironment();openDialog('settingsDialog');});}else if(mod&&event.key.toLowerCase()==='s'&&$('editDialog').open){event.preventDefault();run(saveEdit);}});
if(!token)notify('缺少連線驗證資訊。請使用終端機顯示的完整網址。','error');
else run(async()=>{await loadEnvironment();await loadMeetings();await loadJobs().catch(()=>{});});
