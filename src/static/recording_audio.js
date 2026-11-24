// Bounded, automatically queued WAV chunks share one Web Audio playback clock.
// All offsets are session time, including silence reconstructed by the archive.
window.RecordingAudio = class RecordingAudio {
  constructor(onerror) {
    this.onerror = onerror;
    this.context = null;
    this.sources = new Set();
    this.cache = new Map();
    this.token = 0;
    this.active = false;
    this.muted = false;
  }

  stop() {
    this.token++;
    clearInterval(this.timer);
    this.active = false;
    for (const source of this.sources) source.stop();
    this.sources.clear();
  }

  reset() { this.stop(); this.cache.clear(); }

  mute(value) {
    this.muted = value;
    if (this.gain) this.gain.gain.value = value ? 0 : 1;
  }

  async chunk(index, session) {
    const key = `${session}:${index}`;
    if (!this.cache.has(key)) {
      const pending = (async () => {
        const response = await fetch(`/api/viewer/audio?session=${encodeURIComponent(session)}&start=${index * 30}&duration=30&silence=1`,
          {signal: AbortSignal.timeout(20000)});
        if (!response.ok) throw new Error((await response.json()).error);
        return this.context.decodeAudioData(await response.arrayBuffer());
      })();
      this.cache.set(key, pending);
      // Retain at most 90 seconds of decoded audio, regardless of session size.
      while (this.cache.size > 3) this.cache.delete(this.cache.keys().next().value);
      pending.catch(() => { if (this.cache.get(key) === pending) this.cache.delete(key); });
    }
    return this.cache.get(key);
  }

  position() {
    return Math.min(this.duration, this.cursor + Math.max(0, this.context.currentTime - this.started) * this.rate);
  }

  schedule(buffer, index) {
    const begin = Math.max(this.cursor, index * 30);
    const end = Math.min(this.duration, (index + 1) * 30);
    if (end <= begin) return;
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.playbackRate.value = this.rate;
    source.connect(this.gain);
    source.onended = () => this.sources.delete(source);
    source.start(this.started + (begin - this.cursor) / this.rate, begin - index * 30, end - begin);
    this.sources.add(source);
  }

  async start(session, cursor, rate, duration) {
    this.stop();
    const token = this.token;
    if (!this.context) {
      const Audio = window.AudioContext || window.webkitAudioContext;
      if (!Audio) throw new Error('Synchronized audio is unavailable in this browser.');
      this.context = new Audio();
      this.gain = this.context.createGain();
      this.gain.connect(this.context.destination);
      this.mute(this.muted);
    }
    // Resume inside the user's play/seek gesture before waiting for the network.
    await this.context.resume();
    const index = Math.floor(cursor / 30);
    const buffer = await this.chunk(index, session);
    if (token !== this.token) return false;
    // A seek can land milliseconds before a boundary. Buffer the following
    // chunk before starting the clock so it is already ready at that boundary.
    const following = (index + 1) * 30 < duration ? await this.chunk(index + 1, session) : null;
    if (token !== this.token) return false;
    Object.assign(this, {session, cursor, rate, duration, active: true,
      started: this.context.currentTime + .04, next: index + (following ? 2 : 1), pending: false});
    this.schedule(buffer, index);
    if (following) this.schedule(following, index + 1);
    this.timer = setInterval(() => this.pump(), 1000);
    this.pump();
    return true;
  }

  async pump() {
    if (!this.active || this.pending || this.next * 30 >= this.duration || this.next * 30 > this.position() + 40) return;
    this.pending = true;
    const token = this.token, index = this.next;
    try {
      const buffer = await this.chunk(index, this.session);
      if (token !== this.token) return;
      if (this.position() > index * 30) throw new Error('Audio buffering interrupted playback. Press PLAY to resume.');
      this.schedule(buffer, index);
      this.next++;
    } catch (error) {
      if (token === this.token) { this.stop(); this.onerror(error); }
    } finally {
      if (token === this.token) this.pending = false;
    }
  }
};
