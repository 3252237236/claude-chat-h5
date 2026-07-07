#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude 聊天 H5 - 生产服务器
================================
一键启动:  python server.py
部署上线:  设置环境变量 API_KEY=sk-xxx 后启动

安全设计:  API Key 仅存服务端，绝不传到浏览器。
前端通过 /api/chat 调用，由服务端添加 Key 后转发。
"""

import json
import os
import sys
import gzip
import io
import traceback
import logging
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """多线程 HTTP 服务器，Docker 环境下更稳定"""
    daemon_threads = True
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from pathlib import Path

# 立即输出日志（方便 Railway 调试）
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")

# ==================== 配置 ====================
PORT = int(os.environ.get("PORT", 8765))
HOST = os.environ.get("HOST", "0.0.0.0")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", 4096))
TIMEOUT = int(os.environ.get("TIMEOUT", 180))

# ==================== API 平台管理（从 providers.json 加载） ====================
# 编辑 providers.json 即可添加任意平台，无需改代码。
# Key 优先级：providers.json 中的 key > 环境变量 > .claude_key 文件

def _load_providers():
    """从 providers.json 加载平台配置，不存在则回退到 providers.example.json"""
    base = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(base, "providers.json"),
        "providers.json",
        os.path.join(base, "providers.example.json"),
        "providers.example.json",
    ]
    for p in paths:
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    providers = data.get("providers", [])
                    print(f"  ✓ 加载平台配置: {len(providers)} 个 ({os.path.basename(p)})")
                    return providers
        except Exception as e:
            print(f"  ✗ 解析失败 ({p}): {e}")
    print("  ✗ 未找到 providers.json 或 providers.example.json")
    return []

def _load_key_file(filename):
    paths = [filename, os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)]
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.readline().strip()
        except Exception:
            pass
    return ""

GENERIC_KEY = os.environ.get("API_KEY", "") or _load_key_file(".claude_key")

# 加载所有平台
ALL_PROVIDERS = _load_providers()

# 为每个平台解析 Key（json 里的 key > 环境变量 > 通用 Key）
for p in ALL_PROVIDERS:
    json_key = p.get("key", "")
    env_key = os.environ.get(p.get("env", ""), "")
    p["_resolved_key"] = json_key or env_key or GENERIC_KEY

def get_available_providers():
    """返回所有已配置 Key 的平台"""
    return [{
        "id": p.get("id", p["name"].lower().replace(" ", "-")),
        "name": p["name"],
        "url": p["url"],
        "model": p["model"],
    } for p in ALL_PROVIDERS if p.get("_resolved_key")]

def get_key_for_url(url):
    """根据 API URL 找到匹配的 Key"""
    for p in ALL_PROVIDERS:
        if p["url"] == url and p.get("_resolved_key"):
            return p["_resolved_key"]
    return GENERIC_KEY

def has_any_key():
    return bool(get_available_providers())

# ==================== 内容类型映射 ====================
MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
}

STATIC_DIR = os.path.dirname(os.path.abspath(__file__))


class AppHandler(SimpleHTTPRequestHandler):
    """生产级请求处理器"""

    # ---------- 路由 ----------
    def do_GET(self):
        path = self.path.split("?")[0]  # 去参数
        if path == "/":
            return self.serve_static("/index.html")
        if path == "/api/config":
            return self.handle_config()
        if path == "/api/health":
            return self.handle_health()
        return self.serve_static(path)

    def do_POST(self):
        if self.path == "/api/chat":
            return self.handle_chat()
        self.send_error(404, "Not Found")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    # ---------- API 端点 ----------
    def handle_config(self):
        """告诉前端哪些平台可用（但不泄露 Key）"""
        self.send_json({
            "hasKey": has_any_key(),
            "providers": get_available_providers(),
        })

    def handle_health(self):
        self.send_json({"status": "ok", "hasKey": has_any_key()})

    def handle_chat(self):
        """核心：接收前端请求，添加 API Key 后原样转发到 AI API"""
        try:
            body = self.read_body()
            data = json.loads(body)
        except Exception as e:
            return self.send_json_error(400, f"请求体解析失败: {e}")

        target_url = data.get("apiUrl", "")
        payload = data.get("payload", {})

        if not target_url:
            return self.send_json_error(400, "缺少 apiUrl")

        # 根据目标 URL 自动匹配对应的 API Key
        api_key = get_key_for_url(target_url)
        if not api_key:
            return self.send_json_error(500, f"服务端未配置该平台的 API Key。请设置对应环境变量（DEEPSEEK_KEY / CLAUDE_KEY / OPENAI_KEY）")

        # 判断 API 类型并添加鉴权
        is_claude = "anthropic.com" in target_url

        req_headers = {"Content-Type": "application/json"}
        if is_claude:
            req_headers["x-api-key"] = api_key
            req_headers["anthropic-version"] = "2023-06-01"
        else:
            req_headers["Authorization"] = f"Bearer {api_key}"

        model = payload.get("model", "?")
        msg_count = len(payload.get("messages", []))
        print(f"  → API: {target_url} | 模型: {model} | 消息数: {msg_count}")

        try:
            req = Request(
                target_url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=req_headers,
                method="POST",
            )
            with urlopen(req, timeout=TIMEOUT) as resp:
                resp_body = resp.read()
                resp_data = json.loads(resp_body)
                print(f"  ← 响应: {resp.status} ({len(resp_body)} bytes)")
                return self.send_json(resp_data, resp.status)
        except HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            try:
                err_json = json.loads(err_body)
                msg = err_json.get("error", {}).get("message", err_body)
            except Exception:
                msg = err_body or str(e)
            print(f"  ← API 错误: {e.code} - {msg}")
            return self.send_json_error(e.code, msg)
        except URLError as e:
            print(f"  ← 网络错误: {e.reason}")
            return self.send_json_error(502, f"无法连接到 API: {e.reason}")
        except Exception as e:
            traceback.print_exc()
            return self.send_json_error(502, f"代理请求失败: {str(e)}")

    # ---------- 静态文件 ----------
    def serve_static(self, path):
        # 安全检查：防止目录遍历
        safe_path = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip("/")))
        if not safe_path.startswith(STATIC_DIR):
            return self.send_error(403)

        if not os.path.exists(safe_path) or not os.path.isfile(safe_path):
            # SPA fallback: 所有未知路径返回 index.html
            safe_path = os.path.join(STATIC_DIR, "index.html")
            if not os.path.exists(safe_path):
                return self.send_error(404)

        ext = os.path.splitext(safe_path)[1].lower()
        content_type = MIME_TYPES.get(ext, "application/octet-stream")

        try:
            with open(safe_path, "rb") as f:
                content = f.read()
        except Exception:
            return self.send_error(500)

        # Gzip 压缩（HTML/CSS/JS/JSON/SVG）
        use_gzip = False
        accept_encoding = self.headers.get("Accept-Encoding", "")
        if "gzip" in accept_encoding and ext in (".html", ".css", ".js", ".json", ".svg", ".txt"):
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
                gz.write(content)
            compressed = buf.getvalue()
            if len(compressed) < len(content):
                content = compressed
                use_gzip = True

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "public, max-age=3600")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(content)

    # ---------- 工具方法 ----------
    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 0:
            return self.rfile.read(length).decode("utf-8")
        # chunked
        parts = []
        while True:
            line = self.rfile.readline().strip()
            if not line:
                break
            try:
                chunk_size = int(line, 16)
            except ValueError:
                break
            if chunk_size == 0:
                break
            parts.append(self.rfile.read(chunk_size))
            self.rfile.readline()
        return b"".join(parts).decode("utf-8") if parts else "{}"

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json_error(self, code, message):
        self.send_json({"error": {"message": message}}, code)

    def log_message(self, format, *args):
        # 只打印非静态资源的日志
        if "200" in str(args) or "304" in str(args):
            return
        print(f"  [{self.address_string()}] {format % args}")


def main():
    try:
        os.chdir(STATIC_DIR)

        available = get_available_providers()

        print("=" * 52, flush=True)
        print("  🤖 Claude 聊天 H5 - 生产服务器", flush=True)
        print("=" * 52, flush=True)
        print(f"  监听:       http://{HOST}:{PORT}", flush=True)
        print(f"  工作目录:    {os.getcwd()}", flush=True)
        print(f"  index.html: {'存在' if os.path.exists('index.html') else '缺失!'}", flush=True)
        if available:
            print(f"  可用平台:    {', '.join(p['name'] for p in available)}", flush=True)
            print(f"  共 {len(available)} 个平台已配置 Key", flush=True)
        else:
            print(f"  API Key:    ❌ 未配置！", flush=True)
            print(f"  MIMO_KEY环境: {'有' if os.environ.get('MIMO_KEY') else '无'}", flush=True)
            print(f"  DEEPSEEK_KEY: {'有' if os.environ.get('DEEPSEEK_KEY') else '无'}", flush=True)
        print("=" * 52, flush=True)

        server = ThreadingHTTPServer((HOST, PORT), AppHandler)
        print(f"🚀 服务器已启动，等待请求...", flush=True)
        server.serve_forever()
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
