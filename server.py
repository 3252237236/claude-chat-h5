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
# 在线状态存储（内存）
online_users = {}  # {user_id: last_heartbeat_time}

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

        # 好友关系表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS friendships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                friend_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                message TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (friend_id) REFERENCES users(id),
                UNIQUE(user_id, friend_id)
            )
        """)

        # 私信表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_user_id INTEGER NOT NULL,
                to_user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                is_read INTEGER DEFAULT 0,
                FOREIGN KEY (from_user_id) REFERENCES users(id),
                FOREIGN KEY (to_user_id) REFERENCES users(id)
            )
        """)

        # 用户动态表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                target_id TEXT,
                target_title TEXT,
                content TEXT,
                image_url TEXT,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # 通知表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT,
                link TEXT,
                is_read INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # 迁移：给 users 表加 bio 和 avatar_url 列
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "bio" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN bio TEXT DEFAULT ''")
        if "avatar_url" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN avatar_url TEXT DEFAULT ''")

        # 迁移：给旧表加 content 和 image_url 列
        cols = [r[1] for r in conn.execute("PRAGMA table_info(activities)").fetchall()]
        if "content" not in cols:
            conn.execute("ALTER TABLE activities ADD COLUMN content TEXT")
        if "image_url" not in cols:
            conn.execute("ALTER TABLE activities ADD COLUMN image_url TEXT")

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

@app.route("/friends")
def friends_page():
    return send_file("friends.html")

@app.route("/admin")
def admin_page():
    return send_file("admin.html")

@app.route("/profile")
def profile_page():
    return send_file("profile.html")

@app.route("/search")
def search_page():
    return send_file("search.html")

@app.route("/notifications")
def notifications_page():
    return send_file("notifications.html")

@app.route("/convert")
def convert_page():
    return send_file("convert.html")

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

    # 记录用户动态
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO activities (user_id, action, target_id, target_title, created_at) VALUES (?, ?, ?, ?, ?)",
            (user["id"], "submit_app", item["id"], title, int(time.time()))
        )

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

    # 记录用户动态
    user = session.get("user")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO activities (user_id, action, target_id, target_title, created_at) VALUES (?, ?, ?, ?, ?)",
            (user["id"], "upload_file", item["id"], title, int(time.time()))
        )

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

@app.route("/api/admin/all")
@admin_required
def api_admin_all():
    """返回全部社区作品和文件（含已审核和待审核）"""
    return jsonify({
        "apps": load_community_apps(),
        "uploads": load_uploads(),
    })

@app.route("/api/admin/delete/<item_type>/<item_id>", methods=["POST"])
@admin_required
def api_admin_delete(item_type, item_id):
    """删除任意项目（已审核或待审核）"""
    if item_type == "app":
        items = [i for i in load_community_apps() if i["id"] != item_id]
        save_community_apps(items)
    elif item_type == "upload":
        items = load_uploads()
        for item in items:
            if item["id"] == item_id:
                fpath = os.path.join(UPLOAD_DIR, item.get("stored", ""))
                if os.path.exists(fpath):
                    os.remove(fpath)
        items = [i for i in items if i["id"] != item_id]
        save_uploads(items)
    return jsonify({"ok": True})

# ---------- 好友系统 API ----------
@app.route("/api/friends/request", methods=["POST"])
@login_required
def api_friend_request():
    """发送好友请求"""
    data = request.get_json(force=True)
    to_user_id = data.get("to_user_id")
    message = data.get("message", "").strip()

    if not to_user_id:
        return jsonify({"error": "缺少目标用户ID"}), 400

    user = session.get("user")
    from_user_id = user["id"]

    if from_user_id == to_user_id:
        return jsonify({"error": "不能添加自己为好友"}), 400

    with sqlite3.connect(DB_PATH) as conn:
        # 检查目标用户是否存在
        target = conn.execute("SELECT id, username FROM users WHERE id = ?", (to_user_id,)).fetchone()
        if not target:
            return jsonify({"error": "用户不存在"}), 404

        # 检查是否已经是好友
        existing = conn.execute(
            "SELECT id, status FROM friendships WHERE (user_id = ? AND friend_id = ?) OR (user_id = ? AND friend_id = ?)",
            (from_user_id, to_user_id, to_user_id, from_user_id)
        ).fetchone()

        if existing:
            if existing[1] == "accepted":
                return jsonify({"error": "已经是好友了"}), 400
            elif existing[1] == "pending":
                return jsonify({"error": "已经发送过好友请求"}), 400

        # 创建好友请求
        conn.execute(
            "INSERT INTO friendships (user_id, friend_id, status, created_at, message) VALUES (?, ?, 'pending', ?, ?)",
            (from_user_id, to_user_id, int(time.time()), message)
        )

    return jsonify({"ok": True})

@app.route("/api/friends/accept/<int:request_id>", methods=["POST"])
@login_required
def api_friend_accept(request_id):
    """接受好友请求"""
    user = session.get("user")

    with sqlite3.connect(DB_PATH) as conn:
        # 查找好友请求
        request = conn.execute(
            "SELECT id, user_id, friend_id, status FROM friendships WHERE id = ? AND friend_id = ? AND status = 'pending'",
            (request_id, user["id"])
        ).fetchone()

        if not request:
            return jsonify({"error": "好友请求不存在"}), 404

        # 更新状态为已接受
        conn.execute("UPDATE friendships SET status = 'accepted' WHERE id = ?", (request_id,))

        # 创建双向好友关系
        conn.execute(
            "INSERT OR IGNORE INTO friendships (user_id, friend_id, status, created_at) VALUES (?, ?, 'accepted', ?)",
            (user["id"], request[1], int(time.time()))
        )

    return jsonify({"ok": True})

@app.route("/api/friends/reject/<int:request_id>", methods=["POST"])
@login_required
def api_friend_reject(request_id):
    """拒绝好友请求"""
    user = session.get("user")

    with sqlite3.connect(DB_PATH) as conn:
        # 查找并删除好友请求
        result = conn.execute(
            "DELETE FROM friendships WHERE id = ? AND friend_id = ? AND status = 'pending'",
            (request_id, user["id"])
        )

        if result.rowcount == 0:
            return jsonify({"error": "好友请求不存在"}), 404

    return jsonify({"ok": True})

@app.route("/api/friends")
@login_required
def api_friends_list():
    """获取好友列表"""
    user = session.get("user")

    with sqlite3.connect(DB_PATH) as conn:
        # 获取所有已接受的好友
        friends = conn.execute("""
            SELECT u.id, u.username, u.created_at
            FROM friendships f
            JOIN users u ON (f.friend_id = u.id)
            WHERE f.user_id = ? AND f.status = 'accepted'
            ORDER BY u.username
        """, (user["id"],)).fetchall()

    result = []
    for friend in friends:
        is_online = friend[0] in online_users and (time.time() - online_users[friend[0]]) < 120  # 2分钟内活跃
        result.append({
            "id": friend[0],
            "username": friend[1],
            "is_online": is_online,
            "last_active": online_users.get(friend[0], 0)
        })

    return jsonify(result)

@app.route("/api/friends/requests")
@login_required
def api_friend_requests():
    """获取收到的好友请求"""
    user = session.get("user")

    with sqlite3.connect(DB_PATH) as conn:
        requests = conn.execute("""
            SELECT f.id, u.username, f.message, f.created_at, u.id as from_user_id
            FROM friendships f
            JOIN users u ON f.user_id = u.id
            WHERE f.friend_id = ? AND f.status = 'pending'
            ORDER BY f.created_at DESC
        """, (user["id"],)).fetchall()

    result = []
    for req in requests:
        result.append({
            "id": req[0],
            "from_username": req[1],
            "message": req[2],
            "created_at": req[3],
            "from_user_id": req[4]
        })

    return jsonify(result)

@app.route("/api/friends/<int:friend_id>", methods=["DELETE"])
@login_required
def api_friend_delete(friend_id):
    """删除好友"""
    user = session.get("user")

    with sqlite3.connect(DB_PATH) as conn:
        # 删除双向好友关系
        conn.execute(
            "DELETE FROM friendships WHERE (user_id = ? AND friend_id = ?) OR (user_id = ? AND friend_id = ?)",
            (user["id"], friend_id, friend_id, user["id"])
        )

    return jsonify({"ok": True})

@app.route("/api/friends/search")
@login_required
def api_friend_search():
    """搜索用户"""
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])

    user = session.get("user")

    with sqlite3.connect(DB_PATH) as conn:
        users = conn.execute(
            "SELECT id, username FROM users WHERE username LIKE ? AND id != ? LIMIT 20",
            (f"%{query}%", user["id"])
        ).fetchall()

        # 获取当前用户的好友列表
        friends = conn.execute(
            "SELECT friend_id FROM friendships WHERE user_id = ? AND status = 'accepted'",
            (user["id"],)
        ).fetchall()
        friend_ids = {f[0] for f in friends}

        # 获取待发送的请求
        sent_requests = conn.execute(
            "SELECT friend_id FROM friendships WHERE user_id = ? AND status = 'pending'",
            (user["id"],)
        ).fetchall()
        sent_request_ids = {r[0] for r in sent_requests}

        # 获取收到的请求
        received_requests = conn.execute(
            "SELECT user_id FROM friendships WHERE friend_id = ? AND status = 'pending'",
            (user["id"],)
        ).fetchall()
        received_request_ids = {r[0] for r in received_requests}

    result = []
    for u in users:
        is_friend = u[0] in friend_ids
        has_sent_request = u[0] in sent_request_ids
        has_received_request = u[0] in received_request_ids

        result.append({
            "id": u[0],
            "username": u[1],
            "is_friend": is_friend,
            "has_sent_request": has_sent_request,
            "has_received_request": has_received_request
        })

    return jsonify(result)

# ---------- 私信 API ----------
@app.route("/api/messages/send", methods=["POST"])
@login_required
def api_message_send():
    """发送私信"""
    data = request.get_json(force=True)
    to_user_id = data.get("to_user_id")
    content = data.get("content", "").strip()

    if not to_user_id:
        return jsonify({"error": "缺少目标用户ID"}), 400
    if not content:
        return jsonify({"error": "消息内容不能为空"}), 400

    user = session.get("user")

    with sqlite3.connect(DB_PATH) as conn:
        # 检查是否是好友
        friendship = conn.execute(
            "SELECT id FROM friendships WHERE user_id = ? AND friend_id = ? AND status = 'accepted'",
            (user["id"], to_user_id)
        ).fetchone()

        if not friendship:
            return jsonify({"error": "只能给好友发送私信"}), 400

        # 发送消息
        conn.execute(
            "INSERT INTO messages (from_user_id, to_user_id, content, created_at) VALUES (?, ?, ?, ?)",
            (user["id"], to_user_id, content, int(time.time()))
        )

    return jsonify({"ok": True})

@app.route("/api/messages/<int:other_user_id>")
@login_required
def api_messages_list(other_user_id):
    """获取与某用户的聊天记录"""
    user = session.get("user")

    with sqlite3.connect(DB_PATH) as conn:
        messages = conn.execute("""
            SELECT m.id, m.from_user_id, m.to_user_id, m.content, m.created_at, m.is_read
            FROM messages m
            WHERE (m.from_user_id = ? AND m.to_user_id = ?) OR (m.from_user_id = ? AND m.to_user_id = ?)
            ORDER BY m.created_at ASC
            LIMIT 100
        """, (user["id"], other_user_id, other_user_id, user["id"])).fetchall()

    result = []
    for msg in messages:
        result.append({
            "id": msg[0],
            "from_user_id": msg[1],
            "to_user_id": msg[2],
            "content": msg[3],
            "created_at": msg[4],
            "is_read": bool(msg[5])
        })

    return jsonify(result)

@app.route("/api/messages/unread")
@login_required
def api_messages_unread():
    """获取未读消息数"""
    user = session.get("user")

    with sqlite3.connect(DB_PATH) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE to_user_id = ? AND is_read = 0",
            (user["id"],)
        ).fetchone()[0]

    return jsonify({"count": count})

@app.route("/api/messages/read/<int:other_user_id>", methods=["POST"])
@login_required
def api_messages_read(other_user_id):
    """标记消息为已读"""
    user = session.get("user")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE messages SET is_read = 1 WHERE from_user_id = ? AND to_user_id = ? AND is_read = 0",
            (other_user_id, user["id"])
        )

    return jsonify({"ok": True})

# ---------- 动态 API ----------
@app.route("/api/activities")
@login_required
def api_activities():
    """获取好友动态"""
    user = session.get("user")

    with sqlite3.connect(DB_PATH) as conn:
        # 获取好友ID列表
        friend_ids = conn.execute(
            "SELECT friend_id FROM friendships WHERE user_id = ? AND status = 'accepted'",
            (user["id"],)
        ).fetchall()
        friend_ids = [f[0] for f in friend_ids]
        friend_ids.append(user["id"])  # 包含自己的动态

        # 获取动态
        placeholders = ",".join(["?" for _ in friend_ids])
        activities = conn.execute(f"""
            SELECT a.id, u.username, a.action, a.target_title, a.content, a.image_url, a.created_at
            FROM activities a
            JOIN users u ON a.user_id = u.id
            WHERE a.user_id IN ({placeholders})
            ORDER BY a.created_at DESC
            LIMIT 50
        """, friend_ids).fetchall()

    result = []
    for act in activities:
        result.append({
            "id": act[0],
            "username": act[1],
            "action": act[2],
            "target_title": act[3],
            "content": act[4],
            "image_url": act[5],
            "created_at": act[6]
        })

    return jsonify(result)

# ---------- 心跳 API ----------
@app.route("/api/heartbeat", methods=["POST"])
@login_required
def api_heartbeat():
    """心跳接口，更新在线状态"""
    user = session.get("user")
    online_users[user["id"]] = time.time()
    return jsonify({"ok": True})

# ---------- 发布动态 API ----------
@app.route("/api/activities/post", methods=["POST"])
@login_required
def api_activity_post():
    """发布新动态"""
    data = request.get_json(force=True)
    content = data.get("content", "").strip()
    image_url = data.get("image_url", "").strip()

    if not content and not image_url:
        return jsonify({"error": "请输入内容"}), 400

    user = session.get("user")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO activities (user_id, action, content, image_url, created_at) VALUES (?, ?, ?, ?, ?)",
            (user["id"], "post", content, image_url, int(time.time()))
        )

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

# ---------- 通知 API ----------
@app.route("/api/notifications")
@login_required
def api_notifications():
    """获取通知列表"""
    user = session.get("user")
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, type, title, content, link, is_read, created_at FROM notifications WHERE user_id = ? ORDER BY created_at DESC LIMIT 50",
            (user["id"],)
        ).fetchall()
    return jsonify([{
        "id": r[0], "type": r[1], "title": r[2], "content": r[3],
        "link": r[4], "is_read": bool(r[5]), "created_at": r[6]
    } for r in rows])

@app.route("/api/notifications/unread")
@login_required
def api_notifications_unread():
    """获取未读通知数"""
    user = session.get("user")
    with sqlite3.connect(DB_PATH) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0",
            (user["id"],)
        ).fetchone()[0]
    return jsonify({"count": count})

@app.route("/api/notifications/read/<int:nid>", methods=["POST"])
@login_required
def api_notification_read(nid):
    """标记单条通知已读"""
    user = session.get("user")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?", (nid, user["id"]))
    return jsonify({"ok": True})

@app.route("/api/notifications/read-all", methods=["POST"])
@login_required
def api_notifications_read_all():
    """全部标记已读"""
    user = session.get("user")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0", (user["id"],))
    return jsonify({"ok": True})

def create_notification(user_id, ntype, title, content="", link=""):
    """创建通知的辅助函数"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO notifications (user_id, type, title, content, link, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, ntype, title, content, link, int(time.time()))
        )

# ---------- 用户资料 API ----------
@app.route("/api/profile")
@login_required
def api_profile_me():
    """获取当前用户资料"""
    user = session.get("user")
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT id, username, bio, avatar_url, created_at, is_admin FROM users WHERE id = ?", (user["id"],)).fetchone()
    if not row:
        return jsonify({"error": "用户不存在"}), 404
    return jsonify({"id": row[0], "username": row[1], "bio": row[2] or "", "avatar_url": row[3] or "", "created_at": row[4], "is_admin": bool(row[5])})

@app.route("/api/profile/<int:uid>")
def api_profile_user(uid):
    """获取指定用户资料"""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT id, username, bio, avatar_url, created_at FROM users WHERE id = ?", (uid,)).fetchone()
        if not row:
            return jsonify({"error": "用户不存在"}), 404
        # 获取作品数
        apps = conn.execute("SELECT COUNT(*) FROM community_apps WHERE json_extract(data, '$.author') = (SELECT username FROM users WHERE id = ?) AND status = 'approved'", (uid,)).fetchone()[0]
        # 获取文件数
        files = load_uploads()
        file_count = len([f for f in files if f.get("uploader_id") == uid and f.get("status") == "approved"])
        # 获取好友数
        friends = conn.execute("SELECT COUNT(*) FROM friendships WHERE user_id = ? AND status = 'accepted'", (uid,)).fetchone()[0]
    return jsonify({"id": row[0], "username": row[1], "bio": row[2] or "", "avatar_url": row[3] or "", "created_at": row[4], "apps_count": apps, "files_count": file_count, "friends_count": friends})

@app.route("/api/profile", methods=["PUT"])
@login_required
def api_profile_update():
    """更新个人资料"""
    data = request.get_json(force=True)
    bio = data.get("bio", "").strip()
    avatar_url = data.get("avatar_url", "").strip()
    user = session.get("user")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET bio = ?, avatar_url = ? WHERE id = ?", (bio, avatar_url, user["id"]))
    return jsonify({"ok": True})

# ---------- 全局搜索 API ----------
@app.route("/api/search")
def api_search():
    """全局搜索：用户、社区作品、文件"""
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"users": [], "apps": [], "files": []})
    like = f"%{query}%"
    # 搜索用户
    with sqlite3.connect(DB_PATH) as conn:
        users = conn.execute("SELECT id, username, bio FROM users WHERE username LIKE ? LIMIT 10", (like,)).fetchall()
    user_results = [{"id": u[0], "username": u[1], "bio": u[2] or ""} for u in users]
    # 搜索社区作品
    apps = load_community_apps()
    app_results = [a for a in apps if a.get("status") == "approved" and (query.lower() in a.get("title", "").lower() or query.lower() in a.get("desc", "").lower())][:10]
    # 搜索文件
    files = load_uploads()
    file_results = [f for f in files if f.get("status") == "approved" and (query.lower() in f.get("title", "").lower() or query.lower() in f.get("desc", "").lower())][:10]
    return jsonify({"users": user_results, "apps": app_results, "files": file_results})

# ---------- 文件格式转换 API ----------
import subprocess
from io import BytesIO

CONVERT_FORMATS = {
    "image": {
        "input": ["jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "ico"],
        "output": ["jpg", "png", "gif", "webp", "bmp", "ico"]
    },
    "document": {
        "input": ["pdf", "docx", "txt", "md", "html"],
        "output": ["pdf", "txt", "html"]
    },
    "data": {
        "input": ["json", "csv", "xml", "yaml", "yml"],
        "output": ["json", "csv", "xml", "yaml"]
    },
    "audio": {
        "input": ["mp3", "wav", "ogg", "flac", "aac", "m4a"],
        "output": ["mp3", "wav", "ogg", "flac"]
    }
}

def detect_category(ext):
    for cat, fmts in CONVERT_FORMATS.items():
        if ext in fmts["input"]:
            return cat
    return None

@app.route("/api/convert/formats")
def api_convert_formats():
    """返回支持的格式列表"""
    return jsonify(CONVERT_FORMATS)

@app.route("/api/convert", methods=["POST"])
def api_convert():
    """文件格式转换"""
    file = request.files.get("file")
    target_format = request.form.get("target", "").lower().strip()

    if not file or file.filename == "":
        return jsonify({"error": "请选择文件"}), 400
    if not target_format:
        return jsonify({"error": "请选择目标格式"}), 400

    filename = secure_filename(file.filename)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if not ext:
        return jsonify({"error": "无法识别文件格式"}), 400

    category = detect_category(ext)
    if not category:
        return jsonify({"error": f"不支持 {ext} 格式"}), 400
    if target_format not in CONVERT_FORMATS[category]["output"]:
        return jsonify({"error": f"无法将 {ext} 转换为 {target_format}"}), 400

    try:
        if category == "image":
            result = convert_image(file, ext, target_format)
        elif category == "document":
            result = convert_document(file, ext, target_format)
        elif category == "data":
            result = convert_data(file, ext, target_format)
        elif category == "audio":
            result = convert_audio(file, ext, target_format)
        else:
            return jsonify({"error": "不支持的转换类型"}), 400

        if result is None:
            return jsonify({"error": "转换失败"}), 500

        output_name = filename.rsplit(".", 1)[0] + "." + target_format
        return send_file(
            BytesIO(result),
            as_attachment=True,
            download_name=output_name,
            mimetype=f"application/octet-stream"
        )
    except Exception as e:
        return jsonify({"error": f"转换失败: {str(e)}"}), 500

def convert_image(file, src_ext, dst_ext):
    try:
        from PIL import Image
    except ImportError:
        return None
    img = Image.open(file.stream)
    if dst_ext in ("jpg", "jpeg"):
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        dst_ext = "jpeg"
    if dst_ext == "bmp":
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format=dst_ext.upper())
    return buf.getvalue()

def convert_document(file, src_ext, dst_ext):
    content = ""
    if src_ext == "txt" or src_ext == "md":
        content = file.read().decode("utf-8", errors="replace")
    elif src_ext == "docx":
        try:
            from docx import Document
        except ImportError:
            return None
        doc = Document(file.stream)
        content = "\n".join([p.text for p in doc.paragraphs])
    elif src_ext == "pdf":
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            return None
        reader = PdfReader(file.stream)
        content = "\n".join([p.extract_text() or "" for p in reader.pages])
    elif src_ext == "html":
        content = file.read().decode("utf-8", errors="replace")

    if dst_ext == "txt":
        return content.encode("utf-8")
    elif dst_ext == "html":
        html = f"<!DOCTYPE html><html><head><meta charset='utf-8'></head><body><pre>{content}</pre></body></html>"
        return html.encode("utf-8")
    elif dst_ext == "pdf":
        try:
            from reportlab.lib.pagesizes import letter
            from reportlab.pdfgen import canvas as rl_canvas
        except ImportError:
            return None
        buf = BytesIO()
        c = rl_canvas.Canvas(buf, pagesize=letter)
        y = 750
        for line in content.split("\n"):
            if y < 50:
                c.showPage()
                y = 750
            c.drawString(50, y, line[:100])
            y -= 14
        c.save()
        return buf.getvalue()
    return None

def convert_data(file, src_ext, dst_ext):
    import csv, xml.etree.ElementTree as ET
    data = None

    if src_ext == "json":
        data = json.loads(file.read().decode("utf-8"))
    elif src_ext == "csv":
        reader = csv.DictReader(io.StringIO(file.read().decode("utf-8")))
        data = list(reader)
    elif src_ext == "xml":
        tree = ET.parse(file.stream)
        root = tree.getroot()
        data = [{child.tag: child.text for child in item} for item in root]
    elif src_ext in ("yaml", "yml"):
        try:
            import yaml
            data = yaml.safe_load(file.read().decode("utf-8"))
        except ImportError:
            return None

    if data is None:
        return None

    if dst_ext == "json":
        return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    elif dst_ext == "csv":
        if isinstance(data, list) and len(data) > 0:
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=data[0].keys())
            writer.writeheader()
            writer.writerows(data)
            return buf.getvalue().encode("utf-8")
    elif dst_ext == "xml":
        root = ET.Element("data")
        if isinstance(data, list):
            for item in data:
                elem = ET.SubElement(root, "item")
                for k, v in item.items():
                    child = ET.SubElement(elem, k)
                    child.text = str(v)
        return ET.tostring(root, encoding="unicode").encode("utf-8")
    elif dst_ext == "yaml":
        try:
            import yaml
            return yaml.dump(data, allow_unicode=True).encode("utf-8")
        except ImportError:
            return None
    return None

def convert_audio(file, src_ext, dst_ext):
    try:
        from pydub import AudioSegment
        audio = AudioSegment.from_file(file.stream, format=src_ext)
        buf = BytesIO()
        audio.export(buf, format=dst_ext)
        return buf.getvalue()
    except Exception:
        return None

# ---------- 入口 ----------
if __name__ == "__main__":
    print(f"🤖 HZ Lab - Flask", flush=True)
    print(f"   平台: {len(ALL_PROVIDERS)} 个", flush=True)
    print(f"   监听: 0.0.0.0:{PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT, debug=False)
