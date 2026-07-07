# Claude 聊天 H5 - 上线部署

## 一次性部署（5分钟），之后用户打开网址直接用

---

### 方式一：Railway（推荐，免费额度够用）

1. 把 `claude-chat-h5` 文件夹上传到 GitHub
2. 打开 [railway.app](https://railway.app)，用 GitHub 登录
3. 点「New Project」→「Deploy from GitHub repo」→ 选你的仓库
4. 在项目设置里加一个环境变量：
   ```
   API_KEY = sk-be72692bda9d47c8b62b50b2c562e9d3
   ```
5. 点 Deploy，等 2 分钟

完成后你会得到一个网址，比如 `https://claude-chat.up.railway.app`，发给用户就能用。

---

### 方式二：自己 VPS

```bash
# 上传到服务器后
API_KEY=sk-xxx python server.py &

# 搭配 Nginx 反代就能通过域名访问
```

---

### 用户视角

用户打开网址 → 看到聊天界面 → 输入消息 → 收到回复

**他们不需要安装任何东西，不需要配置任何东西。**
