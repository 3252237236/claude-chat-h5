#!/usr/bin/env python3
"""HZ Lab - Flask"""
import json, os, io, time, uuid, zipfile, sqlite3, secrets
from flask import Flask, request, send_file, jsonify, session
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, static_folder=".")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB 上限

PORT = int(os.environ.get("PORT", 8765))
TIMEOUT = int(os.environ.get("TIMEOUT", 180))
GENERIC_KEY = ""

# 数据目录：Railway 挂载卷用 /data，本地用 .
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))

# 持久化 secret key，避免重启后 session 失效
SECRET_FILE = os.path.join(DATA_DIR, ".secret_key")
if os.path.exists(SECRET_FILE):
    with open(SECRET_FILE, "r") as f:
        app.secret_key = f.read().strip()
else:
    app.secret_key = secrets.token_hex(32)
    os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
    with open(SECRET_FILE, "w") as f:
        f.write(app.secret_key)
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
META_FILE = os.path.join(DATA_DIR, "uploads.json")
COMMUNITY_APPS_FILE = os.path.join(DATA_DIR, "community_apps.json")
DB_PATH = os.path.join(DATA_DIR, "users.db")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ---------- 用户数据库 ----------
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                is_admin INTEGER DEFAULT 0
            )
        """)
        # 迁移：给旧表加 is_admin 列
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "is_admin" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
        # 环境变量指定的管理员自动提权
        admin_user = os.environ.get("ADMIN_USER", "")
        if admin_user:
            conn.execute("UPDATE users SET is_admin = 1 WHERE username = ?", (admin_user,))
        # 指定用户提权
        conn.execute("UPDATE users SET is_admin = 1 WHERE username = ?", ("3252237236",))

def create_user(username, password):
    with sqlite3.connect(DB_PATH) as conn:
        # 管理员判定：第一个注册 或 在 ADMIN_USER 列表中
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        admin_list = os.environ.get("ADMIN_USER", "").split(",")
        is_admin = 1 if (count == 0 or username == "3252237236" or username in admin_list) else 0
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at, is_admin) VALUES (?, ?, ?, ?)",
                (username, generate_password_hash(password), int(time.time()), is_admin)
            )
            return True, None
        except sqlite3.IntegrityError:
            return False, "用户名已存在"

def verify_user(username, password):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, is_admin FROM users WHERE username = ?",
            (username,)
        ).fetchone()
        if row and check_password_hash(row[2], password):
            return {"id": row[0], "username": row[1], "is_admin": bool(row[3])}
    return None

init_db()

# ---------- 权限检查 ----------
from functools import wraps
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return jsonify({"error": "请先登录"}), 401
        return f(*args, **kwargs)
    return wrapper

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = session.get("user")
        if not user:
            return jsonify({"error": "请先登录"}), 401
        if not user.get("is_admin"):
            return jsonify({"error": "需要管理员权限"}), 403
        return f(*args, **kwargs)
    return wrapper

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

# ---------- 种子数据：默认社区作品 ----------
DEFAULT_COMMUNITY = [
    {
        "id": "seed_mc",
        "title": "MiniCraft",
        "desc": "简易我的世界 · 3D 体素沙盒，自由建造与探索",
        "url": "/minecraft.html",
        "icon": "⛏️",
        "color": "#00d2a0",
        "author": "HZ Lab",
        "time": int(time.time()),
        "status": "approved",
    },
    {
        "id": "seed_gomoku",
        "title": "五子棋",
        "desc": "经典双人对弈 · 15路棋盘，五子连珠即胜",
        "url": "/gomoku.html",
        "icon": "🎯",
        "color": "#ff922b",
        "author": "HZ Lab",
        "time": int(time.time()),
        "status": "approved",
    },
]

def seed_community_apps():
    apps = load_community_apps()
    existing = {a.get("id", "") for a in apps}
    changed = False
    for item in DEFAULT_COMMUNITY:
        if item["id"] not in existing:
            apps.insert(0, item)
            changed = True
    if changed:
        save_community_apps(apps)

seed_community_apps()

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

# ---------- 门禁 ----------
AUTH_WHITELIST = {"/login", "/api/login", "/api/register", "/api/logout", "/api/me", "/api/health"}

@app.before_request
def require_login():
    if request.path in AUTH_WHITELIST:
        return None
    if request.path.startswith("/api/"):
        if not session.get("user"):
            return jsonify({"error": "请先登录"}), 401
        return None
    # 页面访问：未登录跳转登录页
    if not session.get("user"):
        # 排除静态资源请求（浏览器自动发起的 favicon 等）
        if request.path.startswith("/api/"):
            return jsonify({"error": "请先登录"}), 401
        # 允许 login 页面自身
        if request.path == "/login":
            return None
        return send_file("login.html")
    return None

# ---------- 路由 ----------
@app.route("/")
def index():
    if not session.get("user"):
        return send_file("login.html")
    return send_file("index.html")

@app.route("/login")
def login_page():
    if session.get("user"):
        return send_file("index.html")
    return send_file("login.html")

@app.route("/chat")
def chat():
    return send_file("chat.html")

@app.route("/upload")
def upload_page():
    return send_file("upload.html")

@app.route("/submit")
def submit_page():
    return send_file("submit.html")

@app.route("/files")
def files_page():
    return send_file("files.html")

@app.route("/admin")
def admin_page():
    return send_file("admin.html")

# ---------- 用户认证 API ----------
@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    if len(username) < 2 or len(username) > 20:
        return jsonify({"error": "用户名需要 2-20 个字符"}), 400
    if len(password) < 4:
        return jsonify({"error": "密码至少 4 位"}), 400

    ok, err = create_user(username, password)
    if not ok:
        return jsonify({"error": err}), 409

    # 注册成功直接登录
    user = verify_user(username, password)
    session["user"] = user
    return jsonify({"ok": True, "user": user})

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    user = verify_user(username, password)
    if not user:
        return jsonify({"error": "用户名或密码错误"}), 401

    session["user"] = user
    return jsonify({"ok": True, "user": user})

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("user", None)
    return jsonify({"ok": True})

@app.route("/api/me")
def api_me():
    user = session.get("user")
    return jsonify({"user": user})

# ---------- 社区作品 API ----------
@app.route("/api/community-apps")
def api_community_apps():
    """返回所有已审核的社区作品"""
    items = [i for i in load_community_apps() if i.get("status", "approved") == "approved"]
    return jsonify(items)

@app.route("/api/submit-app", methods=["POST"])
@login_required
def api_submit_app():
    """接收社区作品提交"""
    title = request.form.get("title", "").strip()
    desc = request.form.get("desc", "").strip()
    url = request.form.get("url", "").strip()
    author = request.form.get("author", "").strip()
    icon = request.form.get("icon", "🎯").strip()
    color = request.form.get("color", "#6c5ce7").strip()
    file = request.files.get("file")

    # 已登录用户自动用其用户名
    user = session.get("user")
    if user and not author:
        author = user["username"]

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
        "status": "pending",
    }

    apps = load_community_apps()
    apps.insert(0, item)
    save_community_apps(apps)

    return jsonify({"ok": True, "item": item, "pending": True})

@app.route("/api/uploads")
def api_uploads():
    """返回所有已审核的上传文件"""
    items = [i for i in load_uploads() if i.get("status", "approved") == "approved"]
    return jsonify(items)

@app.route("/api/upload", methods=["POST"])
@login_required
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
        "status": "pending",
    }

    uploads = load_uploads()
    uploads.insert(0, item)
    save_uploads(uploads)

    return jsonify({"ok": True, "item": item, "pending": True})

# ---------- 管理员 API ----------
@app.route("/api/admin/users")
@admin_required
def api_admin_users():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, username, is_admin, created_at FROM users ORDER BY id"
        ).fetchall()
    users = [{"id": r[0], "username": r[1], "is_admin": bool(r[2]), "created_at": r[3]} for r in rows]
    return jsonify(users)

@app.route("/api/admin/users/<int:uid>/toggle-admin", methods=["POST"])
@admin_required
def api_admin_toggle(uid):
    current = session.get("user")
    if current["id"] == uid:
        return jsonify({"error": "不能给自己切换管理员"}), 400
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (uid,)).fetchone()
        if not row:
            return jsonify({"error": "用户不存在"}), 404
        new_val = 0 if row[0] else 1
        conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (new_val, uid))
    return jsonify({"ok": True, "is_admin": bool(new_val)})

@app.route("/api/admin/users/<int:uid>/delete", methods=["POST"])
@admin_required
def api_admin_delete_user(uid):
    current = session.get("user")
    if current["id"] == uid:
        return jsonify({"error": "不能删除自己"}), 400
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    return jsonify({"ok": True})

@app.route("/api/admin/pending")
@admin_required
def api_admin_pending():
    apps = [a for a in load_community_apps() if a.get("status") == "pending"]
    uploads = [u for u in load_uploads() if u.get("status") == "pending"]
    return jsonify({"apps": apps, "uploads": uploads})

@app.route("/api/admin/approve/<item_type>/<item_id>", methods=["POST"])
@admin_required
def api_admin_approve(item_type, item_id):
    if item_type == "app":
        items = load_community_apps()
        for item in items:
            if item["id"] == item_id:
                item["status"] = "approved"
                save_community_apps(items)
                return jsonify({"ok": True})
    elif item_type == "upload":
        items = load_uploads()
        for item in items:
            if item["id"] == item_id:
                item["status"] = "approved"
                save_uploads(items)
                return jsonify({"ok": True})
    return jsonify({"error": "未找到"}), 404

@app.route("/api/admin/reject/<item_type>/<item_id>", methods=["POST"])
@admin_required
def api_admin_reject(item_type, item_id):
    if item_type == "app":
        items = load_community_apps()
        items = [i for i in items if i["id"] != item_id]
        save_community_apps(items)
    elif item_type == "upload":
        items = load_uploads()
        for item in items:
            if item["id"] == item_id:
                # 删除文件
                fpath = os.path.join(UPLOAD_DIR, item.get("stored", ""))
                if os.path.exists(fpath):
                    os.remove(fpath)
        items = [i for i in items if i["id"] != item_id]
        save_uploads(items)
    return jsonify({"ok": True})

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
