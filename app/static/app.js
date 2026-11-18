'use strict';
const $ = (id) => document.getElementById(id);
const jobsBox = $('jobs');
let lastJobsSignature = '';
let busyLink = false;
let busyUpload = false;
function showError(id, message) { const el=$(id); el.textContent=message; el.hidden=!message; }
async function api(path, options={}) {
  const headers = Object.assign({ 'X-Sara-Request':'1' }, options.headers||{});
  const response = await fetch(path, {...options, headers, credentials:'same-origin'});
  if (response.status === 401) { window.location.href='/login'; throw new Error('انتهت جلسة الدخول'); }
  let data=null;
  try { data=await response.json(); } catch (_) { /* web server error */ }
  if(!response.ok) throw new Error(data?.detail || `تعذر الاتصال بالخادم (${response.status})`);
  return data;
}
$('pasteBtn').addEventListener('click', async () => {
  try { const text=await navigator.clipboard.readText(); $('mediaUrl').value=text.trim(); $('mediaUrl').focus(); }
  catch (e) { $('mediaUrl').focus(); showError('linkError','المتصفح لم يسمح باللصق تلقائيًا. اضغط مطولًا داخل الحقل واختر لصق.'); }
});
$('linkForm').addEventListener('submit', async (event) => {
  event.preventDefault(); if(busyLink) return; busyLink=true;
  showError('linkError',''); $('downloadBtn').disabled=true;
  try {
    await api('/api/jobs', { method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url:$('mediaUrl').value.trim(),mode:document.querySelector('input[name="mode"]:checked').value}) });
    await loadJobs(); document.querySelector('#results-title').scrollIntoView({behavior:'smooth',block:'center'});
  } catch(e) {showError('linkError', e.message);}
  finally {busyLink=false; $('downloadBtn').disabled=false;}
});
function updateFileLabel(file) {
  $('filePrompt').textContent=file?.name || 'اختار ملف أو اسحبه هنا';
  $('fileSub').textContent=file ? `${(file.size/1024/1024).toFixed(1)} MB` : 'MP4 · MOV · MP3 · M4A · JPG · PNG وغيرها';
}
$('localFile').addEventListener('change', ()=>updateFileLabel($('localFile').files[0]));
const dz=$('dropzone');
dz.addEventListener('dragover',e=>{e.preventDefault(); dz.classList.add('dragging');});
dz.addEventListener('dragleave',()=>dz.classList.remove('dragging'));
dz.addEventListener('drop',e=>{
  e.preventDefault(); dz.classList.remove('dragging');
  if(e.dataTransfer.files.length){$('localFile').files=e.dataTransfer.files;updateFileLabel(e.dataTransfer.files[0]);}
});
$('uploadForm').addEventListener('submit',async (event)=>{
  event.preventDefault();if(busyUpload)return;
  const f=$('localFile').files?.[0];
  if(!f){showError('uploadError','اختر ملف أولًا');return;}
  busyUpload=true;$('uploadBtn').disabled=true;showError('uploadError','');
  const body=new FormData();body.append('file',f);body.append('mode',$('uploadMode').value);
  try { await api('/api/upload',{method:'POST',body});$('uploadForm').reset();updateFileLabel(null);await loadJobs();
    document.querySelector('#results-title').scrollIntoView({behavior:'smooth',block:'center'});
  }catch(e){showError('uploadError',e.message);}
  finally{busyUpload=false;$('uploadBtn').disabled=false;}
});
function el(tag, cls, txt) {const n=document.createElement(tag);if(cls)n.className=cls;if(txt!==undefined)n.textContent=txt;return n;}
function makeJob(job) {
  const card=el('article','job');
  const main=el('div','job-main');const info=el('div','job-info');
  const original=job.kind==='link' ? (()=>{try {return new URL(job.source).hostname;}catch(_){return job.source;}})() : job.source;
  info.append(el('div','job-name',job.filename||original));
  const statusNames={queued:'في الانتظار',running:'جاري المعالجة',done:'جاهز',failed:'فشل',cancelled:'ملغي'};
  info.append(el('div','job-meta',`${job.message} · ${job.mode==='audio'?'MP3':job.mode==='original'?'أصلي':job.mode==='best'?'تلقائي':job.mode}`));
  const stat=el('span',`status ${job.state}`,statusNames[job.state]||job.state);
  main.append(info,stat); card.append(main);
  if(job.state==='running'||job.state==='queued'){
    const track=el('div','progress-track');const bar=el('div','progress-bar');bar.style.width=`${job.progress}%`;track.append(bar);card.append(track);
  }
  if(job.state==='done'){
    const acts=el('div','job-actions');const link=el('a','', '⬇ تنزيل الملف');
    link.href=`/api/jobs/${job.id}/file`;acts.append(link);card.append(acts);
  } else if(job.state==='running'||job.state==='queued'){
    const acts=el('div','job-actions');const b=el('button','','إلغاء');b.type='button';
    b.addEventListener('click',async()=>{b.disabled=true;try{await api(`/api/jobs/${job.id}/cancel`,{method:'POST'});await loadJobs();}catch(_){b.disabled=false;}});
    acts.append(b);card.append(acts);
  } else if(job.error) card.append(el('div','job-error',job.error));
  return card;
}
async function loadJobs(){
  try{ const response=await api('/api/jobs'); const entries=response.jobs||[];
    const sig=JSON.stringify(entries.map(j=>[j.id,j.state,j.progress,j.message,j.filename]));
    if(sig===lastJobsSignature)return;lastJobsSignature=sig;
    jobsBox.replaceChildren();
    if(!entries.length){const empty=el('div','empty-state');empty.append(el('div','empty-icon','↓'),el('strong','','مفيش تحميلات لسه'),el('p','','ابدأ برابط أو ملف، والنتيجة هتظهر هنا.'));jobsBox.append(empty);}
    else entries.forEach(j=>jobsBox.append(makeJob(j)));
  } catch(error) {if(error.message!=='انتهت جلسة الدخول')jobsBox.replaceChildren(el('p','form-error',error.message));}
}
$('refreshBtn').addEventListener('click',loadJobs);
loadJobs();setInterval(()=>{if(!document.hidden)loadJobs();},2500);
// Offer paste from a Shortcuts/open URL parameter: https://host/?url=...
const shared=new URLSearchParams(location.search).get('url');
if(shared && /^https?:\/\//i.test(shared)){$('mediaUrl').value=shared;}
