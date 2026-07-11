const express = require('express');
const http = require('http');
const { WebSocketServer } = require('ws');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');
const pty = require('node-pty');

// Load config (fallback to env vars if config.json doesn't exist)
let config = {};
try {
  config = JSON.parse(fs.readFileSync(path.join(__dirname, 'config.json'), 'utf-8'));
} catch (e) {
  console.log('[Config] config.json not found, using environment variables');
}
const PORT = process.env.VIBE_PORT || config.port || 3000;
const PASSWORD = process.env.VIBE_PASSWORD || config.password || 'vibe123';
let WORK_DIR = process.env.VIBE_WORK_DIR || config.workDir || __dirname;
let allowUnsafe = config.allowUnsafe || false;

const app = express();
app.use(express.json());
const server = http.createServer(app);
const wss = new WebSocketServer({ server });

// Serve static files
app.use(express.static(path.join(__dirname, 'public')));

// ========== Shared Session State ==========
let sharedSessionId = null;
let claudeProcess = null;
let messageHistory = []; // Store all messages for catch-up
const clients = new Set(); // All authenticated WebSocket clients

// ========== History Sync ==========
const historyPath = path.join(os.homedir(), '.claude', 'history.jsonl');

function appendToHistory(message, sessionId, project) {
  const entry = {
    display: message,
    pastedContents: {},
    timestamp: Date.now(),
    project: project || WORK_DIR,
    sessionId: sessionId || sharedSessionId || ''
  };
  try {
    fs.appendFileSync(historyPath, JSON.stringify(entry) + '\n');
    console.log(`[History] Synced: ${message.substring(0, 50)}...`);
  } catch (err) {
    console.error(`[History] Failed to write:`, err.message);
  }
}

// ========== PTY (Terminal Mirror) State ==========
let ptyProcess = null;
let ptyBuffer = ''; // Buffer for incomplete output
let terminalInputBuffer = ''; // Buffer for user input
const terminalClients = new Set(); // Clients in terminal mode

function broadcast(data, excludeWs) {
  const msg = JSON.stringify(data);
  for (const client of clients) {
    if (client !== excludeWs && client.readyState === 1) {
      client.send(msg);
    }
  }
}

function broadcastAll(data) {
  const msg = JSON.stringify(data);
  let sent = 0;
  for (const client of clients) {
    if (client.readyState === 1) {
      client.send(msg);
      sent++;
    }
  }
  if (data.type === 'chat_message') {
    console.log(`[Broadcast] ${data.role}: ${(data.content || '').substring(0, 50)} → ${sent} clients`);
  }
}

// Send chat message to terminal viewers as readable text
function broadcastToTerminal(text) {
  for (const client of terminalClients) {
    if (client.readyState === 1) {
      client.send(JSON.stringify({ type: 'terminal', data: text }));
    }
  }
}

// ========== PTY Management ==========
function startPty(cols, rows, mode) {
  if (ptyProcess) {
    ptyProcess.kill();
    ptyProcess = null;
  }

  let shell, shellArgs;

  const claudePath = os.platform() === 'win32'
    ? path.join(os.homedir(), 'AppData', 'Roaming', 'npm', 'claude.cmd')
    : 'claude';

  if (mode === 'claude') {
    // Start Claude directly
    shell = claudePath;
    shellArgs = ['--permission-mode', allowUnsafe ? 'auto' : 'default'];
    if (allowUnsafe) {
      shellArgs.push('--dangerously-skip-permissions');
    }
  } else {
    // Start regular shell (default) - with claude in PATH
    if (os.platform() === 'win32') {
      shell = 'cmd.exe';
      shellArgs = ['/K', `set PATH=%PATH%;${path.dirname(claudePath)}`];
    } else {
      shell = process.env.SHELL || '/bin/bash';
      shellArgs = [];
    }
  }

  ptyProcess = pty.spawn(shell, shellArgs, {
    name: 'xterm-256color',
    cols: cols || 80,
    rows: rows || 24,
    cwd: WORK_DIR,
    env: { ...process.env }
  });

  ptyBuffer = '';

  ptyProcess.onData((data) => {
    ptyBuffer += data;
    // Broadcast to all terminal clients
    for (const client of terminalClients) {
      if (client.readyState === 1) {
        client.send(JSON.stringify({ type: 'terminal', data }));
      }
    }
  });

  ptyProcess.onExit(({ exitCode }) => {
    console.log(`[PTY] Claude exited with code ${exitCode}`);
    for (const client of terminalClients) {
      if (client.readyState === 1) {
        client.send(JSON.stringify({ type: 'terminal_exit', code: exitCode }));
      }
    }
    ptyProcess = null;
  });

  console.log('[PTY] Started Claude terminal');
}

function writeToPty(data) {
  if (ptyProcess) {
    ptyProcess.write(data);
  }
}

function resizePty(cols, rows) {
  if (ptyProcess) {
    ptyProcess.resize(cols, rows);
  }
}

function stopPty() {
  if (ptyProcess) {
    ptyProcess.kill();
    ptyProcess = null;
  }
}

// ========== API Routes ==========

// Health check
app.get('/health', (req, res) => res.json({ status: 'ok' }));

// Get/set unsafe mode
app.get('/api/mode', (req, res) => res.json({ allowUnsafe }));

app.post('/api/mode', (req, res) => {
  allowUnsafe = !!req.body.allowUnsafe;
  console.log(`[Mode] allowUnsafe set to ${allowUnsafe}`);
  res.json({ allowUnsafe });
});

// Get current session info
app.get('/api/session', (req, res) => res.json({
  sessionId: sharedSessionId,
  clientCount: clients.size
}));

// List past sessions from history.jsonl, grouped by project
app.get('/api/sessions', (req, res) => {
  const historyPath = path.join(require('os').homedir(), '.claude', 'history.jsonl');
  const sessionsDir = path.join(require('os').homedir(), '.claude', 'sessions');

  // Get search query parameter
  const searchQuery = req.query.q || '';

  // Scan all project folders in ~/.claude/projects/
  const projectsDir = path.join(os.homedir(), '.claude', 'projects');
  const projectFolders = [];

  if (fs.existsSync(projectsDir)) {
    const folders = fs.readdirSync(projectsDir);
    for (const folder of folders) {
      const folderPath = path.join(projectsDir, folder);
      const stat = fs.statSync(folderPath);
      if (!stat.isDirectory()) continue;

      // Check if folder has memory files
      const memoryDir = path.join(folderPath, 'memory');
      const hasMemory = fs.existsSync(memoryDir);

      // Convert folder name back to path (C--Users-hs-Desktop -> C:\Users\hs\Desktop)
      const projectPath = folder.replace(/--/g, '\\').replace(/-/g, ' ');

      // Count session files
      const sessionFiles = fs.readdirSync(folderPath).filter(f => f.endsWith('.jsonl'));

      // Get last modified time
      const lastModified = stat.mtimeMs;

      // Read memory files if they exist
      let memoryFiles = [];
      if (hasMemory) {
        memoryFiles = fs.readdirSync(memoryDir).filter(f => f.endsWith('.md'));
      }

      projectFolders.push({
        folderName: folder,
        projectPath: projectPath,
        displayName: projectPath.split('\\').pop() || folder,
        hasMemory: hasMemory,
        memoryFiles: memoryFiles,
        sessionCount: sessionFiles.length,
        lastModified: lastModified
      });
    }
  }

  // Also get sessions from history.jsonl
  const sessionsMap = new Map();
  if (fs.existsSync(historyPath)) {
    const lines = fs.readFileSync(historyPath, 'utf-8').split('\n').filter(Boolean);
    for (const line of lines) {
      try {
        const entry = JSON.parse(line);
        const sid = entry.sessionId;
        if (!sid) continue;

        if (!sessionsMap.has(sid)) {
          sessionsMap.set(sid, {
            sessionId: sid,
            project: entry.project || '',
            messages: [],
            lastTimestamp: 0
          });
        }

        const session = sessionsMap.get(sid);
        session.messages.push(entry.display || '');
        session.lastTimestamp = Math.max(session.lastTimestamp, entry.timestamp || 0);
      } catch {}
    }
  }

  // Group sessions by project
  const projectMap = new Map();
  for (const s of sessionsMap.values()) {
    const proj = s.project || '(unknown)';
    if (!projectMap.has(proj)) {
      projectMap.set(proj, {
        project: proj,
        displayName: proj.split(/[/\\]/).pop() || proj,
        sessions: []
      });
    }
    projectMap.get(proj).sessions.push({
      sessionId: s.sessionId,
      project: s.project,
      lastMessage: s.messages[s.messages.length - 1] || '',
      messageCount: s.messages.length,
      lastTimestamp: s.lastTimestamp
    });
  }

  // Sort sessions within each project
  for (const p of projectMap.values()) {
    p.sessions.sort((a, b) => b.lastTimestamp - a.lastTimestamp);
  }

  // Merge project folders with sessions
  const allProjects = [];

  // Add projects from folders
  for (const folder of projectFolders) {
    const existingProject = projectMap.get(folder.projectPath);
    allProjects.push({
      project: folder.projectPath,
      displayName: folder.displayName,
      folderName: folder.folderName,
      hasMemory: folder.hasMemory,
      memoryFiles: folder.memoryFiles,
      sessions: existingProject ? existingProject.sessions : [],
      lastModified: folder.lastModified,
      sessionCount: folder.sessionCount
    });
  }

  // Add projects from history that don't have folders
  for (const [proj, data] of projectMap) {
    if (!projectFolders.find(f => f.projectPath === proj)) {
      allProjects.push({
        project: proj,
        displayName: data.displayName,
        folderName: '',
        hasMemory: false,
        memoryFiles: [],
        sessions: data.sessions,
        lastModified: 0,
        sessionCount: data.sessions.length
      });
    }
  }

  // Apply search filter if query exists
  let filteredProjects = allProjects;
  if (searchQuery) {
    const query = searchQuery.toLowerCase();
    filteredProjects = allProjects.filter(p => {
      // Search in project name
      if (p.displayName.toLowerCase().includes(query)) return true;
      if (p.project.toLowerCase().includes(query)) return true;

      // Search in memory files
      if (p.memoryFiles.some(f => f.toLowerCase().includes(query))) return true;

      // Search in session messages
      return p.sessions.some(s =>
        s.lastMessage.toLowerCase().includes(query)
      );
    });
  }

  // Mark the current server project (don't filter out, just flag it)
  for (const p of filteredProjects) {
    const normalizedProject = p.project.replace(/\//g, '\\').toLowerCase();
    const normalizedWorkDir = WORK_DIR.replace(/\//g, '\\').toLowerCase();
    p.isCurrentServer = normalizedProject === normalizedWorkDir;
  }

  // Sort by last modified time
  filteredProjects.sort((a, b) => (b.lastModified || b.sessions[0]?.lastTimestamp || 0) - (a.lastModified || a.sessions[0]?.lastTimestamp || 0));

  res.json({ projects: filteredProjects, currentDir: WORK_DIR, totalProjects: allProjects.length });
});

// Get conversation history for a specific session
app.get('/api/session/:sessionId/history', (req, res) => {
  const sessionId = req.params.sessionId;
  const historyPath = path.join(require('os').homedir(), '.claude', 'history.jsonl');

  if (!fs.existsSync(historyPath)) {
    return res.json({ messages: [] });
  }

  const lines = fs.readFileSync(historyPath, 'utf-8').split('\n').filter(Boolean);
  const messages = [];

  for (const line of lines) {
    try {
      const entry = JSON.parse(line);
      if (entry.sessionId === sessionId) {
        messages.push({
          role: entry.role || 'user',
          content: entry.display || entry.content || '',
          timestamp: entry.timestamp
        });
      }
    } catch {}
  }

  res.json({ messages });
});

// ========== WebSocket Handler ==========
wss.on('connection', (ws, req) => {
  let authenticated = false;

  ws.on('message', (data) => {
    let msg;
    try {
      msg = JSON.parse(data.toString());
    } catch {
      ws.send(JSON.stringify({ type: 'error', data: 'Invalid message format' }));
      return;
    }

    // Auth handshake
    if (msg.type === 'auth') {
      if (msg.password === PASSWORD) {
        authenticated = true;
        clients.add(ws);
        ws.send(JSON.stringify({ type: 'auth', success: true }));
        // Send current session info
        ws.send(JSON.stringify({ type: 'session_info', sessionId: sharedSessionId }));
        // Send message history for catch-up
        for (const m of messageHistory) {
          ws.send(JSON.stringify(m));
        }
        console.log(`[Auth] Client authenticated (${clients.size} total)`);
      } else {
        ws.send(JSON.stringify({ type: 'auth', success: false, error: 'Wrong password' }));
      }
      return;
    }

    if (!authenticated) {
      ws.send(JSON.stringify({ type: 'error', data: 'Not authenticated' }));
      return;
    }

    // Chat message → spawn Claude
    if (msg.type === 'chat') {
      const userMessage = msg.content;
      if (!userMessage || typeof userMessage !== 'string') return;

      // Handle slash commands locally
      if (userMessage.startsWith('/')) {
        const cmd = userMessage.trim().toLowerCase();

        if (cmd === '/clear' || cmd === '/new') {
          // Clear conversation and start new
          sharedSessionId = null;
          messageHistory = [];
          broadcastAll({ type: 'new_chat' });
          broadcastAll({ type: 'chat_message', role: 'assistant', content: '✅ 已清除对话，开始新会话。' });
          return;
        }

        if (cmd === '/cost') {
          // Show cost info
          const costInfo = `📊 当前会话统计:\n- 输入 tokens: ${totalTokens.input}\n- 输出 tokens: ${totalTokens.output}\n- 缓存读取: ${totalTokens.cacheRead}\n- 总费用: $${totalCost.toFixed(4)}`;
          broadcastAll({ type: 'chat_message', role: 'assistant', content: costInfo });
          return;
        }

        if (cmd === '/help') {
          const helpText = `📖 可用命令:\n\n/clear 或 /new - 清除对话，开始新会话\n/cost - 查看当前会话费用\n/help - 显示此帮助\n/model - 查看当前模型\n/compact - 压缩对话历史\n\n其他以 / 开头的消息会直接发送给 Claude 处理。`;
          broadcastAll({ type: 'chat_message', role: 'assistant', content: helpText });
          return;
        }

        if (cmd === '/model') {
          const modelInfo = `🤖 当前模型: ${currentModel || 'mimo-v2.5-pro'}`;
          broadcastAll({ type: 'chat_message', role: 'assistant', content: modelInfo });
          return;
        }

        if (cmd === '/compact') {
          // Compact conversation - just notify user
          broadcastAll({ type: 'chat_message', role: 'assistant', content: '🗜️ 对话历史已压缩。' });
          return;
        }
      }

      // Kill previous process if still running
      if (claudeProcess) {
        claudeProcess.kill();
        claudeProcess = null;
      }

      console.log(`[Chat] User: ${userMessage.substring(0, 80)}...`);

      // Store user message in history
      const userMsg = { type: 'chat_message', role: 'user', content: userMessage };
      messageHistory.push(userMsg);
      broadcastAll(userMsg);

      // Sync to history.jsonl
      const chatWorkDir = msg.workDir || WORK_DIR;
      appendToHistory(userMessage, msg.sessionId || sharedSessionId, chatWorkDir);

      // Also show in terminal viewers
      broadcastToTerminal(`\r\n\x1b[36m[You]\x1b[0m ${userMessage}\r\n`);
      broadcastToTerminal(`\x1b[33m[Claude]\x1b[0m `);

      // Build args
      const args = [
        '--print',
        '--output-format', 'stream-json',
        '--verbose'
      ];

      if (allowUnsafe) {
        args.push('--dangerously-skip-permissions');
      }

      if (msg.sessionId) {
        args.push('--resume', msg.sessionId);
        sharedSessionId = msg.sessionId; // Update shared session
      } else if (sharedSessionId) {
        args.push('--resume', sharedSessionId);
      }

      args.push(userMessage);

      try {
        console.log(`[Chat] Spawning claude with args: ${JSON.stringify(args)} in ${chatWorkDir}`);
        claudeProcess = spawn('claude', args, {
          cwd: chatWorkDir,
          shell: true,
          env: { ...process.env },
          windowsHide: true
        });

        console.log(`[Chat] Claude process started, PID: ${claudeProcess.pid}`);

        claudeProcess.on('error', (err) => {
          console.error(`[Chat] Claude process error:`, err);
          broadcastAll({ type: 'error', data: `Failed to start Claude: ${err.message}` });
          claudeProcess = null;
        });

        let buffer = '';
        let assistantText = '';
        let chunkCount = 0;

        claudeProcess.stdout.on('data', (chunk) => {
          chunkCount++;
          const chunkStr = chunk.toString();
          console.log(`[Chat] stdout chunk #${chunkCount}: ${chunkStr.substring(0, 200)}`);
          buffer += chunkStr;
          const lines = buffer.split('\n');
          buffer = lines.pop();

          for (const line of lines) {
            if (!line.trim()) continue;
            try {
              const event = JSON.parse(line);

              // Capture session_id
              if (event.session_id) {
                sharedSessionId = event.session_id;
              }

              // Collect assistant text for history
              if (event.type === 'assistant' && event.message?.content) {
                for (const block of event.message.content) {
                  if (block.type === 'text' && block.text) {
                    assistantText += block.text;
                    // Also show in terminal viewers
                    broadcastToTerminal(block.text);
                  }
                }
              }

              // Broadcast to ALL clients
              broadcastAll({ type: 'claude', event });
            } catch {
              broadcastAll({ type: 'claude', event: { type: 'text', text: line } });
            }
          }
        });

        claudeProcess.stderr.on('data', (chunk) => {
          const errStr = chunk.toString();
          console.log(`[Chat] stderr: ${errStr.substring(0, 200)}`);
          broadcastAll({ type: 'stderr', data: errStr });
        });

        claudeProcess.on('close', (code) => {
          // Flush remaining buffer
          if (buffer.trim()) {
            try {
              const event = JSON.parse(buffer);
              if (event.session_id) sharedSessionId = event.session_id;
              broadcastAll({ type: 'claude', event });
            } catch {
              broadcastAll({ type: 'claude', event: { type: 'text', text: buffer } });
            }
          }

          // Store assistant response in history
          if (assistantText) {
            messageHistory.push({ type: 'chat_message', role: 'assistant', content: assistantText });
          }

          // Add newline in terminal after response
          broadcastToTerminal('\r\n');

          broadcastAll({ type: 'done', code });
          claudeProcess = null;
          console.log(`[Chat] Claude finished (exit ${code})`);
        });

        claudeProcess.on('error', (err) => {
          broadcastAll({ type: 'error', data: `Failed to start Claude: ${err.message}` });
          claudeProcess = null;
        });

      } catch (err) {
        ws.send(JSON.stringify({ type: 'error', data: err.message }));
      }
    }

    // Stop current generation
    if (msg.type === 'stop' && claudeProcess) {
      claudeProcess.kill();
      claudeProcess = null;
      broadcastAll({ type: 'stopped' });
    }

    // New chat
    if (msg.type === 'new_chat') {
      sharedSessionId = null;
      messageHistory = [];
      broadcastAll({ type: 'new_chat' });
    }

    // Terminal mode: start PTY
    if (msg.type === 'terminal_start') {
      terminalClients.add(ws);
      if (!ptyProcess) {
        startPty(msg.cols || 80, msg.rows || 24, msg.mode);
      }
      ws.send(JSON.stringify({ type: 'terminal_started' }));
      console.log(`[Terminal] Client started terminal view (${terminalClients.size} viewers)`);
    }

    // Terminal mode: user input
    if (msg.type === 'terminal_input') {
      writeToPty(msg.data);

      // Buffer user input and store complete messages
      for (const char of msg.data) {
        if (char === '\r' || char === '\n') {
          if (terminalInputBuffer.trim()) {
            const userMsg = { type: 'chat_message', role: 'user', content: terminalInputBuffer.trim() };
            messageHistory.push(userMsg);
            broadcastAll(userMsg);
          }
          terminalInputBuffer = '';
        } else if (char === '\x7f' || char === '\b') {
          // Backspace
          terminalInputBuffer = terminalInputBuffer.slice(0, -1);
        } else {
          terminalInputBuffer += char;
        }
      }
    }

    // Terminal mode: resize
    if (msg.type === 'terminal_resize') {
      resizePty(msg.cols, msg.rows);
    }

    // Terminal mode: stop PTY
    if (msg.type === 'terminal_stop') {
      terminalClients.delete(ws);
      if (terminalClients.size === 0 && ptyProcess) {
        stopPty();
      }
      console.log(`[Terminal] Client stopped terminal view (${terminalClients.size} viewers)`);
    }
  });

  ws.on('close', () => {
    if (authenticated) {
      clients.delete(ws);
      terminalClients.delete(ws);
      console.log(`[WS] Client disconnected (${clients.size} remaining, ${terminalClients.size} terminal)`);
    }
  });
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`\n🚀 Vibe Coding Remote Server`);
  console.log(`   Local:   http://localhost:${PORT}`);
  console.log(`   Network: http://0.0.0.0:${PORT}`);
  console.log(`   Password: ${PASSWORD}`);
  console.log(`   Work Dir: ${WORK_DIR}\n`);
});
