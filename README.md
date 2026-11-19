# CRF

Windows control application for a UR5 arm, a tendon-driven flexible endoscope,
and a SCOPE camera. The browser dashboard combines arm jogging, remote center
of motion (RCM) control, Flex calibration and positioning, and YOLO tracking.

## Setup and launch

Run from the repository root:

```powershell
conda env create -f environment.yaml
conda activate crf
```

The pinned Windows environment includes PyKDL, OpenCV, Ultralytics, and
`ur-rtde`. For the native driver, install Visual Studio 2022 with the C++ desktop
workload, v143 toolset, and a Windows SDK. Build
`src/flex_endo_robo/flex_endo/FlexEndoscopeDriver.sln` in **Debug | x86** to match
the supplied GTS library. Python and the driver exchange fixed-size messages
through Windows shared memory.

Close any running driver or Web UI, then start the full system:

```powershell
.\src\flex_endo_robo\flex_endo\Start-FlexControl.ps1
```

Open [the control panel](http://127.0.0.1:8080). The default UR5 address is
`192.168.1.10`. The launcher starts the driver and Python app and waits for the
driver to exit when the app closes.

| Launcher option | Purpose |
| --- | --- |
| `-RobotIp 192.168.1.10` | Override the UR5 address. |
| `-Port 8080` | Change the dashboard port. |
| `-Configuration Release` | Use a Release build. |
| `-Feedback Planned` | Default: use planned motor travel as feedback. |
| `-Feedback Encoder` | Use encoder feedback. |
| `-Feedback Simulated` | Simulate Flex motors; full-app mode still connects to the UR5. |
| `-FlexOnly` | Run Flex without connecting to the UR5. |
| `-CameraRollOffsetDeg 0` | Supply optical roll relative to the modeled camera. |
| `-GasPort COM4` | Arduino gas-pump serial port (9600 baud, 8N1; `1` on / `0` off). |
| `-WaterVelocity 30` | Axis-6 water-pump velocity; nonzero, from -30 to 30 controller units. Verify direction and flow on the bench. |

Pull Out uses a fixed endpoint 100 mm beyond the captured RCM on the withdrawal
side. This preserves the previous 100 mm setting. It describes the final tip
location, so the total withdrawal from the starting pose can be about 267 mm.

Other entry points:

```powershell
# Passive camera preview without motor or RTDE connections.
python src/app.py --camera-only

# Simulated Flex without a UR5 connection; requires a built native driver.
.\src\flex_endo_robo\flex_endo\Start-FlexControl.ps1 -FlexOnly -Feedback Simulated

# UR5 with modeled Flex, without the native motor driver.
python src/app.py --arm-only

# Full app when the Flex driver is already running separately.
python src/app.py --robot-ip 192.168.1.10

# Show all Python app options.
python src/app.py --help
```

Startup leaves motion idle. Calibrate Flex at rest after physically aligning
the scope. Camera tracking requires a selected category, fresh feedback,
calibrated Flex, and an active UR5 RCM. **INTERRUPT** cancels motion while keeping
the session open; **STOP** stops motion and shuts down the app.

The camera zoom slider defaults to **1x** and ranges from **1x to 2x**. Use the
**1x**, **1.5x**, and **2x** presets, or hold **ZOOM IN** / **ZOOM OUT** for continuous
adjustment. Zoom enlarges and crops the center of the displayed image.

**WATER SPRAY** and **GAS SPRAY** sit between APPLY and CALIBRATE in FlexEndo.
Hold either button to spray and release to stop. Pointer cancellation, moving
the mouse outside the button, focus loss, and held-input expiry also stop the
spray. A focused spray button supports holding Space or Enter.
**CLEAN LENS**, between BIRD-EYE and PULL OUT, runs water for **3 seconds**,
waits for axis 6 to stop, then runs gas for **3 seconds**. Click it again to
cancel. INTERRUPT and STOP cancel both pump outputs.

Spraying requires stationary connected motion hardware, released manual
controls, and no tracking or positioning procedure. Motion and settings are
blocked during spraying; automatic horizon adjustments stay paused until
manual control or a new procedure resumes them. Bend calibration and a UR5
connection are not required in Flex-only mode. Water and gas availability are
independent; CLEAN LENS requires both. Missing gas hardware does not prevent
Flex motion or water spraying. Pump faults remain latched until driver restart.

The native driver owns axis 6 and the serial port, uses nonblocking stage
timing, and stops pumps on application-heartbeat loss. Water stop confirmation
uses controller status/velocity. Gas feedback confirms serial writes only,
not measured flow. The Arduino firmware is external to this repository; its
`1`/`0` protocol cannot guarantee shutoff after driver termination, USB loss,
or host failure without a firmware watchdog. Simulated mode performs no pump
or serial I/O. Python and native code must both use IPC protocol V5.

## Architecture

`app.py` owns HTTP routes and process lifetimes. `control_node.py` coordinates
arm intent and procedures; `ur5_rtde_server.py` owns RTDE connections and arm
motion. `flex_control.py` coordinates physical Flex behavior, while
`flex_ipc.py` transports commands and feedback to the C++ driver. The camera
workers share one camera stream across viewers, with capture and preview
running separately from YOLO inference.

### Thread ownership

| Worker | Responsibility |
| --- | --- |
| Control coordinator | Steps motion procedures at a requested 125 Hz and publishes UI snapshots at 20 Hz. Uses an interruptible wait between ticks. |
| Flex IPC | Reads native feedback and writes the latest complete command/vision snapshots; retries short mutex contention independently of motion calculations. |
| RTDE motion | Sole owner of UR5 motion calls, including reset and stop. |
| RTDE receive and TCP receiver | Acquire and deliver arm telemetry independently of motion calculations. |
| Camera capture/preview | Reads the camera, encodes JPEGs, and publishes frames without waiting for inference. |
| YOLO inference | Processes the newest available raw frame; skips superseded frames instead of growing a queue. |
| HTTP request handlers | Serve controls, cached state, and MJPEG streams. State requests do not acquire the motion lock; stale control snapshots return HTTP 503. |

Standalone UR5 RESET and Flex RESET can run independently, in either order.
Pull Out, camera tracking, and Bird-eye retain coordinated command ownership;
an arm reset cancels a coordinated procedure. STOP and INTERRUPT still cancel
both devices.

## Keyboard and activity indicators

The mappings and requested indicator behavior are in [keyboard_shortcuts.md](keyboard_shortcuts.md).
Focus the dashboard to use held arrow keys and `[` / `]` for UR5 motion,
`W` / `A` / `S` / `D` for Flex, and `R` / `F` for physical Flex roll.
`I` interrupts motion. `Ctrl+B`, `Ctrl+P`, `Ctrl+T`, and `Ctrl+R` trigger
Bird-eye, Pull Out, Track, and Record through their existing button handlers.
Motion releases on key-up, focus loss, hidden tabs, and failed state polling;
held keys use the same heartbeat/expiry checks as pointer controls. Auto-repeat
does not repeatedly execute commands, and shortcuts are suspended in form fields.
In arm-only modeled mode, `R` / `F` do not change arc length; the modeled arc
buttons remain available with the pointer.

Windows starts `keyboard_listener.py` to forward browser-reserved Ctrl shortcuts
to the focused dashboard. It requires both a recent browser focus lease and a
foreground window title beginning with `CRF Control`. It queues button clicks;
the native hook does not issue motor commands. Other platforms retain the DOM
listener, where browser-reserved shortcuts may take precedence. The implementation
uses the documented [Windows low-level keyboard hook](https://learn.microsoft.com/en-us/windows/win32/winmsg/lowlevelkeyboardproc).

| Button | Indicator |
| --- | --- |
| BIRD-EYE | Yellow while positioning; green when the modeled camera view is 30° downward and level within 1°, with settled feedback, even if reached manually. |
| PULL OUT | Yellow preparing; sky blue at the aligned UR5 / saved Flex zero pose; yellow withdrawing; green after settled completion. |
| TRACK | Green following/centered; yellow when correction is limited; sky blue while waiting for a target. |
| RECORD | Green while active; the console displays the zero-based slice directory index and the current slice timer. |

**PULL OUT now requires two presses in both feedback modes.** First press aligns
the UR5 and returns Flex to saved zero. When sky blue, verify straightness and a
clear withdrawal path, then press **PULL OUT** or `Ctrl+P` again. The extra confirm
button has been removed. Confirmation expires after 30 seconds and is rejected
if feedback or the prepared pose changes. Colors come from state feedback, and
the text below the buttons describes the current phase.

## Session recording

Use **RECORD** beside the console to start a session. The button turns green while
recording. Press it again and confirm the popup to stop and save. Recording also
closes during normal app shutdown, including **STOP**. It works in camera-only
preview and continues when the dashboard is closed or refreshed.

`src/status.py` defines the recording schema, configuration collection, sample
rate, rotation interval, and background storage. Control settings stay owned by
their existing controllers; the recorder collects their applied values. Sessions
are saved to `recordings/<UTC-session-id>/` by default; use
`--recordings-dir <path>` to choose another folder. Recordings are ignored by Git.

Each session contains five-minute numbered directories (`00000`, `00001`, …):

| File | Contents |
| --- | --- |
| `events.jsonl` | One complete JSON object per line, with event type, sequence, UTC timestamp, elapsed seconds, monotonic timestamp, and data. |
| `camera.mjpeg` | Concatenated JPEG preview frames, created when frames are available. Each `camera_frame` JSON event identifies its file, byte offset, and byte length. |

The shared timeline accommodates different source rates:

- Robot and camera status are sampled at a target 20 Hz, independently of the
  browser. Robot snapshots include source timestamps, joint angles, modeled
  endoscope-link Cartesian pose in the base frame, Flex roll/bend/tendons,
  connections, motion, faults, calibration flags, and native command feedback.
  These are dashboard-rate snapshots, not every 125 Hz servo/RTDE packet.
- Configuration is saved at the start of every segment and when values change:
  launch arguments, Python module defaults, applied tip/bend/speed limits,
  captured RCM point/axis/depth, and Flex calibration and feedback modes.
- Camera previews are recorded at their available capture rate, even without a
  browser viewer. They contain the same detection overlays as the dashboard.
  YOLO events record every completed inference, including boxes, classes,
  confidence, selected target, centering error, category revision, and original
  frame capture timestamp. Match frame/revision IDs and capture times rather
  than assuming inference and preview advance together.
- Requests and HTTP outcomes are recorded for controls, settings, category
  selection, **BIRD-EYE**, **PULL OUT**, **TRACK**, **INTERRUPT**, and **STOP**,
  including refused commands. Matching request IDs join requests and responses;
  motion completion is reported by subsequent robot feedback. **CALIBRATE**
  also records the available pose at request time. Native raw motor zero offsets
  are not exposed by the shared-memory protocol and are not reconstructed.
- Python logging and dashboard console messages are recorded while active.
  Dashboard messages require an open connected browser. The separate C++ driver
  terminal's raw stdout is not forwarded to Python; its exposed fault and command
  feedback are captured in robot snapshots.

One writer performs disk I/O, with a bounded queue (4096 records and a 32 MiB
estimated payload budget), reserving capacity for commands and logs when camera
traffic is high. Disk errors and dropped-record counts appear beside **RECORD**;
drop counts are also saved in state and session-stop events. Files flush about
once a second and on stop. A forced process termination can lose buffered data;
completed JSONL lines remain readable. Five-minute rotation limits individual
file size; total disk use keeps growing until recording stops. Camera images
usually account for most of it.

## Recording viewer

Run the viewer in the `crf` environment (Matplotlib, NumPy, and Pillow are already
included). It reads files only and never connects to the arm, Flex, or camera.

```powershell
# Inspect the first five minutes of a saved session.
python src/recording_viewer.py recordings/<session-id>

# Follow the newest recording and automatically discover new slices/sessions.
python src/recording_viewer.py recordings --live

# Inspect minutes 5–10, or pass a single numbered segment / events.jsonl file.
python src/recording_viewer.py recordings/<session-id> --start 300 --duration 300

# Export a static plot or a JSON activity/configuration summary.
python src/recording_viewer.py recordings/<session-id> --output activity.png
python src/recording_viewer.py recordings/<session-id> --summary
```

The window shows six UR5 joint traces, Flex bend/roll, four tendon traces,
command markers, a command/console log, connection and tracking states, applied
limits, drop counts, and the recorded camera preview with YOLO summary. Drag the
time slider to inspect a point in the selected window. In live mode, the window
refreshes once per second and follows the newest data. Use a session path to
follow just that session. Offline analysis defaults to the first five minutes;
`--start` and `--duration` select another window (up to one hour).

The reader retains a bounded window and reads only newly appended complete JSONL
lines on refresh. It retries partial lines and buffered image bytes, tolerates
invalid complete lines with a visible count, and reads camera JPEGs by byte range.
Preview overlays come from the recording; the viewer does not rerun YOLO.

Flex commands carry the time when the coordinator last published the intent.
The IPC worker repeats that timestamp, so a stalled coordinator still trips the
native 350 ms command watchdog. Vision keeps the original capture timestamp;
late inference does not make an old target fresh. Preview overlays use recent
completed detections, and inference failure is reported while preview continues.
Thread rates are scheduling targets, not hard real-time guarantees.

Native position completion allows a settled profile residual of up to eight
pulses (under 0.01° on roll). Planned return-to-zero uses the same tolerance.
Trap acceleration has a 0.01 pulse/ms² floor so a small roll correction beside
a large tendon move cannot produce a near-zero ramp. Shared completion timing
includes acceleration/deceleration and smoothing, with velocities solved for
that duration while retaining the existing speed caps.
If a planned profile stops 8–64 pulses from its target, the driver can issue at
most two small correction moves toward the original endpoint. It does not
report completion until feedback reaches the eight-pulse tolerance. Larger
errors, continued motion past the deadline, exhausted retries, and SDK errors
still fault. Correction moves are disabled in encoder mode.
The encoder travel check remains 25 pulses in encoder mode, and a profile still
moving at its deadline still faults. Driver faults `-112` and `-113` mean profile
completion timeout and encoder-following failure respectively; terminal output
includes the residual, feedback mode, and axis status. A rejected trap command
also logs its start/target, acceleration, deceleration, velocity, and duration.

## File reference

### Application

| File | Functionality |
| --- | --- |
| `src/app.py` | Entry point, CLI options, HTTP API, dashboard asset serving, camera streaming, and coordinated stop/shutdown handling. |
| `src/status.py` | Recording schema/configuration collection, independent status sampling, timestamped events, bounded queue, and five-minute JSONL/MJPEG storage. |
| `src/keyboard_listener.py` | Windows focus-gated bridge for browser-reserved Ctrl command shortcuts. |
| `src/recording_viewer.py` | Offline/live activity plots, time-slider inspection, camera/YOLO replay, PNG/PDF export, and JSON summary. |
| `src/static/index.html` | Dashboard structure: device controls, camera preview, category selector, status displays, and console. |
| `src/static/dashboard.css` | Dashboard colors, responsive layout, controls, and motion plot styling. |
| `src/static/dashboard.js` | Browser input handling, held-key heartbeats, API requests, state polling, category selection, and display updates. |
| `src/static/keyboard.js` | Keyboard motion/command mappings, repeat/editing guards, and native shortcut polling. |
| `src/flex_endo_robo/control_node.py` | 125 Hz coordinator, relay connection, input expiry, RCM capture, arm commands, modeled Flex, Bird-eye/Pull Out procedures, and state snapshots. |
| `src/flex_endo_robo/kinematics.py` | KDL chain construction, forward kinematics, camera/shaft geometry, RCM angle conversions, horizon leveling, and wrist-constrained inverse kinematics. |
| `src/flex_endo_robo/ur5_rtde_server.py` | RTDE relay, command validation, speed/RCM constraints, telemetry, watchdogs, interrupt handling, and Dashboard Stop. Also supports standalone relay execution. |
| `src/flex_endo_robo/ur5_endo_flex.urdf` | Robot description: UR5, endoscope mount, flexible section, camera frames, and visualization geometry. |
| `src/flex_endo_robo/flex_endo/flex_control.py` | Physical Flex state machine: calibration, jogging, horizon correction, camera tracking with arm assistance, positioning, return to zero, and withdrawal. |
| `src/flex_endo_robo/flex_endo/flex_ipc.py` | Dedicated shared-memory I/O worker, immutable feedback snapshots, bounded latest-intent publication, session ownership, and reconnection. |
| `src/flex_endo_robo/flex_endo/Start-FlexControl.ps1` | Launches the driver and app, selects feedback mode, and passes runtime options. |
| `src/scope/camera.py` | Separate DirectShow capture/preview and inference workers, latest-frame handoff, category revisions, timestamped observations, and JPEG streaming. |
| `src/scope/yolo.py` | YOLO inference wrapper, categories/colors, detection and observation records, target selection, centering error, and overlays. |
| `src/scope/yolov8n.pt` | Bundled YOLO model weights. |

### Native Flex driver

Paths below are relative to `src/flex_endo_robo/flex_endo/`.

| File | Functionality |
| --- | --- |
| `FlexEndoscopeDriver.sln` | Visual Studio solution with Debug and Release x86 configurations. |
| `FlexEndoscopeDriver/FlexEndoscopeDriver.vcxproj` | C++17 build settings, SDK linkage, output directories, and runtime/config copying. |
| `FlexEndoscopeDriver/FlexEndoscopeDriver.vcxproj.filters` | Source and header grouping in Visual Studio. |
| `FlexEndoscopeDriver/FlexEndoscopeDriver.cpp` | Native motion owner for tendon axes 1–4 and roll axis 5; GTS calls, simulation, calibration references, limits, watchdogs, and command execution. |
| `FlexEndoscopeDriver/flex_protocol.h` | Message layouts, actions, results, flags, session names, timeouts, and ABI assertions. Must match `flex_ipc.py`. |
| `FlexEndoscopeDriver/GTS800.cfg` | Vendor controller configuration loaded during hardware initialization. |
| `FlexEndoscopeDriver/gts.h` | Vendor GTS SDK declarations. |
| `FlexEndoscopeDriver/gts.lib` | Vendor x86 import library used when linking the driver. |
| `FlexEndoscopeDriver/gts.dll` | Vendor runtime library copied beside the driver executable. |

### Repository and verification

| File | Functionality |
| --- | --- |
| `README.md` | Setup, launch instructions, architecture, and file reference. |
| `environment.yaml` | Pinned Windows Conda environment and pip dependencies. |
| `.gitignore` | Excludes caches, build outputs, logs, editor files, and local configuration. |
| `.gitmodules` | Defines the upstream KDL parser submodule. |
| `src/flex_endo_robo/flex_endo/.gitignore` | Excludes local Visual Studio settings. |
| `imgs/sys_architecture.jpg` | System architecture illustration. |
| `tests/test_app.py` | Hardware-free HTTP checks for dashboard assets, state/config routes, and invalid requests. |
| `tests/test_controls.py` | Hardware-free checks for modeled Flex, cancellation, camera tracking setup, kinematics, and IPC layouts. |
| `tests/test_threads.py` | Worker isolation, slow inference, frame skipping, category/failure invalidation, IPC contention, and stale command/vision timestamp checks. |
| `tests/test_flex_motion.cpp` | Native profile/completion checks using mocked GTS calls: bounded residual correction, unequal-axis ramps, synchronized timing, speed caps, SDK rejection, encoder checks, and saved zero. Never opens hardware. |

### Bundled KDL parser dependency

`dependencies/kdl_parser` is third-party source. The environment installs
`kdl-parser-jvytee`; the submodule provides an upstream reference or a source
for local installation. Its implementation and license are maintained separately
from the application.

| File within `dependencies/kdl_parser/` | Functionality |
| --- | --- |
| `README.md` | Upstream description and installation instructions. |
| `LICENSE` | Upstream license terms. |
| `setup.py` | Python package metadata and installation configuration. |
| `kdl_parser/__init__.py` | KDL parser package marker. |
| `kdl_parser/urdf.py` | Converts URDF descriptions into PyKDL trees. |
| `kdl_parser/scripts/display_urdf` | Command-line utility for inspecting URDF/KDL structure. |
| `kdl_parser/urdf_parser_py/__init__.py` | Bundled URDF parser package marker. |
| `kdl_parser/urdf_parser_py/urdf.py` | URDF element models and XML serialization/parsing. |
| `kdl_parser/urdf_parser_py/sdf.py` | SDF element models and XML serialization/parsing. |
| `kdl_parser/urdf_parser_py/xml_reflection/__init__.py` | XML reflection package exports. |
| `kdl_parser/urdf_parser_py/xml_reflection/basics.py` | Basic XML value conversion and helpers. |
| `kdl_parser/urdf_parser_py/xml_reflection/core.py` | XML reflection types, attributes, aggregates, and serialization machinery. |

## Validation

With the `crf` environment active:

```powershell
python -m unittest discover -s tests -v
node --check src/static/dashboard.js
node tests/test_dashboard_recording.js
node tests/test_dashboard_spray.js
node tests/test_dashboard_zoom.js
node --check src/static/keyboard.js
node tests/test_keyboard.js
```

The Python tests use HTTP requests in memory, actual URDF kinematics, and fake device
interfaces. They do not open the camera, connect to the UR5, or start the motor
driver. Node.js runs the optional JavaScript syntax and recording-button checks.
Build the native solution separately in Visual Studio. Physical calibration,
camera alignment, and coordinated motion still require bench verification.

To build from a VS 2022 Developer PowerShell or Developer Command Prompt:

```powershell
msbuild src/flex_endo_robo/flex_endo/FlexEndoscopeDriver.sln /p:Configuration=Debug /p:Platform=x86
```

To run the native completion checks, use an **x86** VS 2022 developer shell
after building the driver:

```powershell
$driverSource = 'src/flex_endo_robo/flex_endo/FlexEndoscopeDriver'
$testOutput = 'src/flex_endo_robo/flex_endo/build/Win32/Debug'
cl /nologo /std:c++17 /EHsc /source-charset:.936 /execution-charset:utf-8 "/Fo$testOutput/test_flex_motion.obj" "/Fe$testOutput/test_flex_motion.exe" tests/test_flex_motion.cpp "$driverSource/gts.lib" /link /INCREMENTAL:NO
& "$testOutput/test_flex_motion.exe"
```

These checks mock GTS feedback and never open hardware. Fault messages from the
negative cases are expected; the final line reports the number of passed checks.

The pump state machine also has a standalone native test:

```powershell
cl /nologo /std:c++17 /EHsc "/Fo$testOutput/test_spray_control.obj" "/Fe$testOutput/test_spray_control.exe" tests/test_spray_control.cpp
& "$testOutput/test_spray_control.exe"
# Opt-in real shared-memory integration; the test always spawns --simulate.
$env:CRF_SIM_DRIVER = (Resolve-Path "$testOutput/FlexEndoscopeDriver.exe").Path
python -m unittest discover -s tests -p test_spray_driver.py -v
```

The retired named-world-target prototype, its unused geometry helpers, the old
integration plan, and the Visual C++ template header have been removed. The
active YOLO camera tracker and `--arm-only` modeled Flex mode remain.
