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
| `src/static/index.html` | Dashboard structure: device controls, camera preview, category selector, status displays, and console. |
| `src/static/dashboard.css` | Dashboard colors, responsive layout, controls, and motion plot styling. |
| `src/static/dashboard.js` | Browser input handling, held-key heartbeats, API requests, state polling, category selection, and display updates. |
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
```

The Python tests use HTTP requests in memory, actual URDF kinematics, and fake device
interfaces. They do not open the camera, connect to the UR5, or start the motor
driver. Node.js is needed only for the optional JavaScript syntax check.
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

The retired named-world-target prototype, its unused geometry helpers, the old
integration plan, and the Visual C++ template header have been removed. The
active YOLO camera tracker and `--arm-only` modeled Flex mode remain.
