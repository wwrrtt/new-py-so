#!/usr/bin/env python3
"""test.py — Python + libsingbox.so

关键修复：全部使用 c_void_p 处理 char* 返回值，
避免 c_char_p 自动转换导致 C 指针被污染。
"""

import sys, os, json, signal, ctypes, urllib.request, tempfile, threading

WEB_PORT       = int(os.environ.get("WEB_PORT",       "5000"))

SO_URL = "https://github.com/lostwwrrtt/sbso/releases/download/v1.0/server.so"


# ═══════════════════════════════════════════════
# SingBox 封装
# ═══════════════════════════════════════════════

class SingBoxError(Exception):
    pass

class SingBox:
    def __init__(self, lib):
        self._lib = lib
        self._lock = threading.Lock()

        # ─── 全部用 c_void_p，不用 c_char_p ───
        self._lib.singbox_version.restype = ctypes.c_void_p

        self._lib.singbox_start.argtypes = [ctypes.c_char_p]
        self._lib.singbox_start.restype  = ctypes.c_void_p

        self._lib.singbox_stop.restype = ctypes.c_void_p

        self._lib.singbox_is_running.restype = ctypes.c_int

        self._lib.singbox_check_config.argtypes = [ctypes.c_char_p]
        self._lib.singbox_check_config.restype  = ctypes.c_void_p

        self._lib.singbox_format_config.argtypes = [ctypes.c_char_p]
        self._lib.singbox_format_config.restype  = ctypes.c_void_p

        self._lib.singbox_free_string.argtypes = [ctypes.c_void_p]
        self._lib.singbox_free_string.restype  = None

    # ── 内部工具 ──

    def _read_c_string(self, ptr):
        """从 c_void_p 安全读取字符串，读完不释放（调用者决定是否 free）"""
        if ptr is None or ptr == 0:
            return None
        return ctypes.string_at(ptr).decode("utf-8", errors="replace")

    def _check_null(self, ptr):
        """NULL (0 或 None) = 成功, 非 NULL = 错误 → 读取 + free + 抛异常"""
        if ptr is None or ptr == 0:
            return
        msg = self._read_c_string(ptr)
        self._lib.singbox_free_string(ptr)
        raise SingBoxError(msg)

    def _take_string(self, ptr):
        """读取字符串，读取后自动 free"""
        if ptr is None or ptr == 0:
            return None
        s = self._read_c_string(ptr)
        self._lib.singbox_free_string(ptr)
        return s

    # ── 公开 API ──

    def version(self):
        return self._take_string(self._lib.singbox_version())

    def start(self, config):
        if isinstance(config, dict):
            config = json.dumps(config, ensure_ascii=False)
        with self._lock:
            ptr = self._lib.singbox_start(config.encode("utf-8"))
            self._check_null(ptr)

    def stop(self):
        with self._lock:
            ptr = self._lib.singbox_stop()
            self._check_null(ptr)

    @property
    def running(self):
        return bool(self._lib.singbox_is_running())

    def check_config(self, config):
        if isinstance(config, dict):
            config = json.dumps(config, ensure_ascii=False)
        ptr = self._lib.singbox_check_config(config.encode("utf-8"))
        self._check_null(ptr)

    def format_config(self, config):
        if isinstance(config, dict):
            config = json.dumps(config, ensure_ascii=False)
        return self._take_string(self._lib.singbox_format_config(config.encode("utf-8")))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop()


# ═══ 配置 ══════════════════════════════════

CF_TOKEN       = os.environ.get("CF_TOKEN",       "xxx")
VMESS_UUID     = os.environ.get("VMESS_UUID",     "ee1feada-4e2f-4dc3-aaa6-f97aeed0286b")
VMESS_PORT     = int(os.environ.get("VMESS_PORT",     "44344"))
VMESS_PATH     = os.environ.get("VMESS_PATH",     "/ray10086")
HA_CONNECTIONS = int(os.environ.get("HA_CONNECTIONS", "0"))

config = {
    "log": {"disabled": True},
    "inbounds": [
        {"type": "cloudflared", "tag": "cf-tunnel-in",
         "token": CF_TOKEN,
         "protocol": "quic",
         "ha_connections": HA_CONNECTIONS,
         "edge_ip_version": 0,
         "grace_period": "30s"},
        {"type": "vmess", "tag": "vmess-ws-in",
         "listen": "0.0.0.0",
         "listen_port": VMESS_PORT,
         "users": [{"uuid": VMESS_UUID, "alterId": 0}],
         "transport": {"type": "ws", "path": VMESS_PATH}},
    ],
    "outbounds": [
        {"type": "direct", "tag": "direct-out"}
    ],
}


# ═══ Web 服务 ═════════════════════════════

INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.html")

from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if os.path.isfile(INDEX_PATH):
            with open(INDEX_PATH, "rb") as f:
                body = f.read()
            ct = "text/html; charset=utf-8"
        else:
            body = b"hello world"
            ct = "text/plain; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


# ═══ 主入口 ═══════════════════════════════

_web_server = None
stop_event = threading.Event()

def handle_signal(sig, _frame):
    print(f"\n[main] 收到信号 {sig}，stopping...")
    stop_event.set()
    if _web_server:
        threading.Thread(target=_web_server.shutdown, daemon=True).start()

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT,  handle_signal)


def main():
    global _web_server

    print(f"[main] getting server")
    fd, so_path = tempfile.mkstemp(suffix=".so", prefix="libsingbox_")
    os.close(fd)
    urllib.request.urlretrieve(SO_URL, so_path)
    print("[main] ✅ got it")

    print("[main] loading server...")
    lib = ctypes.cdll.LoadLibrary(so_path)
    os.unlink(so_path)
    sb = SingBox(lib)
    print(f"[main] ✅ Loaded successfully.  (version={sb.version()})")

    print("[main] Check configuration...")
    try:
        sb.check_config(config)
        print("[main] ✅ Configuration is right.")
    except SingBoxError as e:
        print(f"[main] ❌ Configuration is bad.: {e}")
        return

    print("[main] starting...")
    try:
        sb.start(config)
        print(f"[main] ✅ server is running (running={sb.running})")
    except SingBoxError as e:
        print(f"[main] ❌ Startup failed: {e}")
        return

    _web_server = HTTPServer(("0.0.0.0", WEB_PORT), Handler)
    web_thread = threading.Thread(target=lambda s: s.serve_forever(), args=(_web_server,), daemon=True)
    web_thread.start()
    print(f"[web] ✅ 监听 http://0.0.0.0:{WEB_PORT}")

    try:
        stop_event.wait()
    except Exception as e:
        print(f"[main] 异常: {e}")

    print("[main] stop server...")
    try:
        sb.stop()
    except SingBoxError as e:
        print(f"[main] ⚠ stop warning: {e}")
    print("[main] ✅ all server stopped")


if __name__ == "__main__":
    main()
