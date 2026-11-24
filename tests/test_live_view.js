// Passive live view follows revisions, clears stale data and never sends commands.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Element {
  constructor() { this.dataset={};this.attrs={};this.children=[];this.listeners={};this.style={}; }
  setAttribute(name,value) { this.attrs[name]=value; }
  getAttribute(name) { return this.attrs[name]; }
  removeAttribute(name) { delete this.attrs[name]; }
  set src(value) { this.attrs.src=value; }
  addEventListener(name,fn) { this.listeners[name]=fn; }
  append(...items) { this.children.push(...items); }
  appendChild(item) { this.children.push(item); }
  replaceChildren() { this.children=[]; }
  querySelector() { return this.dd ||= new Element(); }
}
async function main() {
  const nodes=new Map(), timers=[], events={}, requests=[];
  const get=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  const phases=[{id:1,name:'First',duration_s:40.8},{id:2,name:'Second',duration_s:568.8}];
  let feed={mode:'full',revision:2,ready:true,zoom:1.6,vision_status:'ready',stage:{status:'ready',phases,
    top3:[{id:2,prob:.85}],phase_elapsed_s:120}};
  let failure=false;
  let robotFailure=false, speechFailure=false;
  let robotFeed={arm_enabled:true,relay_connected:true,joints_received:true,arm_moving:false,tracking:true,
    flex_hardware:{connected:true,moving:false},task:{id:16,revision:1,name:'Tracking',phase:'running'}};
  let speechFeed={phase:'listening',enabled:true,sequence:3};
  const context=vm.createContext({document:{getElementById:get,createElement:()=>new Element()},
    window:{addEventListener:(name,fn)=>{events[name]=fn;}},AbortSignal,AbortController,
    setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout(){},
    fetch:async(url,options)=>{
      requests.push({url,options});
      if(failure || (robotFailure && url==='/api/state') || (speechFailure && url.startsWith('/api/llm')))throw new Error('Disconnected');
      return {ok:true,json:async()=>url==='/api/state'?robotFeed:url.startsWith('/api/llm')?speechFeed:feed};
    }});
  for(const file of ['stage.js','status.js','live.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/static',file),'utf8'),context);
  const flush=()=>new Promise(setImmediate), poll=async()=>{const jobs=timers.splice(0);await Promise.all(jobs.map(fn=>fn()));await flush();};
  await flush();
  assert.equal(get('camera-feed').attrs.src,'/api/camera/stream?revision=2');
  assert.equal(get('camera-feed').style.transform,'scale(1.6)','new live tabs restore current zoom');
  const chip=id=>get(id).querySelector('dd').textContent;
  assert.equal(chip('arm-status'),'Connected');
  assert.equal(chip('flex-connection'),'Connected');
  assert.equal(chip('motion-status'),'Tracking');
  assert.equal(chip('task-status'),'16 · Running');
  assert.equal(chip('llm-status'),'Listening');
  assert.equal(get('motion-status').dataset.tone,'tracking');
  assert.equal(get('stage-rail').children[1].attrs['aria-current'],'step');
  assert.equal(get('stage-rail').children[1].children[1].children[3].textContent,'120 / 568.8 s');
  get('camera-feed').listeners.load();
  assert.equal(get('camera-placeholder').hidden,true);
  // The dashboard changes mode/category: follow its new stream revision.
  feed={mode:'test',revision:3,ready:true,zoom:1.2,stage:{status:'inactive',phases,top3:[]}};
  await poll();
  assert.equal(get('camera-feed').attrs.src,'/api/camera/stream?revision=3');
  assert.equal(get('surgical-stage').dataset.inactive,'true');
  assert.equal(get('perception-mode-label').textContent,'Test · boxes');
  assert.equal(get('camera-feed').style.transform,'scale(1.2)');
  robotFeed={...robotFeed,motion_fault:'Fault',tracking:false,flex_hardware:{fault:1},task:{id:16,revision:2,name:'Tracking',error:'Lost target'}};
  speechFeed={phase:'error',error:'Speech failure',sequence:4};
  await poll();
  assert.equal(chip('arm-status'),'Error');
  assert.equal(chip('flex-connection'),'Error');
  assert.equal(chip('task-status'),'16 · Error');
  assert.equal(chip('llm-status'),'Error');
  assert.equal(get('llm-status').dataset.tone,'error');
  robotFailure=true;await poll();
  for(const id of ['arm-status','flex-connection','motion-status','task-status'])assert.equal(chip(id),'Unavailable');
  assert.equal(get('camera-feed').attrs.src,'/api/camera/stream?revision=3','system status failure must not interrupt camera');
  robotFailure=false;speechFailure=true;
  robotFeed={arm_enabled:false,task:{id:0,revision:3,name:'Stop',phase:'idle'}};
  await poll();
  assert.equal(chip('arm-status'),'Disabled');
  assert.equal(chip('flex-connection'),'Disabled');
  assert.equal(chip('task-status'),'Ready');
  assert.equal(chip('llm-status'),'Unavailable');
  speechFailure=false;speechFeed={phase:'ready',enabled:false,sequence:5};await poll();
  assert.equal(chip('llm-status'),'Ready');
  assert.equal(get('llm-status').title,'Ready · Task triggering disabled');
  for (const zoom of [1.4,1.6,1.8,2,1.7,1.4,1]) {
    feed={...feed,zoom};await poll();
    assert.equal(get('camera-feed').style.transform,`scale(${zoom})`,'follow server zoom tasks without a dashboard tab');
  }
  feed={mode:'full',revision:4,ready:true,stage:{status:'ready',phases,top3:[{id:1,prob:.9}],phase_elapsed_s:5}};
  await poll();
  failure=true;await poll();
  assert.equal(get('camera-feed').attrs.src,undefined);
  assert.equal(get('camera-placeholder').hidden,false);
  assert.equal(get('perception-mode-label').textContent,'Disconnected');
  assert.ok(get('stage-rail').children.every(row=>row.attrs['aria-current']===undefined));
  assert.equal(chip('task-status'),'Unavailable');
  failure=false;await poll();
  assert.equal(get('camera-feed').attrs.src,'/api/camera/stream?revision=4');
  assert.equal(get('stage-rail').children[0].attrs['aria-current'],'step');
  get('camera-feed').listeners.error();
  assert.equal(get('camera-feed').attrs.src,undefined);
  await poll();
  assert.equal(get('camera-feed').attrs.src,'/api/camera/stream?revision=4');
  const count=requests.length;
  events.pagehide();
  assert.equal(get('camera-feed').attrs.src,undefined);
  assert.ok(requests.every(r=>['/api/camera/state','/api/state'].includes(r.url) || r.url.startsWith('/api/llm?after=')));
  assert.ok(requests.every(r=>!r.options.method),'live view only performs read-only requests');
  events.pageshow({persisted:true});await flush();
  assert.equal(requests.length,count+3);
  console.log('Live view stage, zoom, shared system/voice status, disconnect recovery and passive requests passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
