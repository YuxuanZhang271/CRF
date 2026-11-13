#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jetson AGX Orin RTDE relay (fixed)
- TCP Server : 192.168.55.1:9000  (Windows PC client)
- RTDE client: 192.168.1.10:30004 (UR5)

修复点：
  1. move_joint 前调用 stopJ() 释放运动锁，避免 "Another thread is already controlling the robot"
  2. stop 命令改为 stopJ()，不再误杀 External Control 程序
  3. speedJ duration 下限提高到 0.3s，防止看门狗超时
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

# ---------------- 释放运动锁的辅助函数 ----------------
def release_motion_lock():
    """
    如果当前有 speedJ 线程在控制机器人，先停止它并等待控制器释放运动锁。
    stopJ 是同步的，返回后运动锁即被释放，后续 moveJ 不会冲突。
    """
    try:
        rtde_c.stopJ(3.0)          # 3 rad/s² 减速度，立即停止关节运动
        time.sleep(0.05)            # 给控制器一个周期释放锁
        log.debug("Motion lock released via stopJ()")
    except Exception as e:
        log.warning(f"stopJ() failed: {e}")

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
        # ★ 关键修复：在 moveJ 之前释放速度控制的运动锁
        release_motion_lock()

        q = cmd.get("positions")
        if not (isinstance(q, list) and len(q) == 6):
            log.warning(f"move_joint with bad positions: {q}"); return
        speed = float(cmd.get("speed", 0.5))
        accel = float(cmd.get("accel", 0.5))
        try:
            rtde_c.moveJ([float(x) for x in q], speed, accel, True)   # async
            log.info("moveJ dispatched after motion-lock release")
        except Exception as e:
            log.warning(f"moveJ failed: {e}")

    elif t == "move_joint_vel":
        qd = cmd.get("velocities")
        if not (isinstance(qd, list) and len(qd) == 6):
            log.warning(f"move_joint_vel with bad velocities: {qd}"); return
        accel = float(cmd.get("accel", 3.0))
        # ★ 提高 duration 下限，防止看门狗超时
        duration = max(float(cmd.get("duration", 0.1)), 0.3)
        try:
            rtde_c.speedJ([float(x) for x in qd], accel, duration)
        except Exception as e:
            log.warning(f"speedJ failed: {e}")

    elif t == "stop":
        # ★ 只停运动，不杀 External Control 程序
        try:
            rtde_c.stopJ(3.0)
            log.warning("stopJ sent (soft stop)")
        except Exception as e:
            log.warning(f"stopJ failed: {e}")

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
        try: rtde_c.stopJ(3.0)
        except Exception: pass