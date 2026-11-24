# Development reference

Use the [README](../README.md) for setup, launching, controls, shortcuts, and tasks. This file covers configuration and implementation details for maintaining CRF.

## Code map

| Path | Responsibility |
| --- | --- |
| `src/app.py` | Application entry point, HTTP API, resource ownership, and shutdown. |
| `src/static/` | Dashboard, keyboard controls, voice panel, and browser recording viewer. |
| `src/flex_endo_robo/control_node.py` | Motion coordinator, manual controls, RCM, and modeled Flex. |
| `src/flex_endo_robo/ur5_rtde_server.py` | UR5 RTDE connection, relay, and Freedrive. |
| `src/flex_endo_robo/kinematics.py`, `ur5_endo_flex.urdf` | Kinematics and robot geometry; both are under `src/flex_endo_robo/`. |
| `src/flex_endo_robo/flex_endo/` | Flex Python interface, native GTS driver, IPC, and PowerShell launcher. |
| `src/scope/camera.py`, `src/scope/yolo.py` | Camera capture, inference, detector categories, and observations. |
| `src/scope/catalog.py`, `surgical.py`, `temporal.py` | Mode metadata, Gastro28 masks, paired temporal model and independent stage inference. |
| `src/tasks.py` | Task IDs, shared state, execution, stop confirmation, and HTTP/voice task coordination. |
| `src/llm/` | Voice service, task mapping, fast Stop detection, dependency pins, and settings. |
| `src/llm/streamingcommand_sdk/` | Vendored speech SDK, CLI, examples, licenses, and model provenance. |
| `src/status.py` | State snapshots and session recording. |
| `src/recording_web.py`, `src/recording_viewer.py` | Standalone browser replay and desktop viewer/export CLI. |
| `src/diagnostics.py` | Dashboard diagnostic feed and per-run warning/error files. |
| `src/performance.py`, `src/static/performance.*` | Passive hardware sampling and dashboard camera overlay. |
| `src/keyboard_listener.py` | Windows bridge for browser-reserved Alt shortcuts. |
| `tests/` | Hardware-free Python checks, Node dashboard checks, and native C++ tests. |
| `environment.yaml` | Pinned Windows Conda environment, including voice requirements. |

## Advanced configuration

Run `python src/app.py --help` for all options. Direct Python launches using Flex require a separately running native driver. The PowerShell launcher manages the driver but forwards only its own supported settings; it does not forward arbitrary Python flags.

| Python option | Default | Purpose |
| --- | --- | --- |
| `--camera-index` | `0` | DirectShow camera device index. |
| `--model` | `src/scope/yolov8n.pt` | YOLO weights. |
| `--surgical-model` | `models/scope/gastro28_yolo26m_seg.pt` | Paired Gastro28 segmentation weights for Full perception mode. |
| `--temporal-model` | `models/scope/YOLO26m_CausalMSTCN_fold0.pt` | Online Causal MS-TCN checkpoint; the paired TeCNO checkpoint is also supported. |
| `--stage-tips` | `src/scope/stage_tips.json` | Reserved JSON mapping stage IDs 1–7 to text, loaded at startup; not displayed in the current dashboard. |
| `--host-web` / `--port-web` | `127.0.0.1` / `8080` | Dashboard bind address and port. |
| `--robot-ip` / `--relay-port` | `192.168.1.10` / `9000` | UR5 address and local RTDE relay port. |
| `--urdf` | `src/flex_endo_robo/ur5_endo_flex.urdf` | Robot/endoscope geometry. |
| `--rcm-local` | `0 0 -0.30` | Local RCM coordinates, in meters. |
| `--tip-local` | `0 0 0.20195` | Local tip coordinates, in meters. |
| `--camera-roll-offset-deg` | `0` | Optical roll offset in degrees, from −180 to 180. |
| `--jog-speed-scale` | `10` | Manual arm speed factor; choices: `3`, `5`, `10`, `15`. |
| `--recordings-dir` | Repository `recordings/` | Session recording output directory. |
| `--llm-config` | `src/llm/settings.json` | Speech/command model configuration. |
| `--microphone` | System default | Input device name on the server PC. |

The mutually exclusive `--camera-only`, `--flex-only`, and `--arm-only` modes select camera preview, Flex without UR5, and UR5 with modeled Flex, respectively. No mode flag selects the full system. Camera-only still starts the speech service, but connects to neither robot device.

For example, select a microphone and configuration while previewing a camera:

```powershell
python src/app.py --camera-only --microphone "Microphone name" --llm-config src/llm/settings.json
```

For a custom test detector, update `CATEGORIES` and `COLORS` in `src/scope/yolo.py` to match its class IDs. Full mode requires the paired Gastro28 weights. Camera resolution and frame-rate defaults are in `src/scope/camera.py`. Launch settings and runtime configuration changes are retained in recordings.

## Gastro28 perception

Perception starts in `test` independently of the hardware launch mode. The mode
API is `POST /api/camera/mode` with `{"mode":"test"}` or `{"mode":"full"}`.
Both this endpoint and `/api/camera/category` accept an optional `revision`;
stale selections receive HTTP 409. Mode changes are rejected during Task 16 or
stopping. Model loading occurs on the workers, outside the motion and camera
locks. An accepted switch immediately increments the camera revision, clears
the category, observations and overlays, and closes streams for the old
revision. Results from old revisions cannot publish tracking observations.

`GET /api/camera/state` includes active category metadata, mode and mode revision,
vision loading/error status, and a stage object. `/api/config` also reports the
active categories. Test IDs retain COCO numbering, including person at 0; Full
mode exposes Gastro28 IDs 1–27. Background 0 is excluded. Voice dispatch resolves
names case-insensitively using this same active registry; candidates include the
surgical names, while unsupported names are rejected in the current mode.

Three independent workers own capture, primary inference, and stage inference.
Each inference worker consumes the latest raw frame rather than a queue. Test
inference retains the existing YOLO boxes. Full inference uses all Gastro28
classes at 512px, stores polygons in original frame coordinates, and filters only
drawing and target selection. Mask overlays and tracking observations retain
their capture timestamps and expire after 0.5 seconds.

The stage worker loads its own YOLO and temporal head only in Full mode. It
samples at most once per capture second, uses the same unmodified BGR frame,
stretches it to 512×512, and extracts layer-22 neck/global/mask features with the
supplied feature protocol. It does not share the primary predictor or run on the
capture/motion threads. The neck hook attaches to the initialized predictor's
actual model because the installed Ultralytics copies the original model during
setup. Repeated-sample smoke tests cover this version-sensitive behavior.

Mode changes release the previous worker models and discard causal history.
Category changes preserve stage context. Sampling gaps over 2.5 seconds reset
history rather than treating widely separated frames as consecutive 1 Hz input.
Stage results older than three seconds are shown as waiting, with no active
prediction or tip. The recent-history display retains at most 120 samples;
Causal MS-TCN retains up to its 505-sample receptive field internally (TeCNO:
509). GPU resource contention can still affect achieved throughput; the workers
do not guarantee a specific camera/inference FPS.

| Stage ID | Label | Statistical duration (seconds) |
| --- | --- | --- |
| 1 | Duodenum transection | 40.8 |
| 2 | Hepatic artery exposure | 568.8 |
| 3 | Ligament incision | 101.4 |
| 4 | Right gastric division | 583.8 |
| 5 | Hepatic artery dissection | 490.2 |
| 6 | Common hepatic dissection | 830.4 |
| 7 | Coronary vein division | 216.6 |

The stage card integrates top-three confidence scores and proportional fills in
the seven stage boxes. Unreturned scores are dashes, not zero probabilities.
The vertical index rail fills the active stage segment using elapsed time
against the supplied statistical duration. Rows with IDs below the current
recognized stage are highlighted and their timing segments fill to 100%. This
progression is derived from the current ID, without retaining a maximum ID or
inventing confidence or elapsed times for earlier stages. It clears when the
current prediction clears, including stale recognition and Test mode. `PHASE_DURATIONS_S` in `src/scope/catalog.py` stores the seven supplied
statistics in checkpoint stage order. Stage metadata exposes each as `duration_s`;
the UI uses that value for its time ratio and displays seconds. Durations are
fixed, with no editable reference controls or assumed five-minute defaults.

The stage worker retains `phase_started_at` from the capture timestamp of each
new top-1 prediction, independently of the bounded history. Mode/context resets
and sampling gaps reset it; category changes preserve it. Status exposes
`phase_elapsed_s` using the server monotonic clock only while the prediction is
fresh. Stage events record the visit start for replay/auditing. Timer overruns
remain visible while the fill caps at 100%; no completion or remaining-time
estimate is inferred. The recent-history strip is removed from the UI. The
Doctor tips UI is also removed; its optional backend configuration remains.

The three main column headings are visually hidden but retained for accessible
region labels. Both side panels remain visible, with no folding controls.
The Surgical stage heading is inside a translucent card anchored over the
camera's upper-left corner. The stage list scrolls within the image height on
small screens. A native `details`/`summary` disclosure starts expanded on both
the dashboard and `/live`; only the body carries the inactive/disabled state,
so the heading remains operable in Test mode. Stage rendering updates the body
without resetting the disclosure state. Desktop columns use a 1:2:1 ratio for controls, camera and VLI
after accounting for the gaps. Existing compact-screen stacking remains. The
camera uses a square viewport sized to `min(100cqw, 100cqh)` within its available
area above the controls. `object-fit: contain` preserves the full source at 1x,
letterboxing non-square inputs. The camera ResizeObserver keeps
zoom controls aligned with the dynamically sized viewport. Zoom transforms only
the image, leaving the stage overlay fixed. Tasks help text and the existing
Tracking button share their respective title rows, aligned right; help text
wraps within its header on narrow screens.

`GET /live` serves a passive duplicate in `src/static/live.html`, with its own
`live.css` and `live.js`. Its header shows the shared lab logo at the top right
without navigation or theme controls. Shared `theme.js` restores the dashboard's
`abml-color-theme` preference and listens for same-origin storage changes to keep
open tabs synchronized; pages without a theme toggle are supported. Its content
grid retains 1:2:1 columns for surgical
stage, annotated camera and an empty reserved column. The stage panel sits in
the left column instead of overlaying the camera. `stage.js` shares the exact
stage rendering with the dashboard (scores, timing rail and inactive states).
The new tab polls `/api/camera/state` and consumes `/api/camera/stream` for the
current revision, reusing the existing camera/inference workers. It follows
mode/category changes and reconnects on stream failure; status failures clear
the image and stage highlights. No dashboard/keyboard script, focus lease,
motion endpoint or additional model instance is used. The recording `/viewer`
routes remain separate. Camera status includes the current shared `TaskState.zoom`.
Dashboard manual zoom updates are coalesced through `POST /api/camera/zoom`,
with one request in flight and an expected task revision. The server rejects
stale updates, updates during stopping, and manual writes while Tasks 3/4 own
zoom. This endpoint updates display state only; it never selects a task or
sends motion. Initial dashboard state restores the saved zoom, and task-feedback
animation never publishes manual updates. The live view polls at 200 ms and
applies that zoom to the annotated image, with a 200 ms visual transition.
Both views clip around the image center and retain the fixed stage panel.

The live camera column also includes the dashboard's five system-status chips.
`status.js` shares device/motion, task and speech label/tone calculations between
both views. The passive view reads `/api/state` and `/api/llm?after=...` at 200 ms,
separately from camera polling; its speech cursor avoids re-fetching event history.
Failed feeds replace affected chips with Unavailable while other feeds continue.
Closing the view cancels its timers/requests and releases its image stream;
restoring a cached tab resumes polling without acquiring a control lease.

Model source, file sizes and hashes are retained in
[`models.json`](../src/scope/models.json) and
[`MODEL_PROVENANCE.md`](../src/scope/MODEL_PROVENANCE.md). Runtime assets live
under ignored `models/scope/`. Segmentation and temporal loading both enforce
the paired vision hash. Temporal loading also checks format, architecture,
512px `global_mask` protocol, and strict state-dict compatibility.

Recordings include `perception_mode` transitions, `surgical_stage` predictions
with capture times/frame IDs, mode-tagged `yolo` events and camera frames, and
mode metadata in sampled state/configuration. Existing JSONL/MJPEG readers
remain compatible; the replay UI does not add a separate stage rail.

## Hardware performance monitoring

`PerformanceMonitor` owns a daemon sampler with a one-second wait between samples.
`GET /api/performance` deep-copies its cached snapshot; HTTP requests never sample
hardware or acquire motion/camera locks. The main dashboard polls independently
with a timeout and bounded 60-second browser history. A stale sample, failed
request or unavailable sampler clears the old readings; reconnection starts a
new history. CPU/RAM use the existing `psutil` dependency. GPU sampling uses the
existing `nvidia-ml-py` NVML binding, without importing Torch or initializing CUDA.
Missing libraries/drivers, unsupported utilization queries and GPU loss remain
unavailable rather than being shown as zero. Monitoring stops on app shutdown.

CPU deltas use native thread IDs mapped to Python thread names, grouping capture,
primary inference, stage inference, control/RTDE/Flex IPC, voice and recording.
Unregistered/native pool threads remain in App / native pools because their
work cannot be attributed reliably to the initiating Python thread. Recursive
child-process CPU/RSS is grouped by speech worker script (ASR, LLM, other).
Process CPU baselines include creation times to avoid PID-reuse spikes; first
observations have no CPU delta. All CPU deltas are divided by logical core count,
matching whole-machine percentages. Group lines and server total are separate,
not additive stacked samples. Native Flex driver/unrelated process usage is
included only in host totals. RAM uses process RSS over installed RAM; shared
pages can be counted by multiple processes, so these are not additive partitions
of host memory. GPU utilization/VRAM is reported per device, without invented
per-thread/model attribution. GPU summary shows the busiest monitored device.
While RECORD is active, telemetry is saved as deduplicated `performance` events
at the monitor's nominal 1 Hz rate, plus the initial session snapshot. It remains
absent from the passive `/live` view.
The overlay uses the stage card's translucent style, caps both cards below half
the image width to avoid overlap, and leaves image zoom independent of the cards.

## Control and task implementation

![System architecture](../imgs/system_architecture.jpg)

The supported deployment uses Windows for GTS and DirectShow, with the UR5 connected directly through RTDE. The control coordinator requests 125 Hz updates and publishes dashboard snapshots at 20 Hz. Flex IPC, RTDE motion/telemetry, camera capture, inference, and HTTP handling run independently. Inference uses the latest frame to avoid accumulating a backlog.

The [alternative architecture image](../imgs/system_architecture_alt.jpg) shows a Jetson deployment concept; the current Windows setup does not require that machine or a ROS bridge.

Task definitions and ownership live in `src/tasks.py`. Task entry stops the previous motion and waits for device acknowledgements before activation. Revisions prevent delayed requests from affecting newer selections. Continuous Tasks 1–10 and 16 remain active at limits; Tasks 11–15 return to Task 0 after confirmed completion. Manual robot controls remain locked during tasks, stopping, errors, and Freedrive transitions.

With a connected UR5, look tasks require active RCM. Task 12 restores the captured RCM reference axis while retaining pivot and insertion depth, then waits for the arm and Flex zero to settle. Tracking additionally requires calibrated Flex, a capable driver, and fresh observations for the selected camera/category revision. Refer to the source for device-specific checks rather than duplicating them in clients.

The browser handles held motion, zoom, and Freedrive so releases are immediate. `src/keyboard_listener.py` forwards Alt+B/P/T/R/M/L on Windows while the dashboard owns a fresh focus lease, suppressing browser defaults and the Alt-release menu action. Outside the dashboard, normal shortcuts continue to work. Modeled Flex arc controls use pointer input; R/F roll shortcuts use the hardware interface.

### CB3 Freedrive compatibility

The current PolyScope 3.15 / CB3 integration intentionally uses `ur_rtde`'s `teachMode()` / `endTeachMode()` compatibility API. The legacy control-script handlers support `freedrive_mode()` / `end_freedrive_mode()`; newer `freedriveMode()` / `endFreedriveMode()` handlers are gated to PolyScope 5.10 and later. See the [UR release notes](https://www.universal-robots.com/articles/ur/release-notes/release-note-software-version-31xx/) and [upstream control script](https://gitlab.com/sdurobotics/ur_rtde/-/blob/master/scripts/rtde_control.script).

Freedrive entry clears queued targets and the RCM pivot. The dashboard heartbeat lease expires after 1.5 seconds; a controller RTDE watchdog also stops the program if relay communication fails. Preserve these behaviors when changing control or shutdown paths.

## Voice integration

The [voice reference](../src/llm/README.md) covers dependency pins, model revisions, cache handling, worker processes, diagnostics, and encoding. The SDK is vendored from the supplied CRF-LLM archive; retain its licenses, notices, and provenance.

To test speech independently of the robot app using the app's configuration:

```powershell
python -m streamingcommand.cli --config src/llm/settings.json run
python -m streamingcommand.cli --config src/llm/settings.json run src/llm/streamingcommand_sdk/example/cantonese.wav
```

The parser produces intent (`I`), target (`T`), and direction (`P`) slots. `src/llm/mapping.py` maps them to task IDs:

| Intent (`I`) | Direction (`P`) | Task ID |
| --- | --- | --- |
| `stop` | Any | 0 |
| `move` | `forward` / `backward` | 1 / 2 |
| `zoom in` / `zoom out` | `none` | 3 / 4 |
| `bend` | `up` / `down` / `left` / `right` | 5 / 6 / 7 / 8 |
| `self rotate` | `left` / `right` | 9 / 10 |
| `bird view` | `none` | 11 |
| `reset` | `none` | 12 |
| `clean lens` | `none` | 13 |
| `spray water` / `spray air` | `none` | 14 / 15 |
| `track` | `none` | 16 |

Except for Stop, non-tracking commands require target `none`. Tracking accepts the current detector's categories; target `none` retains the selected category. Named tracking selects its category, confirms stopping, and waits up to two seconds for fresh inference for that revision. A fresh frame with no target enters Waiting for target. Stop, INTERRUPT, disabling LISTEN, or changing category cancels pending preparation.

Fast Stop detects the standalone English word in ASR partials/finals only while LISTEN is enabled. Other partial, unresolved, duplicate, stale-generation, or superseded command results cannot start tasks. Stop invalidates older pending voice results. Keep all accepted voice tasks on the same coordinator path as dashboard tasks.

### Voice loading benchmark

With `crf` activated, close any running CRF speech session and run:

```powershell
python tests/benchmark_llm_loading.py --workers 8 1 4 --repeats 2
# Also exercise recognition using the bundled recording:
python tests/benchmark_llm_loading.py --workers 4 --repeats 1 --audio src/llm/streamingcommand_sdk/example/cantonese.wav
```

This loads the configured cached models offline, with normal verification and
warmup, without opening a microphone, camera or robot connection. Each trial
uses a fresh Python process and reverses worker-count order on alternate
repeats. The OS file cache is retained, so these are repeated-launch timings,
not measurements after reboot. Python interpreter/app imports before SDK
startup and dashboard/perception work are outside the reported startup time.
The benchmark reports total, resource, ASR engine, language-worker, speech-helper
and LLM durations, plus a real parser check. Concurrent phase times overlap;
do not add them together. JSON results and worker logs are under ignored
`logs/llm-loading/`; use `--output PATH` to retain another run.

`--sequential` disables all startup overlap for a memory-constrained comparison;
it also serializes ASR and LLM startup. `num_workers` adjusts only Transformers'
weight-loading pool. With `parallel_start` enabled, verification uses a separate
bounded pool of four threads, and both CPU language helpers start alongside the
ASR engine. Startup task groups cancel and join unfinished siblings before
releasing their resources. Hash checks, model bank semantics, language-helper
quantization and inference warmups are unchanged. The sequential setting keeps
the previous resource/helper ordering.

## Recording and diagnostics

Sessions use five-minute JSONL/MJPEG/WAV segments under `recordings/`. RECORD
gates storage for all streams, including microphone audio; LISTEN only gates voice
task triggering. This table audits coverage before and after the recording
extension; unavailable devices produce unavailable/null state, not invented data.

| System data | Prior coverage | Current storage and rate |
| --- | --- | --- |
| Annotated camera preview | Recorded | `camera_frame` + indexed `camera.mjpeg`, each published capture frame, JPEG quality 85. Source frame ID, capture clock, size, category/mode revision and overlay source time are retained. Browser zoom is in task state, not baked into the image. |
| Clean camera image | Missing | Same event's `raw_image` indexes `camera_raw.mjpeg`, without overlays, JPEG quality 95 at capture resolution. This is compressed, not lossless sensor output. Encoded only during RECORD. |
| Microphone voice and silence | Missing | `microphone_audio` + `microphone.wav`, every consumed PCM block (normally 20 ms), mono 16 kHz signed little-endian PCM16 after device resampling, before VAD/ASR. Includes LISTEN-off periods during RECORD. |
| ASR and LLM output | Recorded | `llm` events: partial/final transcripts, revisions/segment IDs, slots, clarification, candidate metadata, errors/tracebacks, fast Stop, suppression and dispatch outcomes. No model tensors or token activations are captured. |
| Voice runtime/model state | Events only; no initial state if recording starts late | `state.llm` at 20 Hz and initial snapshot: gate/generation, stream ID and readiness/errors. Worker identity/log path, loading times, settings JSON, pinned model revisions, microphone choice and offline policy in config (updated when models become ready). |
| UR5 telemetry and geometry | Recorded | `state.robot` at 20 Hz from cached feedback: six joint angles, sample freshness/time, motion/connection faults, derived endoscope pose, RCM pivot/axis/depth, tip angles/cone, Freedrive and procedure state. Not the complete 125 Hz RTDE stream, joint torque/current, or a network packet capture. |
| Flex telemetry and modeled shape | Recorded | Same sampled state: bend components/magnitude, roll, tendon positions, model shape, feedback mode, calibration/zero, native feedback stamp/sequence/request/action/result/flags, capabilities, motion/fault and procedure status. Does not archive every native IPC exchange or unexposed motor channels. |
| Water/gas and cleaning | Recorded | Sampled Flex readiness, spray action/phase/error/message, plus command/task transitions and outcomes. No pressure or flow measurements are exposed by the current system. |
| Perception and tracking | Recorded | `yolo` for every completed inference: all returned boxes/confidences/class IDs, segmentation polygons, selected target, pixel error, freshness, capture frame/time and revision. Test mode's configured category filtering still applies. No dense masks, features or unreturned class scores. |
| Surgical stage | Recorded, but omitted from viewer event list | `surgical_stage` at independent 1 Hz sampling when Full mode is ready: top-three scores, frame/time, sample count, phase visit start; sampled status also includes elapsed time and bounded history. Now visible in viewer event lists. |
| Server performance | Missing | `performance` at nominal 1 Hz when the cached monitor sample changes, plus initial snapshot: server CPU/RAM, thread and child-process CPU, process RSS, per-device GPU utilization/VRAM, availability and sample age. Same attribution limits as the dashboard; no hardware queries on the recording thread. |
| Perception mode/category | Recorded | Mode transition event, mode-tagged camera/inference, and sampled metadata/configuration including available categories and stage metadata. |
| Commands and task lifecycle | Recorded | HTTP command bodies/results, shared task selection/stopping/error/completion events, ownership revisions, zoom and sampled current task. Held key requests/releases and focus heartbeats follow the same audited HTTP path; no global keyboard recording. |
| Configuration | Recorded; voice settings path only | Initial/segment config plus changes: launch arguments, JSON-safe module constants, control/Flex limits/calibration, camera selection, and now voice settings contents. Model weights and executable binaries are not duplicated. |
| Console/diagnostics | Recorded while RECORD is active | Python and forwarded dashboard console events, state/media errors, dispatch tracebacks. Separate per-run warning/error log remains independent of RECORD. Worker stderr logs are referenced by path, not copied in full into recordings. |
| Recorder health and timing | Recorded | UTC timestamps, session monotonic/Unix origins, sequence, segment index, nominal state rate, per-kind dropped-record counts and final stop event. Bounded queues drop incoming records under overload instead of blocking producers. |

Schema **2** adds `raw_image` and `audio` references; schema **3** adds
`performance` events and initial performance state. Old JSONL/MJPEG recordings
remain readable and report missing streams as unavailable. A slice contains `events.jsonl`, optional `camera.mjpeg`, optional
`camera_raw.mjpeg`, and optional `microphone.wav`. No microphone file is created
until PCM arrives while RECORD is active. At 16 kHz PCM16, continuous mono audio
uses approximately **9.6 MB per five-minute slice**, plus JSON indexes. Keeping
both camera streams adds image encoding and storage work; queue byte accounting
includes both images and PCM, and camera records leave queue capacity for other
events. Drops remain visible by kind, including `microphone_audio`.

Each audio reference has a segment-local `offset_samples` and `sample_count`.
The enclosing event retains stream-global `sample_start`, stream ID, input format,
`audio_start_monotonic_s`, receive time and stream origin. The first block's
receive time minus duration estimates the stream start; subsequent blocks use
sample counts. It does not claim ADC-level synchronization. WAV files concatenate
stored blocks, so use indexes when recovering gaps or aligning streams. Browser
audio clips reconstruct the requested time range with silence for missing blocks
and can cross segment boundaries. The WAV header and media/index files flush
periodically; normal stop/rotation finalizes headers. A crash can leave the last
buffered records unavailable.

Browser replay reads bounded timeline windows and indexed JPEG byte ranges.
The clean-image checkbox selects the alternate reference; older recordings show
an unavailable message. `recording_audio.js` automatically queues 30-second WAV
chunks using one Web Audio clock for audio and camera playback. Play/pause,
seek, speed changes, session changes and page exit stop or reschedule sources;
mute uses a gain node. At most three decoded chunks are cached. The audio API's
`silence=1` option returns silent gaps for continuous replay, retaining bounded
reads and path/offset/format validation. Each WAV is opened once per request,
including when a chunk crosses slices. Audio buffering failures pause replay.
The performance card reuses `performance.js` in playback mode, receiving only
saved samples and cursor-relative history, with stale readings cleared after
five seconds; it never polls live telemetry. The desktop viewer/export includes
CPU/RAM/GPU summary traces. Neither viewer starts hardware or speech models.

The browser viewer has its own assets in `src/static/recording.*`; `src/recording_web.py` serves it without opening camera or robot connections. Both viewers read the recording format maintained by `src/status.py`.

Additional desktop viewer options:

```powershell
python src/recording_viewer.py recordings/SESSION_ID --start 120 --duration 60
python src/recording_viewer.py recordings --live
python src/recording_viewer.py --help
```

`--duration` defaults to 300 seconds and accepts a maximum of 3600. Input can be a recordings root, session, segment, or `events.jsonl`; `--output` exports PNG/PDF, and `--summary` prints JSON without GUI dependencies.

`src/diagnostics.py` forwards server messages to the dashboard independently of RECORD. Each app run creates a UTF-8 `logs/crf_YYYYMMDD_HHMMSS_microseconds_PID.log` using Hong Kong time, including warning/error tracebacks and run start/end markers. Browser-originated messages are forwarded to the terminal without looping back into the diagnostic feed.

## Dependencies and generated files

The environment installs `kdl-parser-jvytee`. `dependencies/kdl_parser` is an optional upstream source-reference submodule, with its own [README](../dependencies/kdl_parser/README.md) and [license](../dependencies/kdl_parser/LICENSE). Populate it at the commit recorded by CRF with:

```powershell
git submodule update --init --recursive dependencies/kdl_parser
```

The speech SDK is ordinary vendored source, not a submodule. Preserve its source, configuration, licenses, model manifest, and provenance, along with the bundled YOLO weights and GTS SDK `gts.dll` / `gts.lib`.

`.gitignore` excludes native build outputs, Python environments/caches, downloaded `models/` directories, logs, and recordings. StreamingCommand normally stores models and worker logs in user directories; `STREAMINGCOMMAND_CACHE` and `STREAMINGCOMMAND_LOGS` override those locations.

## Validation

Run checks from the repository root with `crf` activated. Python tests use fake hardware; Node.js is required for dashboard checks:

```powershell
python -m unittest discover -s tests -p 'test_*.py'
Get-ChildItem tests/test_*.js | ForEach-Object {
    node $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "Dashboard test failed: $($_.Name)" }
}
```

Native C++ tests in `tests/` exercise motion and spray behavior with mocked GTS calls. For changes to the driver, build the x86 Visual Studio solution and run the relevant native checks using an available C++ toolchain.

For actual model validation without opening cameras, microphones or robots:

```powershell
python tests/smoke_scope_models.py
# Or infer from one frame of a saved video:
python tests/smoke_scope_models.py --video path/to/video.mkv
# Exercise the dashboard against a looping saved video, with hardware disconnected:
python tests/preview_perception.py --video path/to/video.mkv --port 8099
```

The smoke check validates all installed model hashes, mask output, repeated
temporal samples for both heads and context reset. The preview uses the real
camera/inference workers with a file-backed capture; it starts no speech models,
native keyboard hook or robot connections. Its LLM/device statuses are unavailable.

For documentation-only edits, verify local links and heading anchors, script paths, flags/defaults against their parsers, task IDs against `TASK_NAMES`, and shortcut descriptions against the browser/native handlers. Run `git diff --ignore-submodules=all --check` for whitespace validation. If submodule status cannot run because its metadata is inaccessible, validate `.gitmodules` against the indexed gitlink without changing repository permissions.
