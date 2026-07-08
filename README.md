# HZ Lab 门户网站

一个可扩展的个人门户，所有项目点开即用。

## 🚀 如何添加新功能

**只需两步，不用改 HTML：**

1. 把你的 HTML 文件放到项目根目录（例如 `snake.html`）
2. 在 `apps.json` 中添加一条记录：

```json
{
  "title": "贪吃蛇",
  "desc": "经典贪吃蛇游戏，方向键控制",
  "icon": "🐍",
  "url": "/snake.html",
  "color": "#00d2a0",
  "badge": "🎮 新游戏"
}
```

3. 保存，刷新首页 — 卡片自动出现 ✨

### apps.json 字段说明

| 字段 | 说明 |
|------|------|
| `title` | 卡片标题 |
| `desc` | 卡片描述（1-2句话） |
| `icon` | 图标，用 emoji |
| `url` | 点击跳转的链接 |
| `color` | 主题色（十六进制） |
| `badge` | 左上角标签文字 |
| `dashed` | 可选，`true` 表示虚线边框 |

### 颜色建议

| 风格 | 颜色 |
|------|------|
| 紫色（AI/工具） | `#6c5ce7` |
| 绿色（游戏） | `#00d2a0` |
| 蓝色（效率） | `#4dabf7` |
| 橙色（创造） | `#ff922b` |
| 粉色（创意） | `#f06595` |

---

## 一键部署（5分钟）

### Railway（推荐）

1. 把整个文件夹上传到 GitHub
2. 打开 [railway.app](https://railway.app)，用 GitHub 登录
3. 点「New Project」→「Deploy from GitHub repo」→ 选仓库
4. 添加环境变量：
   ```
   MIMO_KEY = tp-你的key
   ```
5. 点 Deploy，等 2 分钟

### 自己 VPS

```bash
# 上传到服务器后
python server.py &

# 搭配 Nginx 反代就能通过域名访问
```

---

## 文件结构

```
├── index.html          # 门户首页（自动从 apps.json 加载卡片）
├── apps.json           # 📝 添加新功能只需改这个文件
├── chat.html           # AI 聊天
├── minecraft.html      # MiniCraft 游戏
├── upload.html         # 作品上传页
├── server.py           # Flask 后端
├── providers.json      # API 密钥（已 gitignore）
└── requirements.txt
```
