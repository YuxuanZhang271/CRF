// Exercise shared playback controls and recorded telemetry without any hardware.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
async function main(){
  class Element{
    constructor(){this.listeners={};this.dataset={};this.value='';this.children=[];this.clientWidth=300;this.attrs={};}
    addEventListener(event,fn){this.listeners[event]=fn;}
    replaceChildren(){this.children=[];}
    append(...items){this.children.push(...items);}
    setAttribute(k,v){this.attrs[k]=v;}
    removeAttribute(k){delete this.attrs[k];}
    getContext(){return new Proxy({},{get:()=>()=>{}});}
  }
  const nodes=new Map(), events={}, requests=[], dispatched=[];
  const get=id=>{if(id==='config')return null;if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  get('speed').value='1';get('sessions').value='session';
  let now=0, raf, withAudio=true;
  const windowData=()=>({duration_s:95,ended:true,counts:{},invalid:0,dropped:{},has_audio:withAudio,
    states:[[0,{robot:{arm_enabled:false,task:{id:0,revision:1,name:'Stop',phase:'idle'}}}]],
    performance:[[0,{status:'ready',sequence:1,sample_age_s:0,cpu:{percent:20},memory:{percent:30}}]],
    frames:[],events:[],configs:[],yolo:[]});
  class Audio{
    constructor(){this.active=false;this.muted=false;this.starts=[];globalThis.testAudio=this;}
    stop(){this.active=false;}
    reset(){this.stop();}
    mute(value){this.muted=value;}
    async start(session,cursor,rate,duration){this.starts.push({session,cursor,rate,duration});this.time=cursor;this.active=true;return true;}
    position(){return this.time;}
  }
  const audioWindow={RecordingAudio:Audio,devicePixelRatio:1,addEventListener:(e,fn)=>events[e]=fn,dispatchEvent:e=>dispatched.push(e)};
  const context=vm.createContext({window:audioWindow,document:{getElementById:get,createElement:()=>new Element(),createTextNode:text=>({text}),documentElement:{}},
    CustomEvent:class{constructor(type,{detail}){this.type=type;this.detail=detail;}},
    MutationObserver:class{observe(){}},performance:{now:()=>now},requestAnimationFrame:fn=>raf=fn,
    getComputedStyle:()=>({getPropertyValue:()=>''}),AbortSignal,setTimeout,
    fetch:async url=>{requests.push(url);return {ok:true,json:async()=>url.endsWith('/sessions')?{sessions:[{id:'session',label:'session'}]}:windowData()};}});
  for(const file of ['status.js','recording.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/static',file),'utf8'),context);
  const flush=()=>new Promise(setImmediate);
  await flush();await get('load').listeners.click();await flush();
  assert.equal(get('mute-audio').disabled,false);
  get('play').listeners.click();await flush();
  const audio=globalThis.testAudio;
  audio.time=31;now=100;raf(now);
  assert.match(get('clock').textContent,/0:31.00/,'Camera cursor follows the audio clock');
  assert.equal(dispatched.at(-1).detail.state,null,'Saved performance becomes unavailable after five seconds');
  get('play').listeners.click();assert.equal(audio.active,false);assert.equal(get('play').textContent,'PLAY');
  get('seek').value='4';get('seek').listeners.input();await get('seek').listeners.change();
  assert.equal(dispatched.at(-1).detail.state.cpu.percent,20,'Seek restores recorded performance');
  get('play').listeners.click();await flush();get('speed').value='2';get('speed').listeners.change();await flush();
  assert.equal(audio.starts.at(-1).rate,2);assert.equal(audio.starts.at(-1).cursor,4);
  get('mute-audio').listeners.click();assert.equal(audio.muted,true);
  get('seek').value='62';get('seek').listeners.input();await get('seek').listeners.change();await flush();
  assert.equal(audio.starts.at(-1).cursor,62);assert.equal(audio.active,true);
  // Seeking to the end pauses there instead of treating the seek as a replay request.
  get('seek').value='95.9';get('seek').listeners.input();await get('seek').listeners.change();await flush();
  assert.equal(audio.active,false);assert.match(get('clock').textContent,/1:35.90/);
  events.pagehide();assert.equal(audio.active,false);
  withAudio=false;await get('load').listeners.click();await flush();
  const starts=audio.starts.length;get('play').listeners.click();await flush();now+=500;raf(now);
  assert.equal(audio.starts.length,starts,'Old recordings play without requesting audio');
  assert.equal(get('mute-audio').disabled,true);assert.match(get('clock').textContent,/0:01.00/);
  assert.ok(requests.every(url=>url.startsWith('/api/viewer/')),'Replay never requests live hardware endpoints');
  console.log('Recording playback controls, legacy media and saved performance checks passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
