#!/usr/bin/env python3
"""HZ Lab - Flask"""
import json, os, io, time, uuid, zipfile
from flask import Flask, request, send_file, jsonify
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder=".")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB 上限

PORT = int(os.environ.get("PORT", 8765))
TIMEOUT = int(os.environ.get("TIMEOUT", 180))
GENERIC_KEY = ""

# 数据目录：Railway 挂载卷用 /data，本地用 .
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
META_FILE = os.path.join(DATA_DIR, "uploads.json")
COMMUNITY_APPS_FILE = os.path.join(DATA_DIR, "community_apps.json")
os.makedirs(UPLOAD_DIR, exist_ok=True)

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

# ---------- 上传数据 ----------
def load_uploads():
    if os.path.exists(META_FILE):
        with open(META_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_uploads(data):
    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ---------- 社区作品数据 ----------
def load_community_apps():
    if os.path.exists(COMMUNITY_APPS_FILE):
        with open(COMMUNITY_APPS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_community_apps(data):
    with open(COMMUNITY_APPS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ---------- ZIP 项目解压 ----------
def extract_zip_project(zip_path, extract_to):
    """安全解压 ZIP 项目包，返回入口文件相对路径（如 index.html）"""
    MAX_TOTAL = 100 * 1024 * 1024  # 100MB 上限

    with zipfile.ZipFile(zip_path, "r") as zf:
        total = sum(m.file_size for m in zf.infolist())
        if total > MAX_TOTAL:
            raise ValueError("ZIP 项目太大（上限 100MB）")

        candidates = []  # [(depth, path)]
        for member in zf.infolist():
            # 防止路径穿越
            safe = os.path.normpath(member.filename)
            if safe.startswith("..") or os.path.isabs(safe):
                continue
            if member.is_dir():
                continue

            target = os.path.join(extract_to, safe)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(member) as src:
                content = src.read()
            with open(target, "wb") as dst:
                dst.write(content)

            basename = os.path.basename(safe).lower()
            if basename == "index.html":
                depth = safe.count("/")
                candidates.append((depth, safe))
            elif basename.endswith(".html") and not any(c[1].lower().endswith("index.html") for c in candidates):
                candidates.append((safe.count("/"), safe))

        if not candidates:
            raise ValueError("ZIP 中没有 HTML 文件，至少需要一个入口页面（index.html）")

        # 选最浅层的 index.html，同层优先 index.html
        candidates.sort(key=lambda x: (x[0], not x[1].lower().endswith("index.html")))
        return candidates[0][1]

# ---------- 路由 ----------
@app.route("/")
def index():
    return send_file("index.html")

@app.route("/chat")
def chat():
    return send_file("chat.html")

@app.route("/upload")
def upload_page():
    return send_file("upload.html")

@app.route("/submit")
def submit_page():
    return send_file("submit.html")

# ---------- 社区作品 API ----------
@app.route("/api/community-apps")
def api_community_apps():
    """返回所有社区提交的作品"""
    return jsonify(load_community_apps())

@app.route("/api/submit-app", methods=["POST"])
def api_submit_app():
    """接收社区作品提交"""
    title = request.form.get("title", "").strip()
    desc = request.form.get("desc", "").strip()
    url = request.form.get("url", "").strip()
    author = request.form.get("author", "").strip()
    icon = request.form.get("icon", "🎯").strip()
    color = request.form.get("color", "#6c5ce7").strip()
    file = request.files.get("file")

    if not title:
        return jsonify({"error": "请输入标题"}), 400
    if not desc:
        return jsonify({"error": "请输入简介"}), 400

    # 如果上传了文件，保存并生成 URL
    if file and file.filename != "":
        filename = secure_filename(file.filename)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        if ext == "zip":
            # ZIP 项目 → 解压到子目录
            dir_name = uuid.uuid4().hex[:8]
            extract_to = os.path.join(UPLOAD_DIR, dir_name)
            os.makedirs(extract_to, exist_ok=True)
            zip_path = os.path.join(UPLOAD_DIR, f"{dir_name}.zip")
            file.save(zip_path)
            try:
                entry = extract_zip_project(zip_path, extract_to)
                url = f"/uploads/{dir_name}/{entry}"
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            finally:
                if os.path.exists(zip_path):
                    os.remove(zip_path)
        else:
            # 单文件
            unique_name = f"{uuid.uuid4().hex[:8]}_{filename}"
            filepath = os.path.join(UPLOAD_DIR, unique_name)
            file.save(filepath)
            url = f"/uploads/{unique_name}"

    if not url:
        return jsonify({"error": "请填写链接或上传文件"}), 400

    item = {
        "id": uuid.uuid4().hex[:8],
        "title": title,
        "desc": desc,
        "url": url,
        "icon": icon,
        "color": color,
        "author": author or "匿名",
        "time": int(time.time()),
    }

    apps = load_community_apps()
    apps.insert(0, item)  # 最新的排前面
    save_community_apps(apps)

    return jsonify({"ok": True, "item": item})

@app.route("/api/uploads")
def api_uploads():
    """返回所有上传作品列表"""
    return jsonify(load_uploads())

@app.route("/api/upload", methods=["POST"])
def api_upload():
    """接收文件上传"""
    title = request.form.get("title", "").strip()
    desc = request.form.get("desc", "").strip()
    file = request.files.get("file")

    if not title:
        return jsonify({"error": "请输入标题"}), 400
    if not file or file.filename == "":
        return jsonify({"error": "请选择文件"}), 400

    filename = secure_filename(file.filename)
    # 加随机前缀防重名
    unique_name = f"{uuid.uuid4().hex[:8]}_{filename}"
    filepath = os.path.join(UPLOAD_DIR, unique_name)
    file.save(filepath)

    item = {
        "id": uuid.uuid4().hex[:8],
        "title": title,
        "desc": desc,
        "filename": filename,
        "stored": unique_name,
        "size": os.path.getsize(filepath),
        "time": int(time.time()),
    }

    uploads = load_uploads()
    uploads.insert(0, item)  # 最新的排前面
    save_uploads(uploads)

    return jsonify({"ok": True, "item": item})

@app.route("/uploads/<path:name>")
def serve_uploads(name):
    """提供上传目录内的文件（HTML/CSS/JS 正常渲染，其他下载）"""
    # 安全处理：将路径逐段用 secure_filename 处理
    parts = name.replace("\\", "/").split("/")
    safe_parts = [secure_filename(p) for p in parts]
    path = os.path.join(UPLOAD_DIR, *safe_parts)
    if os.path.isfile(path):
        # 网页文件不强制下载，直接渲染
        ext = os.path.splitext(path)[1].lower()
        download = ext not in (".html", ".htm", ".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".woff", ".woff2", ".ttf")
        return send_file(path, as_attachment=download)
    elif os.path.isdir(path):
        # 目录→自动找 index.html
        for entry in ["index.html", "index.htm"]:
            idx = os.path.join(path, entry)
            if os.path.isfile(idx):
                return send_file(idx)
        return jsonify({"error": "目录中没有 index.html"}), 404
    return "文件不存在", 404

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
    print(f"🤖 HZ Lab - Flask", flush=True)
    print(f"   平台: {len(ALL_PROVIDERS)} 个", flush=True)
    print(f"   监听: 0.0.0.0:{PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT, debug=False)
