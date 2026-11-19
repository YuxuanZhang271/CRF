| Index | Task | Description |
|---|---|---|
| 0 | stop | The hightest priority. The same function as INTERRUPT. |
| 1 | go forward | Continuously go forward for UR5. |
| 2 | go backward | Continuously go backward for UR5 |
| 3 | zoom in | Continuously digital zoom in for camera view. |
| 4 | zoom out | Continuously digital zoom out for camera view. |
| 5 | look up | Continuously look up, <br> 1. first Flex, <br> 2. if Flex reached edge, then UR5, <br> 3. If both reached edge, do not move and warn in console. |
| 6 | look down | Continuously look down, the same strategy as 5 - look up. |
| 7 | look left | Continuously look left, the same strategy as 5 - look up. |
| 8 | look right | Continuously look right, the same strategy as 5 - look up. |
| 9 | rotate left | Continuously roll camera view anticlockwise (roll -), if reached edge, do not move and warn in console. |
| 10 | rotate right | Continuously roll camera view clockwise (roll +), if reached edge, do not move and warn in console. |
| 11 | bird-eye view | Move to the existing 30° downward, level camera view. Fall back to 0 - stop after finishing. |
| 12 | reset | In active RCM mode, return UR5 tip tilt to the captured RCM reference axis while preserving the pivot and current insertion depth. Keep RCM mode and reset FlexEndo bend-roll to saved zero. Fall back to 0 - stop after both devices settle. |
| 13 | clean lens | CLEAN LENS. Fall back to 0 - stop after finishing. |
| 14 | spray water | Spray water for 3 seconds, then stop. Fall back to 0 - stop after the driver confirms the output stopped. |
| 15 | spray air | Spray air for 3 seconds, then stop. Fall back to 0 - stop after the driver confirms the output stopped. |
| 16 | tracking | Track the selected YOLO category using the existing Flex-first, UR5-assist controller. Requires calibrated Flex, active UR5 RCM, and fresh camera observations. |
