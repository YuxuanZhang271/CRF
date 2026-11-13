#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jetson AGX Orin RTDE relay — servoJ 位置伺服版本
- TCP Server : 192.168.55.1:9000
- RTDE client: 192.168.1.10:30004

协议：
  W->J {"type":"servo_joint","positions":[6 floats]}        # 125 Hz 位置伺服
  W->J {"type":"move_joint","positions":[6 floats],"speed":f,"accel":f}
  W->J {"type":"stop"}                                     # stopJ + 清空 servo slot
  W->J {"type":"estop"}
  J->W {"type":"robot_state","joints":[...],"tcp_pose":[...],"timestamp":f}

核心修改：
  1. handle_client 收到 servo_joint 只更新共享 slot（非阻塞）
  2. 独立 servo_thread 以 125 Hz 读取最新 target 并调用 servoJ(8ms)
  3. speedJ 阻塞问题彻底消除：末端轨迹每 8 ms 基于最新 q_now 更新
"""
import socket, json, threading, time, logging, sys

try:
    from rtde_control import RTDEControlInterface as RTDEControl
    from rtde_receive import RTDEReceiveInterface as RTDEReceive
except ImportError:
    logging.error("pip install ur-rtde")
    sys.exit(1)

UR5_IP            = "192.168.1.10"
TCP_HOST          = "0.0.0.0"
TCP_PORT          = 9000
UR_DASHBOARD_PORT = 29999
STATE_PUSH_HZ     = 125.0
SERVO_HZ          = 125.0
SERVO_PERIOD      = 1.0 / SERVO_HZ          # 8 ms
SERVO_T           = SERVO_PERIOD            # servoJ 的 time 参数
SERVO_LOOKAHEAD   = 0.1                     # 平滑度 vs 响应（0.03~0.2）
SERVO_GAIN        = 300.0                   # 跟踪增益（100~2000）
SERVO_STALE_S     = 0.3                     # slot 超过这个时间未更新则视为 idle

logging.basicConfig(level=logging.INFO, format='[Jetson] %(asctime)s - %(message)s')
log = logging.getLogger()

try:
    rtde_c = RTDEControl(UR5_IP)
    rtde_r = RTDEReceive(UR5_IP)
    log.info(f"RTDE connected to UR5 {UR5_IP}")
except Exception as e:
    log.error(f"RTDE connect failed: {e}")
    sys.exit(1)

# ---------------- Dashboard helper ----------------
def dashboard_stop():
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

# ---------------- Servo slot (latest-value semantics) ----------------
class ServoSlot:
    """线程安全的"最新指令"存储。handle_client 只写，servo_thread 只读。"""
    def __init__(self):
        self._lock = threading.Lock()
        self._mode = 'idle'            # 'idle' | 'servo'
        self._q_target = [0.0]*6
        self._t_update = 0.0

    def set_servo(self, q):
        with self._lock:
            self._q_target = list(q)
            self._mode = 'servo'
            self._t_update = time.monotonic()

    def clear(self):
        with self._lock:
            self._mode = 'idle'

    def snapshot(self):
        with self._lock:
            return self._mode, list(self._q_target), self._t_update

slot = ServoSlot()

# ---------------- Motion-lock helper ----------------
def wait_velocity_zero(timeout_s=0.5):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            qd = rtde_r.getActualQd()
            if all(abs(v) < 1e-3 for v in qd):
                return True
        except Exception:
            pass
        time.sleep(0.005)
    return False

def release_motion_lock():
    try:
        rtde_c.stopJ(3.0)
        wait_velocity_zero(0.5)
    except Exception as e:
        log.warning(f"stopJ() failed: {e}")

# ---------------- Servo thread @ 125 Hz ----------------
def servo_thread():
    """以严格 125 Hz 从 slot 取最新 target 发 servoJ。
    sleep + busy-wait 补偿 Windows/Jetson 抖动。"""
    next_t = time.monotonic()
    while True:
        now = time.monotonic()
        if now < next_t:
            dt_sleep = next_t - now
            if dt_sleep > 0.002:
                time.sleep(dt_sleep - 0.001)
            while time.monotonic() < next_t:
                pass
        next_t += SERVO_PERIOD
        if next_t < time.monotonic():
            next_t = time.monotonic()

        mode, q_tgt, t_upd = slot.snapshot()
        age = (time.monotonic() - t_upd) if t_upd > 0 else float('inf')
        if mode == 'servo' and age < SERVO_STALE_S:
            try:
                rtde_c.servoJ(q_tgt, 0.0, 0.0,
                              SERVO_T, SERVO_LOOKAHEAD, SERVO_GAIN)
            except Exception as e:
                log.debug(f"servoJ: {e}")

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

    if t == "servo_joint":
        q = cmd.get("positions")
        if not (isinstance(q, list) and len(q) == 6):
            log.warning(f"servo_joint bad positions"); return
        slot.set_servo([float(x) for x in q])

    elif t == "move_joint":
        # 低频整段轨迹：直接执行，先释放运动锁
        release_motion_lock()
        slot.clear()
        q = cmd.get("positions")
        if not (isinstance(q, list) and len(q) == 6):
            log.warning(f"move_joint bad positions"); return
        speed = float(cmd.get("speed", 0.5))
        accel = float(cmd.get("accel", 0.5))
        try:
            rtde_c.moveJ([float(x) for x in q], speed, accel, True)
            log.info("moveJ dispatched")
        except Exception as e:
            log.warning(f"moveJ failed: {e}")

    elif t == "move_joint_vel":
        # 兼容旧接口：把速度积分成一次目标位置，走 servo（防止长时间漂移）
        # 通常 control_node 已改成发送 servo_joint，这条路径基本不会再触发
        qd = cmd.get("velocities")
        if not (isinstance(qd, list) and len(qd) == 6):
            return
        try:
            q_now = rtde_r.getActualQ()
            dt = max(float(cmd.get("duration", 0.008)), 0.008)
            q_tgt = [float(q_now[i] + qd[i] * dt) for i in range(6)]
            slot.set_servo(q_tgt)
        except Exception as e:
            log.warning(f"move_joint_vel fallback failed: {e}")

    elif t == "stop":
        slot.clear()
        try:
            rtde_c.stopJ(3.0)
            log.warning("stopJ sent (soft stop)")
        except Exception as e:
            log.warning(f"stopJ failed: {e}")

    elif t == "estop":
        slot.clear()
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
        slot.clear()
        with client_lock:
            if client_conn is conn: client_conn = None
        conn.close()

# ---------------- State push @ 125 Hz ----------------
def state_push_loop():
    while True:
        t0 = time.monotonic()
        with client_lock:
            conn = client_conn
        if conn is not None:
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
        elapsed = time.monotonic() - t0
        sleep_t = (1.0/STATE_PUSH_HZ) - elapsed
        if sleep_t > 0:
            time.sleep(sleep_t)

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
    threading.Thread(target=servo_thread,     daemon=True, name="servo").start()
    threading.Thread(target=state_push_loop,  daemon=True, name="state").start()
    try:
        tcp_server()
    except KeyboardInterrupt:
        log.info("shutdown")
        slot.clear()
        try: rtde_c.stopJ(3.0)
        except Exception: pass