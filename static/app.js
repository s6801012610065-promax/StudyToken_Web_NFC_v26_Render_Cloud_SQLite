function fmt(sec){sec=Math.max(0,Math.floor(sec));const h=Math.floor(sec/3600),m=Math.floor(sec%3600/60),s=sec%60;return h?`${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`:`${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`}
function fmtToday(sec){sec=Math.max(0,Math.floor(sec));const h=Math.floor(sec/3600),m=Math.floor(sec%3600/60);return h?`${h}h ${String(m).padStart(2,'0')}m`:`${m}m`}
async function getJSON(url,opts){const r=await fetch(url,opts);const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}
function numberNormalize(v){let s=String(v??'').trim().toUpperCase().replace(/\s+/g,'');s=s.replace(/[๐-๙]/g,c=>'๐๑๒๓๔๕๖๗๘๙'.indexOf(c));if(/^\d+$/.test(s))return String(Number(s));if(!/^[IVXLCDM]+$/.test(s))return '';const vals={I:1,V:5,X:10,L:50,C:100,D:500,M:1000};let t=0,p=0;for(let i=s.length-1;i>=0;i--){let n=vals[s[i]];t+=n<p?-n:n;p=Math.max(p,n)}return String(t)}
let nfcPairingActive=false;
function setupCourseForm(form,opts){
 const code=form.querySelector('[name="subject_code"]'),name=form.querySelector('[name="name"]'),chapter=form.querySelector('[name="chapter"]'),title=form.querySelector('[name="title"]'),duration=form.querySelector('[name="duration"]'),generate=form.querySelector('.generate-token'),uid=form.querySelector('[name="nfc_uid"]'),token=form.querySelector('[name="nfc_token"]'),tokenDisplay=form.querySelector('.nfc-token-display'),msg=form.querySelector('.form-msg'),submit=form.querySelector('button[type="submit"]'),scanHint=form.querySelector('.scan-hint'),scanUidBtn=form.querySelector('.scan-uid'),uidDisplay=form.querySelector('.nfc-uid-display');
 let linking=false,scanningUid=false;
 const UID_IDLE_TEXT='Scan NFC (optional) เพื่อเขียน URL ทันทีตอน Save';

 // Same principle as v9: let the user scan the physical NFC tag BEFORE
 // saving. If a UID is already captured here, Save Course links it and
 // writes the URL to the tag in the same request (see submit handler).
 async function scanUidNow(){
   if(scanningUid)return;
   scanningUid=true; nfcPairingActive=true;
   if(scanUidBtn)scanUidBtn.disabled=true;
   if(uidDisplay)uidDisplay.value='Scanning... กรุณาแตะ NFC Tag';
   if(msg)msg.textContent='';
   try{
     await getJSON('/api/nfc/pair/start',{method:'POST'});
   }catch(e){
     scanningUid=false; nfcPairingActive=false;
     if(scanUidBtn)scanUidBtn.disabled=false;
     if(uidDisplay)uidDisplay.value=UID_IDLE_TEXT;
     if(msg)msg.textContent=e.message;
     return;
   }
   const poll=async()=>{
     if(!scanningUid)return;
     try{
       const x=await getJSON('/api/nfc/status');
       updateReaderTop(x);
       if(x.pair_uid){
         scanningUid=false; nfcPairingActive=false;
         if(scanUidBtn)scanUidBtn.disabled=false;
         if(uid)uid.value=x.pair_uid;
         if(uidDisplay)uidDisplay.value='Scanned UID: '+x.pair_uid;
         return;
       }
     }catch(e){}
     setTimeout(poll,300);
   };
   poll();
 }
 scanUidBtn?.addEventListener('click',scanUidNow);

 async function generateToken(){
   if(generate) generate.disabled=true;
   if(tokenDisplay) tokenDisplay.value='Generating NFC Token...';
   try{
     // v21 was calling this Flask POST endpoint with GET, causing HTTP 405.
     const x=await getJSON('/api/nfc/token/new',{
       method:'POST',
       headers:{'Content-Type':'application/json'},
       body:'{}'
     });
     if(!x?.nfc_token) throw new Error('server returned no token');
     if(token) token.value=x.nfc_token;
     if(tokenDisplay) tokenDisplay.value=x.nfc_token;
     if(msg && !linking) msg.textContent='NFC Token generated. กด Save Course ก่อน แล้วจึงสแกน NFC เพื่อเชื่อม UID กับ Token';
   }catch(e){
     // Fallback from v15: generate a valid token locally if the server is unavailable.
     const bytes=new Uint8Array(4);
     if(window.crypto?.getRandomValues) window.crypto.getRandomValues(bytes);
     else for(let i=0;i<bytes.length;i++) bytes[i]=Math.floor(Math.random()*256);
     const local='STK-'+Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('').toUpperCase();
     if(token) token.value=local;
     if(tokenDisplay) tokenDisplay.value=local;
     if(msg && !linking) msg.textContent='NFC Token generated locally. กด Save Course ก่อน แล้วจึงสแกน NFC เพื่อเชื่อม UID กับ Token';
   }finally{
     if(generate) generate.disabled=false;
   }
 }

 async function lookup(){
   const c=code.value.trim();
   if(!c)return;
   try{
     const x=await getJSON('/api/subjects/'+encodeURIComponent(c));
     if(x.found){
       name.value=x.name; name.readOnly=true;
       chapter.value=x.next_chapter; chapter.readOnly=true;
       if(msg && !linking) msg.textContent=`พบรหัสวิชา ${x.code} → Course Name ถูกล็อก และ Chapter ถัดไปคือ ${x.next_chapter}`;
     }else{
       name.readOnly=false; chapter.readOnly=false;
       if(msg && !linking) msg.textContent='รหัสวิชาใหม่: กรุณากรอก Course Name และ Chapter';
     }
   }catch(_){
     name.readOnly=false; chapter.readOnly=false;
     if(msg && !linking) msg.textContent='รหัสวิชาใหม่: กรุณากรอก Course Name และ Chapter';
   }
 }

 async function linkNfcToToken(saved){
   linking=true; nfcPairingActive=true;
   if(submit)submit.disabled=true;
   if(generate)generate.disabled=true;
   if(scanHint)scanHint.textContent='Waiting scan...';
   if(msg)msg.textContent=`NFC Token: ${saved.nfc_token} • กรุณาสแกน NFC`;
   try{
     await getJSON('/api/nfc/pair/start',{method:'POST'});
   }catch(e){
     linking=false; nfcPairingActive=false;
     if(submit)submit.disabled=false;
     if(generate)generate.disabled=false;
     if(scanHint)scanHint.textContent='Scan NFC';
     if(msg)msg.textContent=e.message;
     return;
   }

   const poll=async()=>{
     if(!linking)return;
     try{
       const x=await getJSON('/api/nfc/status');
       updateReaderTop(x);
       if(x.pair_uid){
         const scannedUid=x.pair_uid;
         try{
           const result=await getJSON('/api/nfc/link',{
             method:'POST',
             headers:{'Content-Type':'application/json'},
             body:JSON.stringify({nfc_token:saved.nfc_token,nfc_uid:scannedUid})
           });
           linking=false; nfcPairingActive=false;
           if(submit)submit.disabled=false;
           if(generate)generate.disabled=false;
           if(scanHint)scanHint.textContent='Scan NFC';
           let status=`เชื่อมสำเร็จ! UID ${scannedUid} → Token ${result.nfc_token}`;
           if(result.nfc_write_status==='SUCCESS') status+=' • เขียน URL ลง NFC สำเร็จ';
           else if(result.nfc_write_status==='WAITING_FOR_TAG') status+=' • กรุณาแตะ Tag อีกครั้งเพื่อเขียน URL';
           else if(result.nfc_write_status==='ERROR') status+=` • เชื่อมแล้ว แต่เขียน URL ไม่สำเร็จ: ${result.nfc_write_error||'unknown error'}`;
           else status+=' • เชื่อม UID แล้ว';
           if(msg)msg.textContent=status;
           if(opts.afterLink)opts.afterLink(result);
           form.reset();
           name.readOnly=false; chapter.readOnly=false;
           if(token)token.value='';
           if(tokenDisplay)tokenDisplay.value='NFC Token will appear automatically';
           if(uid)uid.value='';
           await generateToken();
           return;
         }catch(e){
           linking=false; nfcPairingActive=false;
           if(submit)submit.disabled=false;
           if(generate)generate.disabled=false;
           if(scanHint)scanHint.textContent='Scan NFC';
           if(msg)msg.textContent=e.message;
           return;
         }
       }
     }catch(e){
       if(msg)msg.textContent='กำลังรอ NFC Tag...';
     }
     setTimeout(poll,300);
   };
   poll();
 }

 code.addEventListener('blur',lookup);
 code.addEventListener('change',lookup);
 chapter.addEventListener('blur',()=>{const n=numberNormalize(chapter.value);if(n)chapter.value=n});

 generate?.addEventListener('click',generateToken);

 form.addEventListener('submit',async e=>{
   e.preventDefault();
   chapter.value=numberNormalize(chapter.value);
   if(!chapter.value){msg.textContent='Chapter ต้องเป็นเลข เช่น 1, ๑, II, IV';return}
   if(!token?.value){msg.textContent='กำลังสร้าง NFC Token กรุณารอสักครู่';await generateToken();if(!token?.value)return}
   const data=Object.fromEntries(new FormData(form));
   const preScannedUid=(uid?.value||'').trim();
   if(!preScannedUid) delete data.nfc_uid;
   try{
     submit.disabled=true;
     const x=await getJSON('/api/courses',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
     if(preScannedUid){
       // UID was already known before Save, so the backend just linked and
       // wrote the URL to the tag in this same request (v9-style automatic write).
       submit.disabled=false;
       let status=`บันทึกสำเร็จ! Token ${x.nfc_token} • UID ${x.tag_uid||preScannedUid}`;
       if(x.nfc_write_status==='SUCCESS') status+=' • เขียน URL ลง NFC สำเร็จ';
       else if(x.nfc_write_status==='WAITING_FOR_TAG') status+=' • กรุณาแตะ Tag อีกครั้งเพื่อเขียน URL';
       else if(x.nfc_write_status==='ERROR') status+=` • เขียน URL ไม่สำเร็จ: ${x.nfc_write_error||'unknown error'}`;
       if(msg)msg.textContent=status;
       if(opts.afterLink)opts.afterLink(x);
       form.reset();
       name.readOnly=false; chapter.readOnly=false;
       if(token)token.value='';
       if(tokenDisplay)tokenDisplay.value='NFC Token will appear automatically';
       if(uid)uid.value='';
       if(uidDisplay)uidDisplay.value=UID_IDLE_TEXT;
       await generateToken();
     }else{
       // No pre-scan: fall back to the original flow (save first, then
       // scan/link/write right after).
       await linkNfcToToken(x);
     }
   }catch(e){
     submit.disabled=false;
     msg.textContent=e.message;
   }
 });

 // Generate the first Token automatically when the Add Course form opens.
 generateToken();
}

function initDashboard(){loadDashboard();const f=document.getElementById('addForm');if(f)setupCourseForm(f,{afterLink:loadDashboard});initVerify();startNfcPageWatcher({redirectOnPaired:true})}
async function loadDashboard(){try{const[courses,today]=await Promise.all([getJSON('/api/courses'),getJSON('/api/today')]);const t=document.getElementById('today');if(t)t.textContent=fmtToday(today.seconds);const box=document.getElementById('courses');if(!box)return;box.innerHTML='';if(!courses.length){box.innerHTML='<div class="muted">ยังไม่มี Course</div>';return}courses.forEach(c=>renderCourseCard(box,c))}catch(e){console.warn(e)}}
function renderCourseCard(box,c){
 const el=document.createElement('div');el.className='course';const done=c.state==='FINISH';
 const studiedMin=Math.floor((c.studied_seconds||0)/60);
 el.innerHTML=`<h3 class="c-name">${escapeHtml(c.name)}</h3>
<div class="small c-meta">รหัสวิชา ${escapeHtml(c.subject_code||'-')} · Chapter ${escapeHtml(c.chapter||'-')} · ${escapeHtml(c.title||'-')}</div>
<div class="small">${fmt(c.position)} / ${fmt(c.duration)} · ${c.progress}%</div>
<div class="small muted">เรียนไปแล้ว ${c.sessions_count||0} ครั้ง · รวม ${studiedMin} นาที</div>
<div class="actions">
<button class="btn" data-act="timeline">Timeline${c.sections_count?' ('+c.sections_count+')':''}</button>
<button class="btn" data-act="edit">Edit</button>
<button class="btn danger" data-act="delete">Delete</button>
<span class="badge ${done?'done':''}">${done?'Finish':escapeHtml(c.state||'NOT_STARTED')}</span>
<span class="small">Token: ${escapeHtml(c.nfc_token||'-')}</span>
</div>
<div class="edit-box" style="display:none"></div>
<div class="timeline-box" hidden></div>`;
 el.querySelector('[data-act="delete"]').onclick=async()=>{if(confirm(`Delete ${c.name} Chapter ${c.chapter}?`)){await getJSON('/api/courses/'+encodeURIComponent(c.id),{method:'DELETE'});loadDashboard()}};
 el.querySelector('[data-act="edit"]').onclick=()=>openEditBox(el,c);
 el.querySelector('[data-act="timeline"]').onclick=()=>toggleCourseTimeline(el,c);
 box.appendChild(el);
}
async function toggleCourseTimeline(el,c){
 const box=el.querySelector('.timeline-box');
 if(!box.hidden){box.hidden=true;box.innerHTML='';return}
 box.hidden=false;box.innerHTML='<div class="muted">Loading...</div>';
 try{
  const x=await getJSON('/api/courses/'+encodeURIComponent(c.id)+'/timeline');
  box.innerHTML='<div class="small muted tl-summary"></div><div class="timeline"></div>';
  box.querySelector('.tl-summary').textContent=x.count?`${x.count} section${x.count>1?'s':''} · ${fmtDur(x.total_seconds)} recorded`:'';
  renderTimeline(box.querySelector('.timeline'),x.sections);
 }catch(e){box.innerHTML=`<div class="muted">${escapeHtml(e.message)}</div>`}
}
function openEditBox(el,c){
 const box=el.querySelector('.edit-box');
 if(box.style.display!=='none'){box.style.display='none';box.innerHTML='';return}
 box.style.display='block';
 box.innerHTML=`<div class="grid two edit-grid">
<input class="e-name" value="${escapeHtml(c.name)}" placeholder="Course Name">
<input class="e-chapter" value="${escapeHtml(c.chapter||'')}" placeholder="Chapter">
<input class="e-title" value="${escapeHtml(c.title||'')}" placeholder="Title">
<input class="e-duration" type="number" min="1" value="${Math.round(c.duration/60)}" placeholder="Time (minutes)">
</div>
<div class="actions"><button class="btn primary" data-act="save">Save Changes</button><button class="btn" data-act="cancel">Cancel</button></div>
<div class="form-msg muted e-msg"></div>`;
 box.querySelector('[data-act="cancel"]').onclick=()=>{box.style.display='none';box.innerHTML=''};
 box.querySelector('[data-act="save"]').onclick=async()=>{
   const msg=box.querySelector('.e-msg');
   const payload={
     name:box.querySelector('.e-name').value.trim(),
     chapter:numberNormalize(box.querySelector('.e-chapter').value),
     title:box.querySelector('.e-title').value.trim(),
     duration:box.querySelector('.e-duration').value.trim()
   };
   try{
     await getJSON('/api/courses/'+encodeURIComponent(c.id),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
     await loadDashboard();
   }catch(e){ if(msg) msg.textContent=e.message; }
 };
}
function initVerify(){
 const btn=document.getElementById('verifyBtn'),input=document.getElementById('verifyInput'),scanBtn=document.getElementById('verifyScanBtn'),result=document.getElementById('verifyResult');
 if(!btn||!result)return;
 let scanning=false;
 function renderResult(found,course){
   if(!found){
     result.innerHTML=`<div class="verify-card notfound"><div class="verify-title">Not found course</div></div>`;
     return;
   }
   const done=course.state==='FINISH';
   const studiedMin=Math.floor((course.studied_seconds||0)/60);
   result.innerHTML=`<div class="verify-card found">
<div class="verify-title">Course Found</div>
<div class="verify-name">${escapeHtml(course.name)}</div>
<div class="verify-line">Course ID: ${escapeHtml(course.subject_code||'-')} · Chapter ${escapeHtml(course.chapter||'-')} · ${escapeHtml(course.title||'-')}</div>
<div class="verify-line">Status: ${done?'FINISH':escapeHtml(course.state||'NOT_STARTED')}</div>
<div class="verify-line">เรียน ${course.sessions_count??0} ครั้ง · รวม ${studiedMin} นาที</div>
<div class="verify-line muted">NFC Token: ${escapeHtml(course.nfc_token||'-')}</div>
<div class="verify-line muted">UID: ${escapeHtml(course.tag_uid||'-')}</div>
</div>`;
 }
 async function runVerify(q){
   q=(q||'').trim();
   if(!q){result.innerHTML='';return}
   result.innerHTML='<div class="muted">กำลังตรวจสอบ...</div>';
   try{
     const x=await getJSON('/api/nfc/verify/'+encodeURIComponent(q));
     renderResult(true,x.course);
   }catch(e){
     renderResult(false,null);
   }
 }
 btn.onclick=()=>runVerify(input.value);
 input?.addEventListener('keydown',e=>{if(e.key==='Enter')runVerify(input.value)});
 scanBtn?.addEventListener('click',async()=>{
   if(scanning)return;
   scanning=true; nfcPairingActive=true; scanBtn.disabled=true;
   result.innerHTML='<div class="muted">กรุณาแตะ NFC Tag เพื่อตรวจสอบ...</div>';
   try{ await getJSON('/api/nfc/pair/start',{method:'POST'}); }
   catch(e){ scanning=false; nfcPairingActive=false; scanBtn.disabled=false; result.innerHTML=`<div class="muted">${escapeHtml(e.message)}</div>`; return; }
   const poll=async()=>{
     if(!scanning)return;
     try{
       const x=await getJSON('/api/nfc/status');
       if(x.pair_uid){
         scanning=false; scanBtn.disabled=false;
         const scannedUid=x.pair_uid;
         if(input)input.value=scannedUid;
         await runVerify(scannedUid);
         // Keep the pairing guard up for a few seconds after the tap: if we
         // clear it (or cancel pairing) right away, the global NFC watcher on
         // this page can mistake the very same tap for a fresh scan and jump
         // straight to the study timer. Waiting lets that watcher's next poll
         // see nfcPairingActive still true and ignore it.
         setTimeout(async()=>{
           nfcPairingActive=false;
           try{ await getJSON('/api/nfc/pair/cancel',{method:'POST'}); }catch(_){}
         },5000);
         return;
       }
     }catch(e){}
     setTimeout(poll,300);
   };
   poll();
 });
}
function initHistory(){loadHistory();startNfcPageWatcher({redirectOnPaired:true})}
async function loadHistory(){const rows=await getJSON('/api/history');const body=document.getElementById('history');if(!body)return;body.innerHTML='';rows.forEach(s=>{const tr=document.createElement('tr');const mins=Math.max(0,Number(s.end_position||0)-Number(s.start_position||0))/60;tr.innerHTML=`<td>${escapeHtml(s.started||'-')}</td><td>${escapeHtml(s.name||'-')}</td><td>${escapeHtml(s.chapter||'-')}</td><td>${escapeHtml(s.title||'-')}</td><td>${Math.floor(mins)} min</td><td>${fmt(s.start_position||0)} → ${fmt(s.end_position||0)}</td>`;body.appendChild(tr)})}
function updateReaderTop(x){const st=document.getElementById('readerStatus');if(st)st.textContent='● '+(x?.status||'UNKNOWN');const port=document.getElementById('readerPort');if(port)port.textContent=x?.port||'-';const uid=document.getElementById('lastUid');if(uid)uid.textContent=x?.last_uid||'-';const seen=document.getElementById('lastSeen');if(seen)seen.textContent=x?.last_seen||'-';const err=document.getElementById('readerError');if(err){err.textContent='';err.style.display='none'}}
function startNfcPageWatcher({redirectOnPaired=true}={}){let initialized=false,lastEvent=-1;const poll=async()=>{try{const x=await getJSON('/api/nfc/status');updateReaderTop(x);const eventId=Number(x.event_id??-1),pairEventId=Number(x.pair_event_id??-2);if(!initialized){initialized=true;lastEvent=eventId}else if(eventId>lastEvent){lastEvent=eventId;if(nfcPairingActive||x.pairing||eventId===pairEventId){}else if(x.last_uid){try{const r=await getJSON('/api/nfc/resolve/'+encodeURIComponent(x.last_uid));if(redirectOnPaired){location.href='/nfc/'+encodeURIComponent(r.nfc_token)+'?source=desktop';return}}catch(e){alert('NFC นี้ยังไม่ได้ผูกกับ Token')}}}}catch(e){}setTimeout(poll,500)};poll()}
function initDesktop(){const f=document.getElementById('desktopAddForm');if(f)setupCourseForm(f,{});initVerify();startNfcPageWatcher({redirectOnPaired:true})}
// ===== Chapter Section Timeline =====
function fmtDur(sec){sec=Math.max(0,Math.floor(sec));const h=Math.floor(sec/3600),m=Math.floor(sec%3600/60),s=sec%60;return h?`${h}h ${m}m`:(m?`${m}m ${String(s).padStart(2,'0')}s`:`${s}s`)}
function renderTimeline(box,sections,opts={}){
 if(!box)return;
 const live=opts.live||null;
 if(!sections.length&&!live){box.innerHTML='<div class="tl-empty muted">No progress saved yet. Press <b>Save</b> while studying to add the first section.</div>';return}
 const items=sections.map((x,i)=>{
   const cur=!live&&i===sections.length-1;
   const clock=String(x.started_at||'').slice(11,16);
   return `<li class="tl-item${cur?' current':''}${x.is_auto?' auto':''}" data-seq="${x.seq}"><div class="tl-time">${fmt(x.start_position)}</div><div class="tl-dot"></div><div class="tl-text">${escapeHtml(x.description)}</div><div class="tl-meta">${escapeHtml(x.study_date||'')}${clock?' '+escapeHtml(clock):''} · ${fmtDur(x.duration)}</div></li>`}).join('');
 const liveHtml=live?`<li class="tl-item live current"><div class="tl-time">${fmt(live.start)}</div><div class="tl-dot"></div><div class="tl-text">Studying now…</div><div class="tl-meta tl-live-elapsed">+${fmt(live.elapsed)}</div></li>`:'';
 box.innerHTML=`<div class="tl-scroll"><ol class="tl-track">${items}${liveHtml}</ol></div>`;
 const sc=box.querySelector('.tl-scroll');if(sc)sc.scrollLeft=sc.scrollWidth;   // newest section in view
}

// ===== Study page =====
let studyState=null;
const $id=id=>document.getElementById(id);
function newClientId(){return String(window.crypto?.randomUUID?(window.crypto.randomUUID()):('c'+Date.now().toString(36)+Math.random().toString(36).slice(2,10))).slice(0,64)}
// Elapsed position comes from the wall clock (not from counting timer ticks), so throttled tabs / locked phones stay accurate.
function studyNow(){const s=studyState;if(!s.running)return s.pos;return Math.min(s.duration,s.base+Math.floor((Date.now()-s.t0)/1000))}
function startRun(){const s=studyState;s.base=s.pos;s.t0=Date.now();s.running=true;s.paused=false;setStudyStatus();updateStudyButtons()}
function stopRun(){const s=studyState;if(s.running)s.pos=studyNow();s.running=false}
function setStudyStatus(){const el=$id('studyStatus'),s=studyState;if(!el)return;if(s.paused){el.textContent='● PAUSED · progress saved';el.classList.add('paused')}else{el.textContent='● SESSION ACTIVE';el.classList.remove('paused')}}
function updateStudyButtons(){
 const s=studyState,busy=s.finishing||s.saving||s.resuming||!s.sid;
 const save=$id('save'),resume=$id('resume'),stop=$id('stop'),skip=$id('skip');
 if(save){save.hidden=s.paused;save.disabled=busy||s.dialogOpen}
 if(resume){resume.hidden=!s.paused;resume.disabled=s.finishing||s.resuming}
 if(stop)stop.disabled=busy||s.dialogOpen;
 if(skip)skip.disabled=busy||s.dialogOpen||s.paused;
}
function studyMsg(t){const el=$id('studyMsg');if(el)el.textContent=t||''}
function renderStudyTimeline(){const s=studyState;renderTimeline($id('studyTimeline'),s.sections,{live:(s.sid&&!s.paused)?{start:s.segStart,elapsed:s.pos-s.segStart}:null})}
async function loadStudyTimeline(){try{const x=await getJSON('/api/courses/'+encodeURIComponent(studyState.courseId)+'/timeline');studyState.sections=x.sections}catch(e){console.warn(e)}renderStudyTimeline()}

function initStudy(){
 const box=document.querySelector('.study'),pos=Number(box.dataset.position);
 studyState={courseId:box.dataset.courseId,duration:Number(box.dataset.duration),pos,base:pos,t0:Date.now(),segStart:pos,segWall:new Date(),sid:null,running:false,paused:false,dialogOpen:false,saving:false,resuming:false,finishing:false,clientId:null,sections:[],timer:null,source:box.dataset.source||'mobile'};
 renderStudy();updateStudyButtons();loadStudyTimeline();startSession();
 $id('skip').onclick=async()=>{const s=studyState;if(!s.running||s.finishing)return;s.pos=Math.min(s.duration,studyNow()+300);s.base=s.pos;s.t0=Date.now();renderStudy();await autosave();if(s.pos>=s.duration)await finish(true)};
 $id('save').onclick=openSaveDialog;
 $id('resume').onclick=resumeStudy;
 $id('stop').onclick=()=>finish(false);
 $id('saveModalConfirm').onclick=confirmSave;
 $id('saveModalCancel').onclick=cancelSave;
 $id('saveModalInput').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();confirmSave()}});
 document.addEventListener('keydown',e=>{if(e.key==='Escape'&&studyState.dialogOpen&&!studyState.saving)cancelSave()});
 window.addEventListener('pagehide',()=>{const s=studyState;if(s?.sid&&!s.finishing){if(s.running)s.pos=studyNow();navigator.sendBeacon('/api/sessions/'+s.sid+'/close',new Blob([JSON.stringify({position:s.pos})],{type:'application/json'}))}});
 window.addEventListener('pageshow',e=>{if(e.persisted)location.reload()});   // restored from back/forward cache: its session was already closed
}
async function startSession(){
 const s=studyState;
 try{
  const x=await getJSON('/api/sessions/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({course_id:s.courseId})});
  s.sid=x.session_id;s.segStart=s.pos;s.segWall=new Date();
  if(s.pos>=s.duration){await finish(true);return}
  startRun();studyLoop();renderStudyTimeline()
 }catch(e){s.running=false;alert(e.message)}
}
function studyLoop(){
 const s=studyState;clearInterval(s.timer);
 s.timer=setInterval(async()=>{if(!s.running||s.finishing)return;const p=studyNow();if(p===s.pos)return;s.pos=p;renderStudy();await autosave();if(s.pos>=s.duration&&s.running)await finish(true)},500);
}
function renderStudy(){
 const s=studyState,p=Math.min(100,Math.floor(s.pos*100/s.duration));
 $id('timer').textContent=fmt(s.pos)+' / '+fmt(s.duration);$id('barFill').style.width=p+'%';$id('percent').textContent=p+'%';
 const el=document.querySelector('.tl-live-elapsed');if(el)el.textContent='+'+fmt(s.pos-s.segStart);
}
async function autosave(){const s=studyState;if(!s.sid)return;try{await getJSON('/api/sessions/'+s.sid+'/autosave',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({position:s.pos})})}catch(e){console.warn(e)}}

// Save: pause the timer, ask what was studied, store the segment as a timeline section.
async function openSaveDialog(){
 const s=studyState;if(!s.sid||s.finishing||s.saving||s.dialogOpen||s.paused)return;
 const p=studyNow();
 if(p<=s.segStart){studyMsg('No new study time since the last saved section.');return}
 studyMsg('');stopRun();s.dialogOpen=true;s.clientId=newClientId();
 renderStudy();updateStudyButtons();
 const clock=s.segWall.toTimeString().slice(0,5);
 $id('saveModalInfo').textContent=`Section ${fmt(s.segStart)} → ${fmt(s.pos)} · ${fmtDur(s.pos-s.segStart)} · started ${clock}`;
 $id('saveModalInput').value='';$id('saveModalMsg').textContent='';$id('saveModalConfirm').disabled=false;
 $id('saveModal').hidden=false;$id('saveModalInput').focus();
 autosave();
}
function closeSaveDialog(){$id('saveModal').hidden=true;studyState.dialogOpen=false}
function cancelSave(){
 const s=studyState;if(!s.dialogOpen||s.saving)return;
 closeSaveDialog();startRun();updateStudyButtons();   // nothing was recorded; time spent in the dialog is not counted
}
async function confirmSave(){
 const s=studyState;if(!s.dialogOpen||s.saving)return;
 const text=$id('saveModalInput').value.trim(),msg=$id('saveModalMsg');
 if(!text){msg.textContent='Please describe what you have studied.';return}
 s.saving=true;$id('saveModalConfirm').disabled=true;updateStudyButtons();msg.textContent='Saving...';
 try{
  const x=await getJSON('/api/sessions/'+s.sid+'/sections',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({position:s.pos,description:text,client_id:s.clientId})});
  s.sections=x.sections;s.pos=x.position;s.segStart=x.position;s.paused=true;
  closeSaveDialog();setStudyStatus();renderStudy();studyMsg('Progress saved. Press Resume Study to start the next section.');
 }catch(e){
  msg.textContent=e.message;$id('saveModalConfirm').disabled=false;s.saving=false;updateStudyButtons();return
 }
 s.saving=false;updateStudyButtons();renderStudyTimeline();
 if(s.pos>=s.duration)await finish(true);   // target reached: close the session, no extra prompt
}
async function resumeStudy(){
 const s=studyState;if(!s.paused||s.resuming||s.finishing)return;
 s.resuming=true;updateStudyButtons();
 try{
  await getJSON('/api/sessions/'+s.sid+'/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  s.segWall=new Date();studyMsg('');startRun();studyLoop();
 }catch(e){alert(e.message)}
 s.resuming=false;updateStudyButtons();renderStudyTimeline();
}
// Finish Study: never prompts. Remaining time is stored by the server; an already-saved section is not duplicated.
async function finish(done){
 const s=studyState;if(!s?.sid||s.finishing)return;
 s.finishing=true;if(s.running)s.pos=studyNow();const wasRunning=s.running;s.running=false;updateStudyButtons();
 try{
  await getJSON('/api/sessions/'+s.sid+'/finish',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({position:s.pos})});
  location.href='/'
 }catch(e){s.finishing=false;if(wasRunning)startRun();updateStudyButtons();alert(e.message)}
}
function escapeHtml(s){return String(s).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]))}
