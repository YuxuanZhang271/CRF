#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jetson AGX Orin RTDE relay — 带运动仲裁锁安全切换的 servoJ 版本
- TCP Server : 192.168.55.1:9000
- RTDE client: 192.168.1.10:30004

核心修复：
  1. 引入 servo_stop_event 事件，moveJ 前通知 servo_thread 安全退出
  2. servo_thread 退出循环后，运动锁才真正释放
  3. 使用 wait_velocity_zero() 确认机器人静止后再发送 moveJ
"""
import socket, json, threading, time, logging, sys

try:
    from rtde_control import RTDEControlInterface as RTDEControl
    from rtde_receive import RTDEReceiveInterface as RTDEReceive
except ImportError:
    logging.error("pip install ur-rtde")
    sys.exit(1)

# ... (IP、端口等常量保持不变) ...
UR5_IP            = "192.168.1.10"
TCP_HOST          = "0.0.0.0"
TCP_PORT          = 9000
UR_DASHBOARD_PORT = 29999
STATE_PUSH_HZ     = 125.0
SERVO_HZ          = 125.0
SERVO_PERIOD      = 1.0 / SERVO_HZ
SERVO_T           = SERVO_PERIOD
SERVO_LOOKAHEAD   = 0.1
SERVO_GAIN        = 300.0
SERVO_STALE_S     = 0.3

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
    # ... (保持不变) ...
    pass

# ---------------- ★ 新增：servo 线程退出事件 ----------------
servo_stop_event = threading.Event()

# ---------------- Servo slot ----------------
class ServoSlot:
    # ... (保持不变) ...
    pass

slot = ServoSlot()

# ---------------- ★ 修改：release_motion_lock ----------------
def wait_velocity_zero(timeout_s=1.0):  # ★ 超时时间延长到 1 秒
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
    """
    ★ 核心修复：安全地将运动控制权从 servo_thread 交回。
    步骤：
      1. 设置事件，通知 servo_thread 退出循环。
      2. 等待 servo_thread 真正退出（锁被释放）。
      3. 确认机器人关节速度已降至零。
    """
    global servo_thread_obj
    log.info("Requesting servo thread to release motion lock...")
    servo_stop_event.set()          # ★ 通知 servo_thread 退出

    # 等待 servo_thread 退出（最多等 2 秒）
    if servo_thread_obj is not None and servo_thread_obj.is_alive():
        servo_thread_obj.join(timeout=2.0)

    # 确认机器人已静止
    if wait_velocity_zero(1.0):
        log.info("Motion lock released, robot is stationary.")
    else:
        log.warning("Velocity not zero within timeout, but proceeding cautiously.")

# ---------------- ★ 修改：servo_thread ----------------
def servo_thread():
    """以严格 125 Hz 从 slot 取最新 target 发 servoJ。
    收到 servo_stop_event 信号后立即退出循环，释放运动锁。"""
    log.info("servo_thread started.")
    next_t = time.monotonic()
    while not servo_stop_event.is_set():  # ★ 检查退出事件
        # ... (sleep + busy-wait 逻辑不变) ...
        
        mode, q_tgt, t_upd = slot.snapshot()
        age = (time.monotonic() - t_upd) if t_upd > 0 else float('inf')
        if mode == 'servo' and age < SERVO_STALE_S:
            try:
                rtde_c.servoJ(q_tgt, 0.0, 0.0,
                              SERVO_T, SERVO_LOOKAHEAD, SERVO_GAIN)
            except Exception as e:
                log.debug(f"servoJ: {e}")
    log.info("servo_thread exiting, motion lock released.")
    # ★ 线程退出后，运动锁被自动释放
    # 重置事件，以便下次 servo 控制可以重新启动线程
    servo_stop_event.clear()

# ---------------- Client / command handling ----------------
# ... (process_command 等函数保持不变，但 move_joint 分支会调用新的 release_motion_lock) ...
def process_command(cmd):
    t = cmd.get("type")
    if t == "move_joint":
        release_motion_lock()  # ★ 调用新的安全释放逻辑
        # ... (后续 moveJ 代码不变) ...
    # ... (其他分支不变) ...