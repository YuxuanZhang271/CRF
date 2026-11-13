#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows Control Panel & TCP Client
- Web UI: http://localhost:8080 (浏览器打开)
- TCP Client: 连接 Jetson 192.168.55.1:9000
"""

import socket
import json
import threading
import time
import logging
from http.server import HTTPServer, SimpleHTTPRequestHandler
import urllib.parse

# ------------------- 配置区 -------------------
JETSON_IP = "192.168.55.1"
JETSON_PORT = 9000
WEB_PORT = 8080
# ---------------------------------------------

logging.basicConfig(level=logging.INFO, format='[Windows] %(asctime)s - %(message)s')
logger = logging.getLogger()

# 全局状态
latest_state = {"joints": [0.0]*6, "tcp_pose": [0.0]*6, "timestamp": 0}
state_lock = threading.Lock()
tcp_sock = None
sock_lock = threading.Lock()

def tcp_client_thread():
    """后台线程：维护与 Jetson 的 TCP 连接，持续接收状态"""
    global tcp_sock
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((JETSON_IP, JETSON_PORT))
            logger.info(f"已连接 Jetson TCP 服务器 {JETSON_IP}:{JETSON_PORT}")
            with sock_lock:
                tcp_sock = sock
            buffer = b""
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line.decode('utf-8'))
                        if msg.get("type") == "robot_state":
                            with state_lock:
                                latest_state.update(msg)
                    except json.JSONDecodeError:
                        pass
        except (ConnectionRefusedError, ConnectionResetError, OSError):
            logger.warning("Jetson 连接断开，3 秒后重试...")
            with sock_lock:
                tcp_sock = None
            time.sleep(3)

def send_to_jetson(cmd_dict):
    """将控制指令发送给 Jetson"""
    with sock_lock:
        if tcp_sock is None:
            logger.warning("Jetson 未连接，无法发送指令")
            return False
        try:
            line = (json.dumps(cmd_dict) + "\n").encode('utf-8')
            tcp_sock.sendall(line)
            return True
        except Exception as e:
            logger.error(f"发送指令失败: {e}")
            return False

# ---------- 简易 Web 控制面板 (HTML) ----------
HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>UR5 Control Panel</title>
    <style>
        body { font-family: Arial; background: #1e1e2f; color: #eee; padding: 20px; }
        .container { display: flex; gap: 40px; flex-wrap: wrap; }
        .card { background: #2a2a40; border-radius: 10px; padding: 20px; width: 400px; }
        h3 { margin-top: 0; color: #ffb86c; }
        input[type=number] { width: 80px; margin: 2px; background: #3a3a50; color: #fff; border: 1px solid #555; border-radius: 4px; padding: 4px; }
        button { background: #6272a4; color: white; border: none; padding: 8px 16px; border-radius: 5px; cursor: pointer; margin: 5px; }
        button:hover { background: #7b8bc4; }
        .status { font-size: 0.9em; color: #50fa7b; }
        pre { background: #1a1a2a; padding: 10px; border-radius: 5px; overflow-x: auto; }
    </style>
</head>
<body>
    <h2>UR5 Control Panel (via Jetson)</h2>
    <div class="container">
        <div class="card">
            <h3>Move J (Joint)</h3>
            <div id="jointInputs"></div>
            <button onclick="sendMoveJ()">Execute moveJ</button>
        </div>
        <div class="card">
            <h3>Move L (Cartesian Pose)</h3>
            <div id="poseInputs"></div>
            <button onclick="sendMoveL()">Execute moveL</button>
        </div>
        <div class="card">
            <h3>Robot State (实时)</h3>
            <pre id="stateDisplay">Waiting for data...</pre>
            <button onclick="sendStop()" style="background:#ff5555;">STOP</button>
        </div>
    </div>

    <script>
        // 动态生成输入框
        const jointDiv = document.getElementById('jointInputs');
        for (let i = 0; i < 6; i++) {
            jointDiv.innerHTML += `<label>q${i+1}(rad): </label><input type="number" id="q${i}" step="0.01" value="0"><br>`;
        }
        const poseDiv = document.getElementById('poseInputs');
        const poseLabels = ['x(m)','y(m)','z(m)','rx(rad)','ry(rad)','rz(rad)'];
        for (let i = 0; i < 6; i++) {
            poseDiv.innerHTML += `<label>${poseLabels[i]}: </label><input type="number" id="p${i}" step="0.01" value="0"><br>`;
        }

        async function sendCmd(cmd) {
            await fetch('/api/command', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(cmd)
            });
        }

        function sendMoveJ() {
            const joints = [];
            for (let i = 0; i < 6; i++) joints.push(parseFloat(document.getElementById('q'+i).value) || 0);
            sendCmd({type: "move_j", joints: joints, speed: 0.5, accel: 0.5});
        }
        function sendMoveL() {
            const pose = [];
            for (let i = 0; i < 6; i++) pose.push(parseFloat(document.getElementById('p'+i).value) || 0);
            sendCmd({type: "move_l", pose: pose, speed: 0.25, accel: 0.5});
        }
        function sendStop() {
            sendCmd({type: "stop"});
        }

        // 轮询获取机器人状态
        setInterval(async () => {
            try {
                const res = await fetch('/api/state');
                const data = await res.json();
                document.getElementById('stateDisplay').textContent =
                    "Joints: " + JSON.stringify(data.joints) + "\\n" +
                    "TCP Pose: " + JSON.stringify(data.tcp_pose) + "\\n" +
                    "Timestamp: " + data.timestamp;
            } catch(e) {}
        }, 200);
    </script>
</body>
</html>
"""

class RequestHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode('utf-8'))
        elif self.path == '/api/state':
            with state_lock:
                body = json.dumps(latest_state).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == '/api/command':
            content_len = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_len)
            try:
                cmd = json.loads(post_data.decode('utf-8'))
                send_to_jetson(cmd)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(e).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # 屏蔽 HTTP 请求日志

if __name__ == '__main__':
    # 启动 TCP 客户端线程
    threading.Thread(target=tcp_client_thread, daemon=True).start()
    # 启动 Web 服务器
    server = HTTPServer(('0.0.0.0', WEB_PORT), RequestHandler)
    logger.info(f"Web 控制面板已启动: http://localhost:{WEB_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Windows 服务关闭")