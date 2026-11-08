/* Binary drafts stay on this device until Send. Message identities remain in messaging.js. */
const dashboardMedia = (() => {
  document.getElementById('message-form').insertAdjacentHTML('afterbegin','<div class="message-media"><label class="button secondary">Attach<input id="message-file" type="file" accept="image/jpeg,image/png,image/gif,image/webp,audio/*,video/*,.pdf,.txt,.docx,.xlsx" disabled></label><button id="message-record" class="button secondary" type="button" disabled>Record voice</button><button id="message-file-remove" class="button secondary" type="button" hidden>Remove</button><span id="message-file-name" role="status"></span></div>');
  document.getElementById('message-text').required=false;
  const picker=document.getElementById('message-file'), label=document.getElementById('message-file-name');
  const record=document.getElementById('message-record'), remove=document.getElementById('message-file-remove');
  const select=document.getElementById('message-agent'), notice=document.getElementById('message-notice');
  let enabled=false, locked=false, recorder=null, stream=null, timer=null;
  const database=new Promise((resolve,reject)=>{
    const request=indexedDB.open('tabularium.attachments',1);
    request.onupgradeneeded=()=>request.result.createObjectStore('drafts');
    request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
  });
  database.catch(()=>{});
  async function stored(agent, value, writing=false){
    const db=await database;
    return new Promise((resolve,reject)=>{
      const tx=db.transaction('drafts',writing?'readwrite':'readonly'), store=tx.objectStore('drafts');
      const request=writing?(value?store.put(value,agent):store.delete(agent)):store.get(agent);
      tx.oncomplete=()=>resolve(request.result);tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error||Error('Attachment storage interrupted'));
    });
  }
  async function refresh(){
    const agent=select.value;
    try{const file=await stored(agent);if(agent!==select.value)return;label.textContent=file?file.name+' · '+(file.size/1048576).toFixed(1)+' MB':'';remove.hidden=!file;}
    catch{label.textContent='Attachment storage unavailable';}
  }
  function controls(available, pending){
    enabled=available;locked=pending;
    picker.disabled=!enabled||locked||!!recorder;record.disabled=!enabled||locked;remove.disabled=locked||!!recorder;
  }
  async function choose(file,agent=select.value){
    if(!file)return;
    if(!file.size||file.size>50*1024*1024)throw Error('Choose a nonempty file no larger than 50 MB.');
    await stored(agent,file,true);await refresh();
  }
  picker.onchange=async()=>{try{await choose(picker.files[0]);}catch(e){notice.textContent=e.message;}finally{picker.value='';}};
  remove.onclick=async()=>{try{await stored(select.value,null,true);await refresh();}catch(e){notice.textContent=e.message;}};
  record.onclick=async()=>{
    if(recorder){recorder.stop();return;}
    const agent=select.value;record.disabled=true;select.disabled=true;
    try{
      stream=await navigator.mediaDevices.getUserMedia({audio:true});
      const type=['audio/webm;codecs=opus','audio/mp4'].find(t=>MediaRecorder.isTypeSupported(t));
      recorder=new MediaRecorder(stream,type?{mimeType:type}:{});
      const chunks=[];let bytes=0;
      recorder.ondataavailable=event=>{if(event.data.size){chunks.push(event.data);bytes+=event.data.size;if(bytes>50*1024*1024&&recorder?.state==='recording')recorder.stop();}};
      recorder.onstop=async()=>{
        const mime=recorder.mimeType;
        clearTimeout(timer);stream.getTracks().forEach(t=>t.stop());recorder=null;stream=null;
        try{await choose(new File(chunks,mime.includes('mp4')?'voice.m4a':'voice.weba',{type:mime}),agent);notice.textContent='Recording saved on this device. Tap Send when ready.';}
        catch(e){notice.textContent=e.message;}
        finally{record.textContent='Record voice';select.disabled=false;controls(enabled,locked);}
      };
      recorder.onerror=()=>{notice.textContent='Recording failed. Try again.';if(recorder?.state==='recording')recorder.stop();};
      recorder.start(1000);timer=setTimeout(()=>{if(recorder?.state==='recording')recorder.stop();},300000);
      record.textContent='Stop recording';notice.textContent='Recording… up to 5 minutes. Nothing is sent until you tap Send.';controls(enabled,locked);
    }catch(e){stream?.getTracks().forEach(t=>t.stop());stream=null;recorder=null;select.disabled=false;controls(enabled,locked);notice.textContent='Microphone unavailable: '+e.message;}
  };
  window.addEventListener('app-background',()=>{if(recorder?.state==='recording')recorder.stop();});
  document.addEventListener('visibilitychange',()=>{if(document.hidden&&recorder?.state==='recording')recorder.stop();});
  return {get:agent=>stored(agent),clear:agent=>stored(agent,null,true),refresh,controls,recording:()=>!!recorder,
    async upload(agent,file){
      const response=await fetch('/api/attachments?agent='+encodeURIComponent(agent)+'&name='+encodeURIComponent(file.name),{
        method:'POST',headers:{'Content-Type':'application/octet-stream','X-CSRF-Token':current.csrf},body:file,signal:AbortSignal.timeout(120000)});
      const result=await response.json();if(!response.ok)throw Error(result.error||'Upload failed');return result;
    }};
})();

/* Same durable inbox as Telegram. Retry keeps the original client identity. */
(() => {
  const select=document.getElementById('message-agent'), input=document.getElementById('message-text');
  const list=document.getElementById('message-list'), send=document.getElementById('message-send');
  const notice=document.getElementById('message-notice'), question=document.getElementById('message-question');
  let selected=null, loading=false, sending=false, ready=false, draft={}, pending=null, rendered='';
  const key=()=> 'tabularium.pending-message.'+select.value;
  function restore(){
    try{pending=JSON.parse(localStorage.getItem(key())||'null');}catch{pending=null;}
    const saved=draft[select.value];
    input.value=pending?.text||saved?.text||'';selected=pending?.question||saved?.question||null;
    input.disabled=!!pending;send.textContent=pending?'Retry send':'Send';
    notice.textContent=pending?'Delivery not confirmed. Retry uses the same message identity.':'';
    showQuestion();dashboardMedia.refresh();
  }
  function showQuestion(){
    question.replaceChildren();question.hidden=!selected;
    if(!selected)return;
    question.append(el('span','Replying to: '+selected.question));
    if(!pending){const cancel=el('button','Cancel reply','button secondary');cancel.type='button';cancel.onclick=()=>{selected=null;showQuestion();};question.append(cancel);}
  }
  async function load(){
    if(loading||document.hidden||document.getElementById('messaging').hidden)return;
    loading=true;const agent=select.value;
    try{
      const data=await api('/api/messages?agent='+encodeURIComponent(agent));
      if(agent!==select.value)return;
      ready=data.ready;send.disabled=sending||!ready||!current?.csrf;dashboardMedia.controls(!!data.media_ready,sending||!!pending);
      document.getElementById('message-connection').textContent=ready?'Agent available':'Agent offline or awaiting Messaging release';
      document.getElementById('message-history-note').textContent=data.note;
      const messages=data.messages.filter(m=>m.channel==='dashboard');
      const signature=JSON.stringify([agent,messages,data.decisions,!!pending]);
      if(signature!==rendered){
        const nearEnd=list.scrollHeight-list.scrollTop-list.clientHeight<70;
        list.replaceChildren();
        for(const m of messages){
          const row=el('article',undefined,'message-row '+m.role);
          row.append(el('div',(m.role==='user'?'You':agentNames[agent])+' · '+m.channel+' · '+new Date(m.time*1000).toLocaleString()+' · '+m.status,'message-meta'),el('p',m.text,'message-body'));
          if(m.attachment)row.append(el('p','Attachment: '+m.attachment.name,'message-meta'));
          const d=m.role==='assistant'&&data.decisions.find(d=>d.id===m.decision_id);
          if(d){const answer=el('button','Reply to this question','button secondary');answer.disabled=!!pending;answer.onclick=()=>{if(pending)return;selected=d;draft[agent]={text:input.value,question:selected};showQuestion();input.focus();};row.append(answer);}
          list.append(row);
        }
        if(!messages.length)list.append(el('p','No recent messages. Start a conversation here.','muted'));
        if(nearEnd||!rendered)list.scrollTop=list.scrollHeight;
        rendered=signature;
      }
    }catch(e){ready=false;send.disabled=true;document.getElementById('message-connection').textContent='Connection unavailable';notice.textContent=e.message;}
    finally{loading=false;}
  }
  document.getElementById('message-form').onsubmit=async event=>{
    event.preventDefault();if(sending||!ready)return;
    if(dashboardMedia.recording()){notice.textContent='Stop recording before sending.';return;}
    const agent=select.value, storageKey=key(), text=input.value, replyTo=selected;
    sending=true;send.disabled=true;select.disabled=true;input.disabled=true;dashboardMedia.controls(true,true);
    try{
      if(!pending){
        const file=await dashboardMedia.get(agent);
        if(!text.trim()&&!file)return;
        if(file&&replyTo)throw Error('Answer the question with text first, then send the attachment separately.');
        pending={client_id:crypto.randomUUID(),text,decision_id:replyTo?.id||0,question:replyTo,has_media:!!file};
        try{localStorage.setItem(storageKey,JSON.stringify(pending));}
        catch(e){pending=null;throw Error('Cannot save a retry identity. Message not sent.');}
      }
      if(pending.has_media&&!pending.attachment){
        const file=await dashboardMedia.get(agent);
        if(!file)throw Error('The saved attachment is unavailable. Keep this message and restore browser storage before retrying.');
        notice.textContent='Uploading '+file.name+'… keep the app open.';
        pending.attachment=await dashboardMedia.upload(agent,file);
        localStorage.setItem(storageKey,JSON.stringify(pending));
      }
      notice.textContent='Saving message…';
      await api('/api/messages',{agent,client_id:pending.client_id,text:pending.text,decision_id:pending.decision_id,attachment:pending.attachment||null},AbortSignal.timeout(20000));
      await dashboardMedia.clear(agent);dashboardMedia.refresh();
      localStorage.removeItem(storageKey);pending=null;selected=null;draft[agent]=null;input.value='';
      notice.textContent='Saved to the agent’s queue.';send.textContent='Send';showQuestion();
    }catch(e){
      // A failed upload cannot have queued a message. Let the owner replace/remove the file.
      // Once a message POST is possible, retain its identity until acceptance is confirmed.
      if(pending?.has_media&&!pending.attachment){localStorage.removeItem(storageKey);pending=null;}
      notice.textContent=(pending?'Not confirmed: ':'')+e.message+(pending?' Retry keeps the same message identity.':'');send.textContent=pending?'Retry send':'Send';}
    finally{sending=false;select.disabled=false;input.disabled=!!pending;send.disabled=!ready;await load();}
  };
  let previous=select.value;
  input.oninput=()=>{draft[select.value]={text:input.value,question:selected};};
  select.onchange=()=>{draft[previous]={text:input.value,question:selected};previous=select.value;rendered='';restore();load();};
  window.addEventListener('messaging-open',()=>{restore();load();});
  document.getElementById('message-menu').onclick=event=>{const open=document.body.classList.toggle('message-menu-open');event.currentTarget.setAttribute('aria-expanded',String(open));};
  window.addEventListener('online',load);
  document.addEventListener('visibilitychange',load);
  setInterval(load,2500);
})();
