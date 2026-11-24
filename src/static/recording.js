(() => {
  const $ = id => document.getElementById(id);
  let session = '', data = null, cursor = 0, duration = 0, playing = false, last = 0;
  let windowStart = 0, loading = false, generation = 0, frameIndex = null, frameLoading = false;
  let nextFrame = null, lastEvents = '', lastConfig = '';
  let audioUrl = null;
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
    for(const [key,value] of values) { const dt=document.createElement('dt'),dd=document.createElement('dd'); dt.textContent=key; dd.textContent=String(value ?? '—'); node.append(dt,dd); }
  }
  async function sessions() {
    const result=await json('/api/viewer/sessions'); $('sessions').replaceChildren();
    for(const item of result.sessions) { const option=document.createElement('option'); option.value=item.id; option.textContent=item.label; $('sessions').append(option); }
    if(session) $('sessions').value=session;
    $('notice').textContent=result.sessions.length ? 'Select LOAD to inspect a recording.' : 'No recordings found in the configured directory.';
  }
  async function loadWindow() {
    if(loading || !session) return;
    loading=true; const token=generation;
    try {
      const start=Math.max(0,Math.floor(cursor/30)*30-15);
      const result=await json(`/api/viewer/window?session=${encodeURIComponent(session)}&start=${start}&duration=60`);
      if(token!==generation) return;
      data=result; windowStart=start; duration=result.duration_s; $('seek').max=String(duration); $('seek').disabled=false; $('play').disabled=false;
      readings('summary', [['Duration',format(duration)],['Session',result.ended?'Saved':'Incomplete / live'],['Camera frames',result.counts.camera_frame||0],['State samples',result.counts.state||0],['Task events',result.counts.task||0],['Speech events',result.counts.llm||0],['Invalid lines',result.invalid],['Dropped records',Object.values(result.dropped).reduce((a,b)=>a+b,0)]]);
      $('notice').textContent='Drag the timeline to seek. Charts show a 60-second window around playback.';
      lastEvents=''; lastConfig=''; draw();
    } catch(error) { $('notice').textContent=error.message; pause(); }
    finally { loading=false; }
  }
  function pause() { playing=false; $('play').textContent='PLAY'; }
  async function load() {
    $('voice-audio').pause(); $('voice-audio').removeAttribute('src');
    if(audioUrl) { URL.revokeObjectURL(audioUrl); audioUrl=null; }
    pause(); generation++; session=$('sessions').value; cursor=0; data=null; frameIndex=null;
    $('frame').removeAttribute('src'); $('no-frame').hidden=false;
    $('play').disabled=true; $('notice').textContent='Loading recording index…';
    while(loading) await new Promise(resolve=>setTimeout(resolve,40));
    await loadWindow();
  }
  function plot(id,title,series) {
    const canvas=$(id), ctx=canvas.getContext('2d'), ratio=window.devicePixelRatio||1;
    const width=canvas.clientWidth, height=160; canvas.width=width*ratio; canvas.height=height*ratio; ctx.scale(ratio,ratio);
    const style=getComputedStyle(document.documentElement), fg=style.getPropertyValue('--dim'), edge=style.getPropertyValue('--edge');
    const values=series.flatMap(s=>s.values.map(v=>v[1])).filter(Number.isFinite);
    let low=values.length?Math.min(...values):0, high=values.length?Math.max(...values):1;
    if(high-low<.1){low-=1;high+=1;} const pad=(high-low)*.08; low-=pad;high+=pad;
    const x=t=>45+(width-60)*(t-windowStart)/60, y=v=>height-24-(height-52)*(v-low)/(high-low);
    ctx.font='11px system-ui';ctx.fillStyle=fg;ctx.fillText(title,5,13);
    for(let i=0;i<3;i++){const v=low+(high-low)*i/2;ctx.fillText(v.toFixed(1),0,y(v)+4);ctx.strokeStyle=edge;ctx.beginPath();ctx.moveTo(43,y(v));ctx.lineTo(width-10,y(v));ctx.stroke();}
    series.forEach((s,i)=>{ctx.strokeStyle=colors[i%colors.length];ctx.beginPath();let open=false;for(const [t,v]of s.values){if(!Number.isFinite(v)){open=false;continue;}if(!open){ctx.moveTo(x(t),y(v));open=true;}else ctx.lineTo(x(t),y(v));}ctx.stroke();ctx.fillStyle=colors[i%colors.length];ctx.fillText(s.name,120+i*52,13);});
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
    const value=(s,key,index)=>{const r=s.robot||{},f=r.flex_hardware||{};if(key==='bend')return f.bend??r.flex_theta_total_deg;if(key==='roll')return f.roll??(Number.isFinite(r.flex_base_yaw)?r.flex_base_yaw*180/Math.PI:null);if(key==='tendon'){const t=f.tendons??r.tendons;return Array.isArray(t)?t[index]:t?.[['R','G','B','Y'][index]]?.[0];}return r[key]?.[index];};
    const series=(name,key,index)=>({name,values:data.states.map(([t,s])=>[t,value(s,key,index)])});
    plot('joints','UR5 · degrees',Array.from({length:6},(_,i)=>series(`J${i+1}`,'joints_deg',i)));
    plot('flex','Flex · degrees',[series('bend','bend'),series('roll','roll')]);
    plot('tendons','Tendons · mm',Array.from({length:4},(_,i)=>series(`T${i+1}`,'tendon',i)));
    const frame=at(data.frames.map(f=>[f.time,f]),cursor);
    if(frame)showFrame(frame.index);else{$('frame').removeAttribute('src');$('no-frame').hidden=false;frameIndex=null;nextFrame=null;}
    const yolo=at(data.yolo,cursor);$('frame-status').textContent=frame?`Frame ${frame.index} · ${format(frame.time)} · ${yolo?.detections?.length??0} detections`: 'No recorded frame before this time';
    events();const config=JSON.stringify(at(data.configs,cursor)||{},null,2);if(config!==lastConfig){$('config').textContent=config;lastConfig=config;}
  }
  function tick(now) {
    if(playing&&data&&!loading){cursor=Math.min(duration,cursor+Math.min(.25,(now-last)/1000)*Number($('speed').value));if(cursor>=duration)pause();if(cursor>windowStart+50)loadWindow();draw();}
    last=now;requestAnimationFrame(tick);
  }
  $('load').addEventListener('click',load);$('refresh').addEventListener('click',async()=>{try{await sessions();if(session)await loadWindow();}catch(e){$('notice').textContent=e.message;}});
  $('play').addEventListener('click',()=>{if(cursor>=duration)cursor=0;playing=!playing;$('play').textContent=playing?'PAUSE':'PLAY';if(cursor<windowStart)loadWindow();});
  $('seek').addEventListener('input',()=>{cursor=Number($('seek').value);if(cursor<windowStart||cursor>windowStart+55)loadWindow();else draw();});
  $('seek').addEventListener('change',()=>loadWindow());
  $('raw-camera').addEventListener('change',()=>{frameIndex=null;draw();});
  $('listen-audio').addEventListener('click',async()=>{
    if(!session)return;
    const token=generation, start=cursor;
    $('voice-audio').pause(); $('listen-audio').disabled=true;
    try {
      const response=await fetch(`/api/viewer/audio?session=${encodeURIComponent(session)}&start=${start}&duration=15`,{signal:AbortSignal.timeout(20000)});
      if(!response.ok){const result=await response.json();throw new Error(result.error);}
      const blob=await response.blob(); if(token!==generation)return;
      if(audioUrl)URL.revokeObjectURL(audioUrl);
      audioUrl=URL.createObjectURL(blob);$('voice-audio').src=audioUrl;
      $('audio-status').textContent=`Microphone audio from ${format(start)}. Missing audio is silent.`;
      await $('voice-audio').play();
    }catch(error){$('audio-status').textContent=error.message;}
    finally{$('listen-audio').disabled=false;}
  });
  window.addEventListener('resize',draw);new MutationObserver(draw).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
  sessions().catch(e=>{$('notice').textContent=e.message;});requestAnimationFrame(tick);
})();
