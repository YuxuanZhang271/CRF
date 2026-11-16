# CRF

[Project description here]

- [CRF](#crf)
  - [Quick Start](#quick-start)
  - [Flexible Endoscopic Robot](#flexible-endoscopic-robot)
  - [SCOPE](#scope)

## Quick Start

```sh
conda env create -f environment.yaml
conda activate crf
python src/app.py
```

## Flexible Endoscopic Robot

Open `src/flex_endo_robo/flex_endo/FlexEndoscopeDriver.sln` in Visual Studio and build **Debug | x86**. Close any running driver or Web UI and run the full app from the repository root in the `crf` environment.

```powershell
.\src\flex_endo_robo\flex_endo\Start-FlexControl.ps1 -FullApp
```

The launcher connects the UR5, camera and Flex driver to the Web UI at `http://127.0.0.1:8080`. It defaults to planned Flex feedback for the bench setup. Add `-RobotIp` if the UR5 differs from the configured IP. Add `-Feedback Encoder` for encoder-backed control. Use `python src/app.py --arm-only` for RTDE with modeled Flex or `python src/app.py --camera-only` for passive preview. **Ctrl+C** stops the session.

Hold the Web UI bend buttons to straighten the scope and **roll+** or **roll-** to align the base. Release the buttons and click **Calibrate** to save that pose as zero. Setup roll and planned roll travel are limited to ±20° from startup. After calibration, automatic horizon correction uses fresh UR5 pose data. Manual roll pauses horizon correction until the next arm motion, bend jog, or automatic procedure. Encoder calibration requires matching planner and encoder travel. If GTS reports alarm or limit inputs before enabling the drives, check drive power and the shared I/O connection. Once the cause is resolved, click **Recheck Flex inputs**. To bench-test Flex with the camera alone, run the same launcher without `-FullApp`.

The plots below YOLO Categories show FlexEndo bend and roll, and UR5 tip direction in RCM mode. Apply a **Tip limit** of 1° to 10° with the arm stationary. The FlexEndo dashed ring guides setup before calibration. Set the initial **Bend limit** of 1° to 60° with **CALIBRATE**, then use **APPLY** to change it while Flex is idle and inside the requested limit. Re-zeroing retains the travel envelope captured at the first calibration. Flex **RESET** returns the bend axes and base roll to the saved zero. Arm **RESET** clears RCM and returns the UR5 to `[0, -120, -120, -30, 90, 45]` degrees.

The status bar shows controller connections and motion. The console below the plots shows command results and diagnostics. **INTERRUPT** cancels both controllers’ motion and leaves the app open at the resulting pose. Fresh motion input is required to resume. **STOP** cancels motion, requests a PolyScope program stop when connected to the UR5, and shuts down the app. Run the launcher again to resume work after **STOP**.

1. **Bird-eye.** With fresh UR5 joints and the scope calibrated, click **BIRD-EYE** and check the live view pitches 30° below horizontal. The app reports when the target exceeds the available bend or roll.
2. **Pull Out.** Restart with `-FullApp -PullOutClearanceM` followed by the bench-verified distance in metres from the RCM to the final scope tip on the withdrawal side. Calibrate again and capture **RCM** at the intended pivot. **PULL OUT** returns Flex to zero. In planned mode, inspect straightness and clearance before clicking **Confirm straight and retract**. Check withdrawal direction and the endpoint. Use **INTERRUPT** to cancel the sequence and keep the session open.
3. **Tracking.** Verify RCM motion holds the pivot. Select one YOLO category and enable **Tracking** with the object already in frame. Flex follows first inside the 10° RCM cone. The UR5 assists near the Flex limit and can steer along the cone boundary when that moves the camera toward the target. Outside the cone, tracking waits for a target correction that brings the arm closer to the cone while holding the captured pivot and insertion depth.

The full-app planned mode uses the controller's commanded Flex positions for the angle readouts, Bird-eye and tracking decisions. Compare the reported motion with the scope and live camera during bench testing. See [the integration plan](src/flex_endo_robo/PLAN.md) for the full bench checks.

## SCOPE

The ***SCOPE - Surgical Cognition Odometry & Perception Engine***, is the brain of the whole system.
