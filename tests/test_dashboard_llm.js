// node tests/test_dashboard_llm.js — transcript revisions, highlights and cursor ownership.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Element {
  constructor() { this.children = []; this.dataset = {}; this.attributes = {}; this.handlers = {}; this.textContent = ''; this.disabled = false; this.scrollHeight = 100; this.scrollTop = 0; this.clientHeight = 100; }
  append(...nodes) { for (const node of nodes) { this.children.push(node); node.parent = this; } }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  setAttribute(name, value) { this.attributes[name] = value; }
  remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
  get firstChild() { return this.children[0]; }
  get lastChild() { return this.children.at(-1); }
  get firstElementChild() { return this.firstChild; }
  get childElementCount() { return this.children.length; }
}
async function main() {
  const elements = new Map(['listen','llm-status','llm-output','llm-load-time'].map(id => [id,new Element()]));
  const timers = [], calls = [];
  const readyTiming = {phase:'ready',elapsed_s:42.25};
  let feed = {enabled: false, phase: 'ready', model_loading:readyTiming, sequence: 1, events: [
    {type: 'transcript.partial', generation: 1, stream_id: 5, segment_id: 1, text: '向上'},
    {type: 'transcript.final', generation: 1, stream_id: 5, segment_id: 1, text: '向上望少少。'},
    {type: 'command.final', generation: 1, stream_id: 5, segment_id: 1, command: 'Y', status: 'EXECUTE', resolved: true, slots: {I:'bend',T:'none',P:'up'}},
    {type: 'command.dispatch', generation: 1, stream_id: 5, segment_id: 1, task_id: 5, result: {ok:true}}
  ]};
  const context = vm.createContext({document: {getElementById:id=>elements.get(id),createElement:()=>new Element()},
    stopping:false,sessionStopped:false,AbortSignal,
    setStatusChip:(id,label,tone)=>{elements.get(id).label=label;elements.get(id).tone=tone;},
    writeOutput:()=>{},
    setTimeout:fn=>timers.push(fn), fetch:async (url,options)=>{
      calls.push([url,options]); return {ok:true,json:async()=>url.includes('/listen')?{enabled:true,sequence:999,events:[]}:feed};
    }});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/static/status.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/static/llm.js'),'utf8'),context);
  const flush = () => new Promise(resolve=>setImmediate(resolve));
  await flush();
  const output=elements.get('llm-output'),button=elements.get('listen');
  assert.equal(elements.get('llm-status').label, 'Ready');
  assert.equal(elements.get('llm-load-time').textContent, 'Loaded in 42.3s');
  assert.equal(output.children.length,1);
  assert.equal(output.firstChild.firstChild.textContent,'向上望少少。');
  assert.equal(output.firstChild.dataset.partial,'false');
  assert.equal(output.firstChild.dataset.command,'true');
  assert.match(output.firstChild.lastChild.textContent,/Task 5 accepted/);
  await button.handlers.click();
  assert.equal(JSON.parse(calls.at(-1)[1].body).enabled,true);
  feed={enabled:true,phase:'listening',model_loading:readyTiming,sequence:2,events:[]};
  await timers.shift()();await flush();
  assert.equal(calls.at(-1)[0],'/api/llm?after=1','toggle responses must not skip pending events');
  assert.equal(button.attributes['aria-pressed'],'true');
  assert.equal(elements.get('llm-load-time').textContent, 'Loaded in 42.3s', 'LISTEN toggles retain model startup time');
  feed={enabled:false,phase:'ready',sequence:3,events:[
    {type:'listen.changed',generation:2,stream_id:5,enabled:false},
    {type:'transcript.final',generation:2,stream_id:5,segment_id:1,text:'向上望少少。'},
    {type:'command.suppressed',generation:2,stream_id:5,segment_id:1}
  ]};
  await timers.shift()();await flush();
  assert.equal(output.children.length,1,'trigger toggles must retain transcript segment identity');
  assert.match(output.firstChild.lastChild.textContent,/Task triggering disabled/);
  assert.equal(elements.get('llm-status').title,'Ready · Task triggering disabled');
  feed={enabled:true,phase:'listening',sequence:4,events:[
    {type:'command.fast_stop',generation:3,stream_id:5,segment_id:1,status:'EXECUTE',resolved:true,
      slots:{I:'stop',T:'none',P:'none'}},
    {type:'command.final',generation:3,stream_id:5,segment_id:1,status:'NON_COMMAND',resolved:false,
      command:'N',slots:{I:null,T:null,P:null}},
    {type:'command.suppressed',generation:3,stream_id:5,segment_id:1}
  ]};
  await timers.shift()();await flush();
  assert.equal(output.firstChild.dataset.fastStop,'true');
  assert.equal(output.firstChild.dataset.command,'true');
  assert.match(output.firstChild.lastChild.textContent,/Fast STOP/);
  feed={enabled:false,phase:'error',model_loading:readyTiming,error:'Tokenizer failed',sequence:5,events:[
    {type:'command.error',generation:2,stream_id:5,segment_id:1,message:'Tokenizer failed',
      diagnostic:'Tokenizer failed\nInput: invalid Unicode\nTraceback: worker line 19',level:'error'}
  ]};
  await timers.shift()();await flush();
  assert.equal(elements.get('llm-status').label,'Error');
  assert.equal(elements.get('llm-status').tone,'error');
  assert.match(output.lastChild.textContent,/Traceback: worker line 19/);
  assert.equal(output.lastChild.dataset.level,'error');
  assert.equal(elements.get('llm-load-time').textContent,'Loaded in 42.3s', 'a command error is not a model loading failure');
  feed={enabled:false,phase:'loading',model_loading:{phase:'loading',elapsed_s:3.2},sequence:6,events:[]};
  await timers.shift()();await flush();
  assert.equal(elements.get('llm-load-time').textContent,'Loading 3.2s');
  feed={enabled:false,phase:'error',model_loading:{phase:'error',elapsed_s:5.4},sequence:7,events:[]};
  await timers.shift()();await flush();
  assert.equal(elements.get('llm-load-time').textContent,'Load failed after 5.4s');
  context.stopping=true;
  const count=calls.length;await button.handlers.click();assert.equal(calls.length,count);
  console.log('LLM transcripts, task results, loading times, event cursor and shutdown gating passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
