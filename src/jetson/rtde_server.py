#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jetson AGX Orin RTDE relay
- TCP Server : 192.168.55.1:9000  (Windows PC client)
- RTDE client: 192.168.1.10:30004 (UR5)

Protocol (JSON Lines, one message per '\\n'):
  W->J  {"type":"move_joint","positions":[6 floats],"speed":f,"accel":f}
  W->J  {"type":"move_joint_vel","velocities":[6 floats],"accel":f,"duration":f}
  W->J  {"type":"stop"}   # soft: stopScript
  W->J  {"type":"estop"}  # hard: dashboard "stop"
  J->W  {"type":"robot_state","joints":[...],"tcp_pose":[...],"timestamp":f}
"""
import socket, json, threading, time, logging, sys

try:
    from rtde_control import RTDEControlInterface as RTDEControl
    from rtde_receive import RTDEReceiveInterface as RTDEReceive
except ImportError:
    logging.error("pip install ur-rtde")
    sys.exit(1)

UR5_IP              = "192.168.1.10"
TCP_HOST            = "0.0.0.0"
TCP_PORT            = 9000
UR_DASHBOARD_PORT   = 29999
STATE_PUSH_HZ       = 30.0

logging.basicConfig(level=logging.INFO, format='[Jetson] %(asctime)s - %(message)s')
log = logging.getLogger()

# ---------------- RTDE ----------------
try:
    rtde_c = RTDEControl(UR5_IP)
    rtde_r = RTDEReceive(UR5_IP)
    log.info(f"RTDE connected to UR5 {UR5_IP}")
except Exception as e:
    log.error(f"RTDE connect failed: {e}")
    sys.exit(1)

# ---------------- Dashboard helper ----------------
def dashboard_stop():
    """Hard stop via dashboard server (immediate)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect((UR5_IP, UR_DASHBOARD_PORT))
        s.sendall(b"stop\n")
        try: s.recv(1024)
        except Exception: pass
        s.close()
        log.warning("Dashboard STOP sent (hard)")
    except Exception as e:
        log.error(f"Dashboard STOP failed: {e}")

# ---------------- Client / command handling ----------------
client_lock = threading.Lock()
client_conn = None

def send_json(conn, obj):
    try:
        conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))
    except Exception as e:
        log.debug(f"send failed: {e}")

def process_command(cmd):
    t = cmd.get("type")
    if t == "move_joint":
        q = cmd.get("positions")
        if not (isinstance(q, list) and len(q) == 6):
            log.warning(f"move_joint with bad positions: {q}"); return
        speed = float(cmd.get("speed", 0.5))
        accel = float(cmd.get("accel", 0.5))
        try:
            rtde_c.moveJ([float(x) for x in q], speed, accel, True)   # async
        except Exception as e:
            log.warning(f"moveJ failed: {e}")
    elif t == "move_joint_vel":
        qd = cmd.get("velocities")
        if not (isinstance(qd, list) and len(qd) == 6):
            log.warning(f"move_joint_vel with bad velocities: {qd}"); return
        accel = float(cmd.get("accel", 3.0))
        duration = float(cmd.get("duration", 0.1))
        try:
            rtde_c.speedJ([float(x) for x in qd], accel, duration)
        except Exception as e:
            log.warning(f"speedJ failed: {e}")
    elif t == "stop":
        try: rtde_c.stopScript()
        except Exception as e: log.warning(f"stopScript failed: {e}")
    elif t == "estop":
        dashboard_stop()
    else:
        log.warning(f"unknown command {t}")

def handle_client(conn, addr):
    global client_conn
    log.info(f"Windows connected from {addr}")
    buf = b""
    try:
        while True:
            data = conn.recv(4096)
            if not data: break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip(): continue
                try:
                    process_command(json.loads(line.decode("utf-8")))
                except json.JSONDecodeError as e:
                    log.warning(f"bad JSON: {e}")
                except Exception as e:
                    log.error(f"cmd error: {e}")
    except ConnectionResetError:
        pass
    finally:
        log.info(f"Windows disconnected {addr}")
        with client_lock:
            if client_conn is conn: client_conn = None
        conn.close()

# ---------------- State push ----------------
def state_push_loop():
    period = 1.0 / STATE_PUSH_HZ
    while True:
        time.sleep(period)
        with client_lock:
            conn = client_conn
        if conn is None: continue
        try:
            joints   = rtde_r.getActualQ()
            tcp_pose = rtde_r.getActualTCPPose()
            send_json(conn, {
                "type": "robot_state",
                "joints":   [round(float(v), 6) for v in joints],
                "tcp_pose": [round(float(v), 6) for v in tcp_pose],
                "timestamp": time.time(),
            })
        except Exception as e:
            log.debug(f"state push: {e}")

# ---------------- TCP server ----------------
def tcp_server():
    global client_conn
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((TCP_HOST, TCP_PORT))
    srv.listen(1)
    log.info(f"TCP listening on {TCP_HOST}:{TCP_PORT}")
    while True:
        conn, addr = srv.accept()
        with client_lock:
            if client_conn is not None:
                log.warning("rejecting second client"); conn.close(); continue
            client_conn = conn
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()

if __name__ == "__main__":
    threading.Thread(target=state_push_loop, daemon=True).start()
    try:
        tcp_server()
    except KeyboardInterrupt:
        log.info("shutdown")
        try: rtde_c.stopScript()
        except Exception: pass