(() => {
  const $ = id => document.getElementById(id);
  let session = '', data = null, cursor = 0, duration = 0, playing = false;
  let windowStart = 0, loading = false, generation = 0, frameIndex = null, frameLoading = false, windowPending = false;
  let nextFrame = null, lastEvents = '';
  let playRevision = 0, starting = false, clockStart = 0, clockCursor = 0, lastDraw = 0, lastPerformance = '';
  const audio = new window.RecordingAudio(error => { pause(); $('audio-status').textContent=error.message; });
  const colors = ['#c995d1','#e9bc62','#5bc5a4','#79b2f2','#edaa78','#d8d2de'];
  const format = t => `${Math.floor(t/60)}:${(t%60).toFixed(2).padStart(5,'0')}`;
  async function json(url) {
    const response = await fetch(url, {signal: AbortSignal.timeout(20000)});
    const value = await response.json(); if (!response.ok) throw new Error(value.error); return value;
  }
  function at(rows, t) {
    let lo=0, hi=rows.length;
    while(lo<hi) { const m=(lo+hi)>>1; if(rows[m][0]<=t) lo=m+1; else hi=m; }
    return lo ? rows[lo-1][1] : null;
  }
  function readings(id, values) {
    const node=$(id); node.replaceChildren();
    for(const [key,value] of values) { const dt=document.createElement('dt'),dd=document.createElement('dd'); dt.textContent=key; dd.textContent=Number.isFinite(value)&&!Number.isInteger(value)?value.toFixed(2):String(value ?? '—'); node.append(dt,dd); }
  }
  async function sessions() {
    const result=await json('/api/viewer/sessions'); $('sessions').replaceChildren();
    for(const item of result.sessions) { const option=document.createElement('option'); option.value=item.id; option.textContent=item.label; $('sessions').append(option); }
    if(session) $('sessions').value=session;
    $('notice').textContent=result.sessions.length ? 'Select LOAD to inspect a recording.' : 'No recordings found in the configured directory.';
  }
  async function loadWindow() {
    if(!session) return;
    if(loading) { windowPending=true; return; }
    loading=true; const token=generation;
    try {
      const start=Math.max(0,Math.floor(cursor/30)*30-15);
      const result=await json(`/api/viewer/window?session=${encodeURIComponent(session)}&start=${start}&duration=60`);
      if(token!==generation) return;
      data=result; windowStart=start; duration=result.duration_s; $('seek').max=String(duration); $('seek').disabled=false; $('play').disabled=false;
      $('mute-audio').disabled=!result.has_audio;
      if(!playing && !starting) $('audio-status').textContent=result.has_audio?'Microphone audio follows playback automatically. Gaps play as silence.':'No microphone audio in this recording. Camera playback is available.';
      readings('summary', [['Duration',format(duration)],['Session',result.ended?'Saved':'Incomplete / live'],['Camera frames',result.counts.camera_frame||0],['State samples',result.counts.state||0],['Task events',result.counts.task||0],['Speech events',result.counts.llm||0],['Invalid lines',result.invalid],['Dropped records',Object.values(result.dropped).reduce((a,b)=>a+b,0)]]);
      $('notice').textContent='Drag the timeline to seek. Charts show a 60-second window around playback.';
      lastEvents=''; draw();
    } catch(error) { $('notice').textContent=error.message; pause(); }
    finally { loading=false; if(windowPending){windowPending=false;loadWindow();} }
  }
  function currentTime() { return audio.active ? audio.position() : Math.min(duration,clockCursor+(performance.now()-clockStart)/1000*Number($('speed').value)); }
  function pause() {
    if(playing)cursor=currentTime();
    playing=false; starting=false; playRevision++; audio.stop(); $('play').textContent='PLAY';
  }
  async function play() {
    if(!data)return;
    if(cursor>=duration)cursor=0;
    const token=++playRevision;
    starting=true; $('play').textContent='PAUSE';
    try {
      if(data.has_audio) {
        $('audio-status').textContent='Buffering synchronized audio…';
        if(!await audio.start(session,cursor,Number($('speed').value),duration))return;
      }
      if(token!==playRevision)return;
      clockCursor=cursor;clockStart=performance.now();playing=true;starting=false;
      $('audio-status').textContent=data.has_audio?'Microphone audio synchronized · gaps play as silence.':'No microphone audio in this recording.';
      if(cursor<windowStart||cursor>windowStart+50)loadWindow();
    }catch(error){if(token===playRevision){pause();$('audio-status').textContent=error.message;}}
  }
  async function load() {
    pause(); audio.reset(); generation++; session=$('sessions').value; cursor=0; data=null; frameIndex=null;
    lastPerformance='';window.dispatchEvent(new CustomEvent('recording-performance',{detail:{state:null,history:[]}}));
    $('frame').removeAttribute('src'); $('no-frame').hidden=false;
    $('play').disabled=true; $('seek').disabled=true; $('mute-audio').disabled=true; $('notice').textContent='Loading recording index…';
    while(loading) await new Promise(resolve=>setTimeout(resolve,40));
    await loadWindow();
  }
  function plot(id,title,series) {
    const canvas=$(id), ctx=canvas.getContext('2d'), ratio=window.devicePixelRatio||1;
    const width=canvas.clientWidth, height=canvas.clientHeight||160; canvas.width=width*ratio; canvas.height=height*ratio; ctx.scale(ratio,ratio);
    const style=getComputedStyle(document.documentElement), fg=style.getPropertyValue('--dim'), edge=style.getPropertyValue('--edge');
    const values=series.flatMap(s=>s.values.map(v=>v[1])).filter(Number.isFinite);
    let low=values.length?Math.min(...values):0, high=values.length?Math.max(...values):1;
    if(high-low<.1){low-=1;high+=1;} const pad=(high-low)*.08; low-=pad;high+=pad;
    const x=t=>45+(width-60)*(t-windowStart)/60, y=v=>height-24-(height-72)*(v-low)/(high-low);
    ctx.font='11px system-ui';ctx.fillStyle=fg;ctx.fillText(title,5,13);
    for(let i=0;i<3;i++){const v=low+(high-low)*i/2;ctx.fillText(v.toFixed(1),0,y(v)+4);ctx.strokeStyle=edge;ctx.beginPath();ctx.moveTo(43,y(v));ctx.lineTo(width-10,y(v));ctx.stroke();}
    series.forEach((s,i)=>{ctx.strokeStyle=colors[i%colors.length];ctx.beginPath();let open=false;for(const [t,v]of s.values){if(!Number.isFinite(v)){open=false;continue;}if(!open){ctx.moveTo(x(t),y(v));open=true;}else ctx.lineTo(x(t),y(v));}ctx.stroke();ctx.fillStyle=colors[i%colors.length];ctx.fillText(s.name,45+i*(width-50)/series.length,30);});
    ctx.strokeStyle=colors[0];ctx.setLineDash([3,3]);ctx.beginPath();ctx.moveTo(x(cursor),22);ctx.lineTo(x(cursor),height-24);ctx.stroke();ctx.setLineDash([]);
    ctx.fillStyle=fg;ctx.fillText(format(windowStart),45,height-5);ctx.fillText(format(windowStart+60),width-65,height-5);
  }
  function showFrame(index) {
    nextFrame=index;
    const raw=$('raw-camera').checked, key=`${index}:${raw}`;
    if(frameLoading || key===frameIndex || index===null) return;
    frameLoading=true; const token=generation, image=new Image();
    const done=ok=>{frameLoading=false;if(token===generation && index===nextFrame && raw===$('raw-camera').checked){frameIndex=key;if(ok){$('frame').src=image.src;$('no-frame').hidden=true;}else{$('frame').removeAttribute('src');$('no-frame').hidden=false;$('no-frame').textContent=raw?'Clean image unavailable in this recording':'Frame unavailable';}}if(nextFrame!==index || raw!==$('raw-camera').checked || token!==generation)showFrame(nextFrame);};
    image.onload=()=>done(true);image.onerror=()=>done(false);
    image.src=`/api/viewer/frame?session=${encodeURIComponent(session)}&index=${index}&raw=${$('raw-camera').checked?1:0}`;
  }
  function events() {
    const visible=data.events.filter(e=>e.time<=cursor), signature=visible.map(e=>`${e.time}:${e.kind}:${e.data.sequence||''}`).join('|');
    if(signature===lastEvents)return;lastEvents=signature;
    $('events').replaceChildren();$('speech').replaceChildren();
    for(const event of visible.slice(-180)) {
      const node=document.createElement('div');node.className='replay-event';node.dataset.kind=event.kind;
      const time=document.createElement('time');time.textContent=format(event.time);node.append(time,document.createTextNode(event.label));
      if(event.kind==='llm') {
        const e=event.data;
        if(e.slots){node.dataset.command=String(e.resolved||e.command==='Y');node.append(document.createTextNode(' '+JSON.stringify(e.slots)));}
        $('speech').append(node);
      } else $('events').append(node);
    }
    for(const id of ['events','speech'])$(id).scrollTop=$(id).scrollHeight;
  }
  function draw() {
    if(!data)return;
    $('seek').value=String(cursor);$('clock').textContent=`${format(cursor)} / ${format(duration)}`;
    const state=at(data.states,cursor),robot=state?.robot||{},flex=robot.flex_hardware||{};
    const task=robot.task;
    readings('state',[['Task',task?`${task.id} · ${task.name}`:'—'],['Phase',task?.phase],['ARM',robot.joints_received?'Connected':'Unavailable'],['Flex',flex.connected?'Connected':'Unavailable'],['Bend',flex.bend??robot.flex_theta_total_deg],['Roll',flex.roll??robot.flex_base_yaw],['Tracking',flex.tracking_state],['Camera',state?.camera?.ready?'Ready':'Unavailable']]);
    const chips=systemStatus(robot);
    for(const [id,label,chip] of [
      ['replay-arm','ARM',chips['arm-status']],['replay-flex','FLEX',chips['flex-connection']],
      ['replay-motion','MOTION',chips['motion-status']],['replay-task','TASK',taskStatus(task)],
      ['replay-llm','LLM',speechStatus(state?.llm||{})]
    ]) {$(id).textContent=`${label} ${chip.label}`;$(id).dataset.tone=chip.tone;}
    const history=(data.performance||[]).filter(([t])=>t<=cursor && t>=cursor-60);
    const latest=at(data.performance||[],cursor);
    const row=[...(data.performance||[])].reverse().find(([t])=>t<=cursor);
    const valid=latest?.status==='ready' && Number.isFinite(latest.sample_age_s) && row && cursor-row[0]+latest.sample_age_s<=5;
    const signature=`${valid}:${row?.[0]}:${windowStart}`;
    if(signature!==lastPerformance){
      lastPerformance=signature;
      window.dispatchEvent(new CustomEvent('recording-performance',{detail:{state:valid?latest:null,
        history:history.filter(([,s])=>s.status==='ready' && s.sample_age_s<=5).map(([,s])=>s)}}));
    }
    const value=(s,key,index)=>{const r=s.robot||{},f=r.flex_hardware||{};if(key==='bend')return f.bend??r.flex_theta_total_deg;if(key==='roll')return f.roll??(Number.isFinite(r.flex_base_yaw)?r.flex_base_yaw*180/Math.PI:null);if(key==='tendon'){const t=f.tendons??r.tendons;return Array.isArray(t)?t[index]:t?.[['R','G','B','Y'][index]]?.[0];}return r[key]?.[index];};
    const series=(name,key,index)=>({name,values:data.states.map(([t,s])=>[t,value(s,key,index)])});
    plot('joints','UR5 · degrees',Array.from({length:6},(_,i)=>series(`J${i+1}`,'joints_deg',i)));
    plot('flex','Flex · degrees',[series('bend','bend'),series('roll','roll')]);
    plot('tendons','Tendons · mm',Array.from({length:4},(_,i)=>series(`T${i+1}`,'tendon',i)));
    const frame=at(data.frames.map(f=>[f.time,f]),cursor);
    if(frame)showFrame(frame.index);else{$('frame').removeAttribute('src');$('no-frame').hidden=false;frameIndex=null;nextFrame=null;}
    const yolo=at(data.yolo,cursor);$('frame-status').textContent=frame?`Frame ${frame.index} · ${format(frame.time)} · ${yolo?.detections?.length??0} detections`: 'No recorded frame before this time';
    events();
  }
  function tick(now) {
    if(playing&&data){cursor=currentTime();if(cursor>=duration)pause();if(cursor<windowStart||cursor>windowStart+50)loadWindow();if(now-lastDraw>40){draw();lastDraw=now;}}
    requestAnimationFrame(tick);
  }
  $('load').addEventListener('click',load);$('refresh').addEventListener('click',async()=>{try{await sessions();if(session)await loadWindow();}catch(e){$('notice').textContent=e.message;}});
  $('play').addEventListener('click',()=>{if(playing||starting){pause();draw();}else play();});
  let resumeAfterSeek=false;
  $('seek').addEventListener('input',()=>{resumeAfterSeek=resumeAfterSeek||playing||starting;pause();cursor=Number($('seek').value);if(cursor<windowStart||cursor>windowStart+55)loadWindow();else draw();});
  $('seek').addEventListener('change',async()=>{await loadWindow();if(resumeAfterSeek){resumeAfterSeek=false;if(cursor<duration)play();else draw();}});
  $('speed').addEventListener('change',()=>{const resume=playing||starting;if(audio.active)cursor=audio.position();else if(playing)cursor=Math.min(duration,clockCursor+(performance.now()-clockStart)/1000*previousSpeed);playing=false;pause();previousSpeed=Number($('speed').value);if(resume)play();});
  let previousSpeed=Number($('speed').value);
  $('mute-audio').addEventListener('click',()=>{audio.mute(!audio.muted);$('mute-audio').setAttribute('aria-pressed',String(audio.muted));$('mute-audio').textContent=audio.muted?'Unmute audio':'Mute audio';});
  $('raw-camera').addEventListener('change',()=>{frameIndex=null;draw();});
  window.addEventListener('pagehide',()=>pause());
  window.addEventListener('resize',draw);new MutationObserver(draw).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
  sessions().catch(e=>{$('notice').textContent=e.message;});requestAnimationFrame(tick);
})();
