# CRF

## System Architecture

![System Architecture](imgs/sys_architecture.jpg)

### IP Table

| Device | Interface | IP | Role |
|---|---|---|---|
| Windows PC | USB virtual network card | `192.168.55.100` | TCP Client -> Jetson |
| Jetson AGX Orin | `l4tbr0` (usb0) | `192.168.55.1` | TCP Server :9000 |
| Jetson AGX Orin | `end0` (LAN) | `192.168.1.100` | RTDE Client -> UR5 |
| UR5 (PolyScope) | LAN | `192.168.1.10` | RTDE Server :30004 |