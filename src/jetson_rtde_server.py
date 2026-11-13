#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jetson AGX Orin Relay Server
- TCP Server: 0.0.0.0:9000 (Windows PC 连接 192.168.55.1:9000)
- RTDE Client: 连接 UR5 (192.168.1.10:30004)
"""

import socket
import json
import threading
import time
import logging
import sys

# 导入 ur_rtde 库 (pip install ur-rtde)
try:
    from rtde_control import RTDEControlInterface as RTDEControl
    from rtde_receive import RTDEReceiveInterface as RTDEReceive
except ImportError:
    logging.error("请先安装 ur-rtde: pip install ur-rtde")
    sys.exit(1)

# ------------------- 配置区 -------------------
UR5_IP = "192.168.1.10"          # UR5 机器人 IP
TCP_HOST = "0.0.0.0"             # 监听所有地址 (Windows 通过 192.168.55.1 访问)
TCP_PORT = 9000
STATE_PUSH_INTERVAL = 0.05       # 状态回传频率 (秒)，约 20Hz
# ---------------------------------------------

logging.basicConfig(level=logging.INFO, format='[Jetson] %(asctime)s - %(message)s')
logger = logging.getLogger()

# 初始化 RTDE 接口
try:
    rtde_c = RTDEControl(UR5_IP)
    rtde_r = RTDEReceive(UR5_IP)
    logger.info(f"RTDE 已连接至 UR5: {UR5_IP}")
except Exception as e:
    logger.error(f"RTDE 连接失败: {e}")
    sys.exit(1)

# 存储当前客户端连接
client_conn = None
client_lock = threading.Lock()

def push_robot_state():
    """后台线程：周期性采集机器人状态并发送给 Windows"""
    global client_conn
    while True:
        time.sleep(STATE_PUSH_INTERVAL)
        try:
            joints = rtde_r.getActualQ()          # 当前关节角度 (rad)
            tcp_pose = rtde_r.getActualTCPPose()  # 当前 TCP 位姿 [x,y,z,rx,ry,rz]
            payload = {
                "type": "robot_state",
                "joints": [round(v, 6) for v in joints],
                "tcp_pose": [round(v, 6) for v in tcp_pose],
                "timestamp": time.time()
            }
            line = (json.dumps(payload) + "\n").encode('utf-8')
            with client_lock:
                if client_conn:
                    try:
                        client_conn.sendall(line)
                    except (BrokenPipeError, ConnectionResetError):
                        logger.warning("状态发送失败，客户端可能已断开")
                        client_conn = None
        except Exception as e:
            logger.debug(f"读取或推送状态异常: {e}")

def handle_client(conn, addr):
    """处理单个 Windows 客户端的指令流"""
    global client_conn
    logger.info(f"Windows 客户端已连接: {addr}")
    buffer = b""
    try:
        while True:
            data = conn.recv(4096)
            if not data:
                break
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    cmd = json.loads(line.decode('utf-8'))
                    process_command(cmd)
                except json.JSONDecodeError:
                    logger.warning(f"收到非法 JSON: {line}")
                except Exception as e:
                    logger.error(f"处理指令出错: {e}")
    except ConnectionResetError:
        logger.info("Windows 客户端连接重置")
    finally:
        logger.info(f"Windows 客户端断开: {addr}")
        with client_lock:
            if client_conn == conn:
                client_conn = None
        conn.close()

def process_command(cmd):
    """解析并执行来自 Windows 的指令"""
    cmd_type = cmd.get("type")
    if cmd_type == "move_j":
        joints = cmd.get("joints")
        speed = cmd.get("speed", 0.5)
        accel = cmd.get("accel", 0.5)
        if joints and len(joints) == 6:
            logger.info(f"执行 moveJ: {joints}")
            rtde_c.moveJ(joints, speed, accel, False)
    elif cmd_type == "move_l":
        pose = cmd.get("pose")
        speed = cmd.get("speed", 0.25)
        accel = cmd.get("accel", 0.5)
        if pose and len(pose) == 6:
            logger.info(f"执行 moveL: {pose}")
            rtde_c.moveL(pose, speed, accel, False)
    elif cmd_type == "stop":
        logger.info("收到停止指令")
        rtde_c.stopScript()
    else:
        logger.warning(f"未知指令类型: {cmd_type}")

def start_tcp_server():
    """启动 TCP 服务器并接受连接"""
    global client_conn
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((TCP_HOST, TCP_PORT))
    server.listen(1)
    logger.info(f"TCP 服务器已启动，监听 {TCP_HOST}:{TCP_PORT}，等待 Windows 连接...")

    while True:
        conn, addr = server.accept()
        with client_lock:
            if client_conn:
                logger.warning("已有客户端连接，拒绝新连接")
                conn.close()
                continue
            client_conn = conn
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()

if __name__ == "__main__":
    # 启动状态推送线程
    threading.Thread(target=push_robot_state, daemon=True).start()
    # 启动 TCP 服务器 (主线程阻塞)
    try:
        start_tcp_server()
    except KeyboardInterrupt:
        logger.info("Jetson 服务关闭")
        rtde_c.stopScript()