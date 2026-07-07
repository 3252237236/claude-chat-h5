#!/usr/bin/env python3
"""Claude 聊天 H5 - Flask 版本"""
import json, os, io
from flask import Flask, request, send_file, jsonify
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

app = Flask(__name__, static_folder=".")

PORT = int(os.environ.get("PORT", 8765))
TIMEOUT = int(os.environ.get("TIMEOUT", 180))
GENERIC_KEY = ""

# ---------- 加载平台 ----------
def _load_providers():
    for fn in ["providers.json", "providers.example.json"]:
        if os.path.exists(fn):
            with open(fn, "r", encoding="utf-8") as f:
                return json.load(f).get("providers", [])
    return []

ALL_PROVIDERS = _load_providers()
for p in ALL_PROVIDERS:
    key = p.get("key", "") or os.environ.get(p.get("env", ""), "") or os.environ.get("API_KEY", "")
    p["_key"] = key
    if key and not GENERIC_KEY:
        GENERIC_KEY = key

# ---------- 路由 ----------
@app.route("/")
def index():
    return send_file("index.html")

@app.route("/chat")
def chat():
    return send_file("chat.html")

@app.route("/<path:path>")
def static_files(path):
    if os.path.exists(path):
        return send_file(path)
    return send_file("index.html")

@app.route("/api/config")
def api_config():
    available = [{"id": p["name"].lower().replace(" ", "-"), "name": p["name"], "url": p["url"], "model": p["model"]} for p in ALL_PROVIDERS if p.get("_key")]
    return jsonify({"hasKey": bool(available), "providers": available})

@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok"})

@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(force=True)
    target_url = data.get("apiUrl", "")
    payload = data.get("payload", {})

    if not target_url:
        return jsonify({"error": {"message": "缺少 apiUrl"}}), 400

    # 找 key
    api_key = ""
    for p in ALL_PROVIDERS:
        if p["url"] == target_url and p.get("_key"):
            api_key = p["_key"]
            break
    if not api_key:
        api_key = GENERIC_KEY
    if not api_key:
        return jsonify({"error": {"message": "服务端未配置 API Key"}}), 500

    is_claude = "anthropic.com" in target_url
    headers = {"Content-Type": "application/json"}
    if is_claude:
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        req = Request(target_url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        with urlopen(req, timeout=TIMEOUT) as resp:
            return jsonify(json.loads(resp.read())), resp.status
    except HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        try:
            msg = json.loads(err).get("error", {}).get("message", err)
        except:
            msg = err
        return jsonify({"error": {"message": msg}}), e.code
    except Exception as e:
        return jsonify({"error": {"message": str(e)}}), 502

# ---------- 入口 ----------
if __name__ == "__main__":
    print(f"🤖 Claude 聊天 H5 - Flask", flush=True)
    print(f"   平台: {len(ALL_PROVIDERS)} 个", flush=True)
    print(f"   监听: 0.0.0.0:{PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT, debug=False)
