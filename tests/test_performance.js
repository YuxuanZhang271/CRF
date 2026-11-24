// Metric switching, stale readings and reconnection without motion requests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Element {
  constructor() { this.dataset={}; this.attrs={}; this.children=[]; this.listeners={}; this.style={setProperty(){}}; }
  setAttribute(key,value) { this.attrs[key]=value; }
  append(...items) { this.children.push(...items); }
  appendChild(item) { this.children.push(item); }
  replaceChildren() { this.children=[]; }
  addEventListener(name,fn) { this.listeners[name]=fn; }
  querySelectorAll() { return buttons; }
}
const buttons=['cpu','ram','gpu'].map(metric=>{const el=new Element();el.dataset.performanceMetric=metric;return el;});
async function main() {
  const nodes=new Map(), jobs=new Map(), events={}, requests=[];
  const get=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  let id=0, failure=false;
  let sample={status:'ready',sequence:1,sampled_at:100,sample_age_s:0,cpu:{percent:12,cores:8},
    memory:{percent:40,total_bytes:1000,used_bytes:400},components:[
      {id:'vision',label:'Vision',cpu_percent:4,rss_bytes:null},
      {id:'main',label:'CRF shared RAM',cpu_percent:null,rss_bytes:200},
      {id:'asr',label:'ASR workers',cpu_percent:2,rss_bytes:100}],
    gpus:[{id:'gpu0',label:'GPU 0',percent:30,memory_used_bytes:100,memory_total_bytes:1000}]};
  const context=vm.createContext({document:{getElementById:get,createElement:()=>new Element(),createElementNS:()=>new Element()},
    window:{addEventListener:(name,fn)=>{events[name]=fn;}},AbortController,
    setTimeout:(fn,delay)=>{jobs.set(++id,{fn,delay});return id;},clearTimeout:key=>jobs.delete(key),
    fetch:async(url,options)=>{requests.push({url,options});if(failure)throw new Error('Offline');return {ok:true,json:async()=>sample};}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/static/performance.js'),'utf8'),context);
  const flush=()=>new Promise(setImmediate);
  const poll=async()=>{const next=[...jobs].find(([,job])=>job.delay===1000);assert.ok(next);jobs.delete(next[0]);await next[1].fn();await flush();};
  const labels=()=>get('performance-legend').children.map(item=>item.children[1].textContent);
  await flush();
  assert.equal(get('performance-cpu').textContent,'12.0%');
  assert.deepEqual(labels(),['Server total','Vision','ASR workers']);
  buttons[1].listeners.click();
  assert.deepEqual(labels(),['Server total','CRF shared RAM','ASR workers']);
  assert.equal(get('performance-legend').children[1].children[2].textContent,'20.0%');
  assert.equal(buttons[1].attrs['aria-pressed'],'true');
  buttons[2].listeners.click();
  assert.deepEqual(labels(),['GPU 0']);
  assert.match(get('performance-detail').textContent,/VRAM/);
  sample={...sample,sequence:2,sampled_at:101,gpus:[],gpu_error:'GPU driver unavailable.'};
  await poll();assert.equal(get('performance-gpu').textContent,'—');
  assert.equal(get('performance-detail').textContent,'GPU driver unavailable.');
  sample={...sample,sample_age_s:6};await poll();
  assert.equal(get('performance-cpu').textContent,'—');assert.equal(labels().length,0);
  sample={...sample,sample_age_s:0,sequence:3};await poll();
  failure=true;await poll();
  assert.equal(get('hardware-performance').dataset.unavailable,'true');
  assert.equal(get('performance-ram').textContent,'—');
  failure=false;sample={...sample,sequence:4,sampled_at:102};await poll();
  assert.equal(get('hardware-performance').dataset.unavailable,'false');
  buttons[0].listeners.click();
  const firstPath=get('performance-lines').children[0].attrs.d;
  assert.equal((firstPath.match(/M/g)||[]).length,1);
  assert.equal((firstPath.match(/L/g)||[]).length,0,'Reconnect must not bridge stale history');
  events.pagehide();assert.equal(jobs.size,0);
  events.pageshow();await flush();
  assert.ok(requests.every(request=>request.url==='/api/performance' && !request.options.method));
  console.log('Performance dashboard checks passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
