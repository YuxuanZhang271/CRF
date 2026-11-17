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
.\src\flex_endo_robo\flex_endo\Start-FlexControl.ps1
```

The launcher connects the UR5, camera and Flex driver to the Web UI at `http://127.0.0.1:8080`. It defaults to planned Flex feedback for the bench setup. Add `-RobotIp` if the UR5 differs from the configured IP. Add `-Feedback Encoder` for encoder-backed control or `-FlexOnly` to bench-test Flex with the camera alone. Use `python src/app.py --arm-only` for RTDE with modeled Flex or `python src/app.py --camera-only` for passive preview. **Ctrl+C** stops the session.

Hold the Web UI bend buttons to straighten the scope and **roll+** or **roll-** to align the base. Release the buttons and click **Calibrate** to save that pose as zero. Roll travel is limited to ±180° from startup in setup and planned modes, and from the first calibration in encoder mode. After saving zero, **roll+** and **roll-** coordinate base roll with all four tendons to roll the camera around its viewing axis. Automatic horizon correction uses fresh UR5 pose data and keeps the last stable image orientation near vertical. Manual roll pauses horizon correction until the next arm motion, bend jog, or automatic procedure. Encoder calibration requires matching planner and encoder travel. If GTS reports alarm or limit inputs before enabling the drives, check drive power and the shared I/O connection. Once the cause is resolved, click **Recheck Flex inputs**.

The plots below the **FlexEndo** section in the control panel show FlexEndo bend and roll, and UR5 tip direction in RCM mode. The UR5 plot also shows axial travel in millimetres from the pose captured when RCM is enabled. Insertion is positive and extraction is negative. Apply a **Tip limit** of 10° to 30° with the arm stationary. The FlexEndo dashed ring guides setup before calibration. Set the initial **Bend limit** of 30° to 90° with **CALIBRATE**, then use **APPLY** to change it while Flex is idle and inside the requested limit. The default tip and bend limits are 10° and 60°. Re-zeroing retains the travel envelope captured at the first calibration. Flex **RESET** returns the bend axes and base roll to the saved zero. Arm **RESET** clears RCM and returns the UR5 to `[0, -120, -120, -30, 90, 45]` degrees.

The status bar shows controller connections and motion. The console below the plots shows command results and diagnostics. **INTERRUPT** cancels both controllers’ motion and leaves the app open at the resulting pose. Fresh motion input is required to resume. **STOP** cancels motion, requests a PolyScope program stop when connected to the UR5, and shuts down the app. Run the launcher again to resume work after **STOP**.

The UR5 **Tip limit** is the cone half-angle measured from the rigid shaft direction captured when you enable RCM. A red marker means the measured shaft angle is outside the cone. Jog inward to recover.

Use the device's **APPLY** button to save the position and speed limits with the motion controls released. Both devices default to **1x** and offer **0.5x**, **1x**, **2x**, and **5x**. For UR5, 1x is **25 mm/s** and caps the tip's linear speed along the arc during RCM tilt. For FlexEndo, 1x is **10°/s** for combined bend and roll. Save the first FlexEndo bend limit with **CALIBRATE**. The applicable speed ceilings apply to jogs, RESET, BIRD-EYE, PULL OUT, horizon correction, and tracking. Rebuild the Flex driver to apply both limits in a single request.

The UR5 relay plans each held jog step from its last nearby servo target, using the same interval for planning and dispatch. Linear jogs use 32 ms and RCM tip jogs use 48 ms. UR5 RESET requests a faster joint profile within the selected linear speed limit. Reachable speed depends on the pose and the joint and controller limits. Flex manual jog and RESET request up to about 99°/s for straight roll and 47°/s per bend coordinate within the configured combined ceiling. With Flex bent, coordinated roll also uses tendon travel, which can lower the reachable roll speed further.

1. **Bird-eye.** With fresh UR5 joints and the scope calibrated, click **BIRD-EYE** and check the live view pitches 30° below horizontal. The app reports when the target exceeds the available bend or roll.
2. **Pull Out.** On the first launch, enter the bench-verified distance in metres from the RCM to the final scope tip on the withdrawal side. The launcher saves it in `config.local.pull-out-clearance-m` at the repository root for the default robot. Press Enter to configure the distance later in the WebUI. The **Pull Out clearance (mm)** field beside **PULL OUT** sets that endpoint for the current session when you click **APPLY**. Update the local file to change the saved launch setting. `-PullOutClearanceM` with a numeric value overrides the file for one session. With `-RobotIp`, enter the clearance for that setup when prompted or pass it as a flag. Calibrate Flex and capture **RCM** at the intended pivot. **PULL OUT** aligns the UR5 to the captured RCM axis at the current insertion depth while Flex returns to zero. It skips either reset when feedback confirms it is already complete. Encoder mode retracts automatically once both resets complete. In planned mode, inspect straightness and clearance before clicking **Confirm straight and retract**. Check withdrawal direction and the endpoint. Use **INTERRUPT** to cancel the sequence and keep the session open.
3. **Tracking.** Verify RCM motion holds the pivot. Select one YOLO category and enable **Tracking** with the object already in frame. The motion status shows **Tracking** throughout the visual-servo session, including when the target is centered. Flex levels the horizon before steering, and the UR5 assists within the configured RCM cone when the correction preserves the horizon and captured pivot. Tracking pauses when the remaining travel cannot keep the camera level. Outside the cone, recovery steps must bring the arm closer to the cone without adding camera tilt.

The full-app planned mode uses the controller's commanded Flex positions for the angle readouts, Bird-eye and tracking decisions. Compare the reported motion with the scope and live camera during bench testing. If the live image has a consistent mounting offset, pass the measured optical roll in degrees with `-CameraRollOffsetDeg`. The angle is the rotation of the optical frame about the modeled forward axis. See [the integration plan](src/flex_endo_robo/PLAN.md) for the full bench checks.

## SCOPE

The ***SCOPE - Surgical Cognition Odometry & Perception Engine***, is the brain of the whole system.
