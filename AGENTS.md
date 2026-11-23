# Repository guidance

## Start here

- Read [README.md](README.md) for the system, setup, launch modes, operation, shortcuts, and tasks.
- Read [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for the code map, advanced configuration, control details, dependency handling, and validation commands.
- For voice work, also read [src/llm/README.md](src/llm/README.md) and the relevant vendored SDK documentation.
- Keep README focused on helping users configure, run, and operate CRF. Put implementation rationale and developer references in the development guide.
- The shortcut and task references are consolidated into README. Update its corresponding sections when behavior changes.

## Environment and entry points

- Supported deployment: Windows, PowerShell, Conda environment `crf`, UR5 RTDE, native GTS Flex driver, and DirectShow camera.
- Main launcher: `src/flex_endo_robo/flex_endo/Start-FlexControl.ps1`. App entry: `src/app.py`.
- Driver solution: `src/flex_endo_robo/flex_endo/FlexEndoscopeDriver.sln`; build Debug or Release **x86** with MSVC v143. Executable output: `build/Win32/<Configuration>/FlexEndoscopeDriver.exe` relative to the solution directory.
- `Planned` feedback controls physical motors. `Simulated` simulates only Flex; combine it with `-FlexOnly` to omit the UR5 connection. Camera preview still requires a camera.
- `--camera-only` connects to neither robot device. It still starts the speech service. Speech models are downloaded ahead of time and loaded from cache.
- `src/recording_web.py` and `src/recording_viewer.py` inspect saved sessions without opening hardware connections.

## Sources of truth

| Area | Files to inspect |
| --- | --- |
| Launch arguments and defaults | `Start-FlexControl.ps1`, `src/app.py` |
| Manual and RCM motion | `src/flex_endo_robo/control_node.py`, `kinematics.py` in the same directory |
| Flex capabilities and protocol | `src/flex_endo_robo/flex_endo/flex_control.py`, `flex_ipc.py`, native driver headers/source |
| Task IDs, entry, stopping, completion | `src/tasks.py` |
| Shortcuts and focus/release handling | `src/static/keyboard.js`, `src/keyboard_listener.py`, `src/static/dashboard.js` |
| Detector categories and camera revisions | `src/scope/yolo.py`, `src/scope/camera.py` |
| Voice dispatch, gating, Stop | `src/llm/service.py`, `mapping.py`, `fast_stop.py` in the same directory |
| Recording format and replay | `src/status.py`, `src/recording_viewer.py`, `src/recording_web.py`, `src/static/recording.*` |
| Diagnostics | `src/diagnostics.py` |

## Working conventions

- Preserve unrelated working-tree edits. Scope changes to the user's request.
- Use hardware-free validation for routine code work. Launchers and motion endpoints can operate real devices; use them only within the user's requested hardware workflow.
- Preserve stop acknowledgements, input lease expiry, calibration/capability checks, RCM validity, and locks during task/Freedrive transitions.
- Route accepted voice and dashboard tasks through the shared task coordinator. Keep task IDs synchronized with README and voice mappings.
- LISTEN gates task triggering, not recognition. Disabling LISTEN leaves active tasks running; fast spoken Stop also requires LISTEN to be on.
- Preserve CB3 Freedrive's `teachMode()` / `endTeachMode()` compatibility calls unless the supported controller requirements change. See the development guide for context.
- Treat `src/llm/streamingcommand_sdk/` as vendored source; retain licenses, notices, and provenance. `dependencies/kdl_parser` is the only declared submodule; preserve the recorded commit unless an update is requested.
- Keep generated build files, caches, downloaded models, recordings, and logs out of Git. Retain the bundled YOLO weights, GTS SDK binaries, and SDK model manifest/configuration.
- Put reusable project facts here or in the development guide, not transient progress reports or machine-specific session notes.

## Validation

- Run relevant Python tests using the activated `crf` environment and relevant dashboard tests with Node. Full commands are in [the development guide](docs/DEVELOPMENT.md#validation).
- Changes to motion, stop behavior, voice dispatch, or recording need the corresponding hardware-free regression checks. Native driver changes also need appropriate x86 build/native checks when the toolchain is available.
- For documentation-only changes, check local links/anchors, executable paths, supported flags/defaults, task IDs, and shortcut mappings. Do not start hardware to validate prose.
- Use `git diff --ignore-submodules=all --check` for whitespace checks. Report any checks that could not run and their concrete cause.
