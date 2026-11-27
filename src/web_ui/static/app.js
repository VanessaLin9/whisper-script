'use strict';
const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const token = params.get('token') || sessionStorage.getItem('meetingDeskToken') || '';
if (params.has('token')) { sessionStorage.setItem('meetingDeskToken', token); history.replaceState(null, '', '/'); }
let meetings = [], selected = null, draft = null, preview = null, previewKind = null, environment = null, processId = null, busy = false;
const progressLabels = {validate_input:'檢查音訊',check_outputs:'檢查輸出',normalize:'準備音訊',transcribe:'Whisper 正在轉錄',validate_artifacts:'檢查逐字稿',cleanup:'整理產物',preclean:'整理逐字稿段落'};
const notice = message => { $('notice').textContent = message || ''; };
const text = (tag, value, className) => { const e = document.createElement(tag); e.textContent = value == null ? '' : String(value); if (className) e.className = className; return e; };
async function api(path, body) {
  const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', headers: {'X-Meeting-Desk-Token': token, ...(body === undefined ? {} : {'Content-Type':'application/json'})}, body: body === undefined ? undefined : JSON.stringify(body), cache:'no-store'});
  const data = await response.json();
  if (!response.ok || !data.ok) throw new Error(data.error || '操作失敗');
  return data;
}
const action = (name, fields={}) => api('/api/action', {action:name, ...fields});
async function run(fn) { try { notice(''); await fn(); } catch (e) { notice(e.message); } }
function confirmAction(message) { return window.confirm(message); }
function showTab(name) { for (const id of ['meet','jobs','settings']) $(id+'View').hidden = id !== name; if (name === 'jobs') run(loadJobs); if (name === 'settings') run(loadEnvironment); }
function choose(id) { selected = meetings.find(m => m.id === id) || null; preview = null; previewKind = null; $('editor').hidden = true; $('detail').hidden = !selected; if (!selected) return; $('meetingTitle').textContent = selected.title; $('meetingStatus').textContent = selected.date+' · '+selected.status+(selected.error ? ' · '+selected.error : ''); $('kind').value = selected.corrected ? 'corrected' : selected.prepared ? 'prepared' : 'raw'; run(loadPreview); }
async function loadMeetings() { const data = await action('list'); meetings = data.meetings || []; $('meetings').replaceChildren(); for (const m of meetings) { const b = text('button', m.title+'\n'+m.date+' · '+m.status); b.addEventListener('click', () => choose(m.id)); $('meetings').append(b); } if (data.warnings?.length) notice(data.warnings.join('；')); if (selected) choose(selected.id); }
async function loadPreview() { if (!selected) return; if (!$('editor').hidden && !confirmAction('放棄尚未儲存的訂正？')) { $('kind').value = previewKind; return; } $('editor').hidden = true; const kind = $('kind').value; preview = await action('preview', {folder:selected.id, kind}); previewKind = kind; $('preview').textContent = preview.text || ''; $('previewPath').textContent = preview.path || ''; $('edit').disabled = !preview.editable; }
async function loadEnvironment() { environment = await action('environment'); const s=environment.settings; $('outputRoot').value=s.output_root||''; $('whisperRoot').value=s.whisper_root||''; $('model').value=s.model||''; $('threads').value=s.threads||''; $('checks').replaceChildren(...(environment.checks||[]).map(c=>text('p',(c.ok?'✓ ':'✕ ')+c.name))); $('profile').replaceChildren(); for (const p of environment.profiles||[]) { const o=text('option',p.label); o.value=p.key; o.disabled=!p.available; $('profile').append(o); } }
async function loadJobs() { const data=await action('list_jobs',{include_archive:$('archive').checked}); $('jobs').replaceChildren(); if (!data.jobs.length) $('jobs').append(text('p','目前沒有工作。')); for (const j of data.jobs) { const box=document.createElement('div'); box.className='job'; for (const key of ['job_id','stage','status','location','meeting_dir','job_path']) if (j[key]) box.append(text('div', key+'：'+j[key])); $('jobs').append(box); } for (const j of data.invalid||[]) $('jobs').append(text('p','無效工作：'+j.job_path+' · '+j.error)); }
async function mutate(name, fields, question, after=true) { if (!confirmAction(question)) return; const data=await action(name,fields); notice('操作完成'+(data.path?'：'+data.path:'')+(data.notes_job_error?'；'+data.notes_job_error:'')); if (after) await loadMeetings(); return data; }
function editPreview() { if (!preview?.editable) return; const wrap=$('editFields'); wrap.replaceChildren(); const srt=$('kind').value==='srt'; $('editHelp').textContent=srt?'只能修改逐段字幕文字；序號、時間碼與順序固定。':'儲存後會建立人工訂正版本，並保留先前稿件。'; if (srt) { for (const cue of preview.cues||[]) { const row=document.createElement('div'); row.className='cue'; row.append(text('p','#'+cue.number+' · '+cue.time)); const field=document.createElement('textarea'); field.value=cue.text; field.dataset.id=cue.id; row.append(field); wrap.append(row); } } else { const field=document.createElement('textarea'); field.value=preview.text; wrap.append(field); } $('editor').hidden=false; }
async function saveEdit() { if (!selected || !preview) return; const fields={folder:selected.id,kind:$('kind').value,token:preview.token}; if (fields.kind==='srt') fields.cues=[...$('editFields').querySelectorAll('textarea')].map(e=>({id:Number(e.dataset.id),text:e.value})); else fields.text=$('editFields').querySelector('textarea').value; await mutate('save_edit',fields,'確認儲存人工訂正？'); $('editor').hidden=true; await loadPreview(); }
async function startProcess() { if (!selected || !confirmAction('確認開始轉錄與預清洗？')) return; const data=await api('/api/process',{action:'process',folder:selected.id}); processId=data.id; $('cancelProcess').hidden=false; $('process').disabled=true; $('progress').textContent='準備開始…'; pollProcess(); }
async function pollProcess() { if (!processId) return; try { const data=await api('/api/process/'+processId); const latest=data.events?.at(-1); $('progress').textContent=latest ? (progressLabels[latest.stage] || latest.stage || '處理中…') : '處理中…'; if (data.done) { processId=null; $('cancelProcess').hidden=true; $('process').disabled=false; if (!data.result.ok) throw new Error(data.result.error||'轉錄失敗'); $('progress').textContent='轉錄與預清洗完成。'; await loadMeetings(); $('kind').value='prepared'; await loadPreview(); } else setTimeout(pollProcess,800); } catch(e) { processId=null; $('cancelProcess').hidden=true; $('process').disabled=false; notice(e.message); await loadMeetings(); } }
function bind(id, fn) { $(id).addEventListener('click',()=>run(fn)); }
bind('tabMeet',()=>showTab('meet')); bind('tabJobs',()=>showTab('jobs')); bind('tabSettings',()=>showTab('settings'));
bind('refresh',loadMeetings); bind('refreshJobs',loadJobs); bind('loadPreview',loadPreview); bind('edit',editPreview); bind('saveEdit',saveEdit); bind('discardEdit',()=>{if(confirmAction('放棄尚未儲存的訂正？')) $('editor').hidden=true;});
bind('inspect',async()=>{draft=await action('inspect',{path:$('audioPath').value.trim()}); $('importDraft').hidden=false; $('resolvedPath').textContent='已解析路徑：'+draft.path+'（時間來源：'+draft.time_source+'）'; $('importTitle').value=draft.title; $('importTime').value=draft.meeting_time;});
$('audioPath').addEventListener('input',()=>{draft=null;$('importDraft').hidden=true;});
bind('importAudio',async()=>{if(!draft) throw new Error('請先檢查音檔。'); const data=await mutate('import',{path:draft.path,title:$('importTitle').value,meeting_time:$('importTime').value},'確認匯入這個音檔？'); if(data){draft=null;$('importDraft').hidden=true;choose(data.meeting.id);}});
bind('rename',async()=>{if(!selected)return;const title=prompt('新的會議名稱',selected.title);if(title===null||title===selected.title)return;await mutate('rename',{folder:selected.id,title,expected_title:selected.title},'確認更新會議名稱？');});
bind('process',startProcess); bind('cancelProcess',async()=>{if(processId&&confirmAction('確認取消轉錄？'))await api('/api/process/'+processId+'/cancel',{});});
bind('handoffClean',()=>selected&&mutate('handoff',{folder:selected.id,profile:$('profile').value,summary:false},'確認建立 LLM 清洗工作？'));
bind('handoffNotes',()=>selected&&mutate('handoff',{folder:selected.id,profile:$('profile').value,summary:true},'確認建立會議記錄工作？'));
bind('importJob',()=>selected&&mutate('import_job',{folder:selected.id},'確認匯入 outbox 清洗工作結果？'));
bind('importCleaned',()=>selected&&mutate('import_cleaned',{folder:selected.id,path:$('cleanedPath').value.trim()},'確認匯入此清洗稿 TXT？'));
bind('review',()=>selected&&mutate('review',{folder:selected.id},'請先對照預清洗稿與清洗稿，確認內容保真。確定已檢查並建立會議記錄工作？'));
bind('importNotes',()=>selected&&mutate('import_notes',{folder:selected.id},'確認匯入會議記錄草稿？此操作不會發布到 Notion。'));
bind('saveSettings',async()=>{await mutate('settings',{settings:{output_root:$('outputRoot').value,whisper_root:$('whisperRoot').value,model:$('model').value,threads:Number($('threads').value)}},'確認儲存設定並切換會議資料根目錄？'); await loadEnvironment();});
$('kind').addEventListener('change',()=>run(loadPreview));
if (!token) notice('缺少連線驗證資訊。請使用終端機顯示的完整網址。'); else run(async()=>{await loadEnvironment();await loadMeetings();});
