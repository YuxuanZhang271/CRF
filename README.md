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

Open `src/flex_endo_robo/flex_endo/XboxEndoscopeTeleoperation.sln` in Visual Studio and build **Debug | x86**. Close any running driver or Web UI and run the full app from the repository root in the `crf` environment.

```powershell
.\src\flex_endo_robo\flex_endo\Start-FlexControl.ps1 -FullApp
```

The launcher connects the UR5, camera and Flex driver to the Web UI at `http://127.0.0.1:8080`. It defaults to planned Flex feedback for the bench setup. Add `-RobotIp` if the UR5 differs from the configured IP. Add `-Feedback Encoder` for encoder-backed control. Use `python src/app.py --arm-only` for RTDE with modeled Flex or `python src/app.py --camera-only` for passive preview. **Ctrl+C** stops the session.

In encoder mode, Xbox **Start** enters or exits setup before calibration. **B/X** and **A/Y** bend, the D-pad rotates the base and the shoulder buttons rotate with tendon compensation. Xbox **Back** stops Flex. If a startup axis fault clears on recheck, release **Start** and press it again to enter setup. Encoder calibration requires matching planner and encoder travel.

In planned mode, hold a Web UI jog button to move and release it to stop. Set the scope physically straight and the base to its intended zero, then click **Calibrate** at rest. Planned roll travel is ±20° from startup. To bench-test Flex with the camera alone, run the same launcher without `-FullApp`.

Before the first calibration there is no software bend limit. Calibration applies a 60° bend limit and re-zeroing retains the travel envelope captured at the first calibration. After calibration, click **Return to Flex zero** in **Flex state** to bring the bend axes and base roll to the latest zero saved in the current driver session. Use **STOP** to cancel the return. Manual bench mode returns to planned axis positions. Encoder-backed mode checks measured position.

1. **Bird-eye.** With fresh UR5 joints and the scope calibrated, click **BIRD-EYE** and check the live view pitches 30° below horizontal. The app reports when the target exceeds the available bend or roll.
2. **Pull Out.** Restart with `-FullApp -PullOutClearanceM` followed by the bench-verified distance in metres from the RCM to the final scope tip on the withdrawal side. Calibrate again and capture **RCM** at the intended pivot. **PULL OUT** returns Flex to zero. In planned mode, inspect straightness and clearance before clicking **Confirm straight and retract**. Check withdrawal direction and the endpoint. **STOP** cancels the sequence.
3. **Tracking.** Verify RCM motion holds the pivot. Select one YOLO category and enable **Tracking** with the object already in frame. Flex follows first. The UR5 assists near the Flex limit within the 10° RCM cone. Check that losing the target stops corrections and that the panel reports when the target cannot be centered.

The full-app planned mode uses the controller's commanded Flex positions for the angle readouts, Bird-eye and tracking decisions. Compare the reported motion with the scope and live camera during bench testing. See [the integration plan](src/flex_endo_robo/PLAN.md) for the full bench checks.

## SCOPE

The ***SCOPE - Surgical Cognition Odometry & Perception Engine***, is the brain of the whole system.
