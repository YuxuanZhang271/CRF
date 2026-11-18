| Index | Function | Keyboard Input | Description |
|---|---|---|---|
| 1 | UR5 linear/tip move up | UP |  |
| 2 | UR5 linear/tip move down | DOWN |  |
| 3 | UR5 linear/tip move left | LEFT |  |
| 4 | UR5 linear/tip move right | RIGHT |  |
| 5 | UR5 linear/tip move forward | ] |  |
| 6 | UR5 linear/tip move backward | [ |  |
| 7 | FlexEndo look up | W |  |
| 8 | FlexEndo look down | S |  |
| 9 | FlexEndo look left | A |  |
| 10 | FlexEndo look right | D |  |
| 11 | FlexEndo roll clockwise (+) | R |  |
| 12 | FlexEndo roll counterclockwise (-) | F |  |
| 13 | BIRD-EYE | `ctrl` + B | Turn yellow when moving for bird-eye view, turn green when at bird-eye view position (no matter pressed bird-eye view or not) |
| 14 | PULL-OUT | `ctrl` + P | 1. Current pull out will have an extra button confirming the UR5 tip and flex at zero position, remove that button, use color to indicate status: yellow when moving, sky blue when zero position; press pull out again for confirming; yellow again when pulling out, green when pull out completed <br> 2. keyboard shortcut the same, press for first time get to zero position, press again for pulling out |
| 15 | INTERRUPT | I |  |
| 16 | TRACKING | `ctrl` + T | Green when tracking on, yellow when object cannot reach, sky blue when waiting for object into the view |
| 17 | RECORD | `ctrl` + R | Green when recording. Add an index for slices and a timer for current slice length |

## Usage notes

- Focus the control dashboard. Hold motion keys to jog; release to stop. Leaving
  the page or focusing another app releases held motion. Form editing suspends
  shortcuts. Command auto-repeat is ignored.
- On Windows, the app forwards the four Ctrl combinations around browser default
  actions while the dashboard is focused. Outside the dashboard, normal browser
  and application shortcuts continue to work.
- Press PULL OUT / Ctrl+P once to prepare. Sky blue means ready at zero. Verify
  scope straightness and the withdrawal path, then press the same command again
  within 30 seconds. This applies to planned and encoder feedback.
- R/F control roll on physical Flex. In arm-only modeled mode they do not change
  arc length; use the modeled arc buttons with the pointer.
- The recording slice index matches the directory name (starting `00000`), and
  the timer resets at each five-minute boundary.
