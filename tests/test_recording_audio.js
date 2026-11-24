// Verify scheduling against the audio clock, seeks, speed, gaps and cancellation.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

async function main() {
  const sources=[], requests=[], timers=new Map();
  let waitForFetch=null, timerId=0;
  class AudioContext {
    constructor(){this.currentTime=100;this.destination={};}
    async resume(){}
    createGain(){return {gain:{value:1},connect(){}};}
    async decodeAudioData(){return {duration:30};}
    createBufferSource(){
      const source={playbackRate:{value:1},connect(){},start(...args){this.args=args;},stop(){this.stopped=true;}};
      sources.push(source);return source;
    }
  }
  const context=vm.createContext({window:{AudioContext},AbortSignal,
    setInterval:fn=>{timers.set(++timerId,fn);return timerId;},clearInterval:id=>timers.delete(id),
    fetch:async url=>{requests.push(url);if(waitForFetch)await (typeof waitForFetch==='function'?waitForFetch(url):waitForFetch);return {ok:true,arrayBuffer:async()=>new ArrayBuffer(4)};}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/static/recording_audio.js'),'utf8'),context);
  const errors=[], audio=new context.window.RecordingAudio(e=>errors.push(e.message));
  const flush=()=>new Promise(setImmediate);
  assert.equal(await audio.start('session',29,2,650),true);
  await flush();
  assert.equal(sources[0].playbackRate.value,2);
  assert.deepEqual(sources[0].args,[100.04,29,1]);
  assert.deepEqual(sources[1].args,[100.54,0,30],'Next chunk must be scheduled without a boundary gap');
  audio.context.currentTime=100.54;
  assert.equal(audio.position(),30);
  audio.mute(true);assert.equal(audio.gain.gain.value,0);
  audio.stop();assert.ok(sources.every(s=>s.stopped));assert.equal(timers.size,0);
  const firstNew=sources.length;
  await audio.start('session',300.5,.5,650);await flush();
  assert.equal(sources[firstNew].args[1],.5,'Seek must use the session-local offset in the correct chunk');
  assert.equal(sources[firstNew].playbackRate.value,.5);
  assert.ok(requests.some(url=>url.includes('start=300&duration=30&silence=1')));
  assert.equal(audio.gain.gain.value,0,'Mute survives seeks and speed changes');
  // The cache remains bounded across a long session and many random seeks.
  for(let cursor=0;cursor<600;cursor+=60){await audio.start('session',cursor,4,650);await flush();assert.ok(audio.cache.size<=3);}
  audio.reset();assert.equal(audio.cache.size,0);
  let release;waitForFetch=new Promise(resolve=>{release=resolve;});
  const pending=audio.start('new session',4,1,90);
  await flush();audio.stop();release();
  assert.equal(await pending,false,'Late buffers must never restart paused/replaced playback');
  waitForFetch=null;
  audio.reset();
  let nextReady;
  const waiting=new Promise(resolve=>{nextReady=resolve;});
  waitForFetch=url=>url.includes('start=30&')?waiting:Promise.resolve();
  const before=sources.length, nearBoundary=audio.start('boundary',29.999,4,95);
  await flush();
  assert.equal(audio.active,false);
  assert.equal(sources.length,before,'A near-boundary seek buffers the following chunk before starting');
  nextReady();await nearBoundary;await flush();waitForFetch=null;
  assert.ok(Math.abs(sources[before+1].args[0]-sources[before].args[0]-.00025)<.000001);
  // Slow prefetch pauses both media instead of silently drifting behind video.
  audio.reset();
  await audio.start('late',0,1,90);await flush();
  audio.next=2;audio.context.currentTime=audio.started+61;
  await audio.pump();assert.match(errors[0],/buffering/);assert.equal(audio.active,false);
  assert.ok(requests.every(url=>url.startsWith('/api/viewer/audio?')));
  console.log('Recording audio clock, boundaries, seek, speed and cancellation checks passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
