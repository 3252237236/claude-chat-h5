(() => {
  // ========== DOM Elements ==========
  const authScreen = document.getElementById('auth-screen');
  const chatScreen = document.getElementById('chat-screen');
  const passwordInput = document.getElementById('password-input');
  const connectBtn = document.getElementById('connect-btn');
  const authError = document.getElementById('auth-error');
  const messagesEl = document.getElementById('messages');
  const messageInput = document.getElementById('message-input');
  const sendBtn = document.getElementById('send-btn');
  const stopBtn = document.getElementById('stop-btn');
  const newChatBtn = document.getElementById('new-chat-btn');
  const statusDot = document.getElementById('status-dot');
  const statusText = document.getElementById('status-text');
  const sessionsBtn = document.getElementById('sessions-btn');
  const sessionsPanel = document.getElementById('sessions-panel');
  const sessionsList = document.getElementById('sessions-list');
  const sessionsCloseBtn = document.getElementById('sessions-close-btn');
  const modeBtn = document.getElementById('mode-btn');

  // ========== Base Path ==========
  // When served behind /vibe/ proxy, adjust paths
  const BASE_PATH = location.pathname.includes('/vibe') ? '/vibe' : '';

  // ========== State ==========
  let ws = null;
  let currentAssistantEl = null;
  let currentBubble = null;
  let streamingText = '';
  let isGenerating = false;
  let receivedStreaming = false;
  let sessionId = localStorage.getItem('vibe-session-id') || null;
  let currentModel = '';
  let lastSentContent = null;
  let lastSentTime = 0;
  let turnStartTime = 0;
  let totalTokens = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };
  let totalCost = 0;

  // Multi-session state
  let currentProject = '';
  const projectSessions = new Map();

  // ========== Markdown setup ==========
  marked.setOptions({
    breaks: true,
    gfm: true,
    highlight: function(code, lang) {
      return code;
    }
  });

  // ========== Auth ==========
  connectBtn.addEventListener('click', connect);
  passwordInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') connect();
  });

  function connect() {
    const password = passwordInput.value.trim();
    if (!password) {
      authError.textContent = '请输入密码';
      return;
    }

    connectBtn.disabled = true;
    connectBtn.textContent = '连接中...';
    authError.textContent = '';

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${location.host}${BASE_PATH}/ws`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'auth', password }));
    };

    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      handleMessage(msg);
    };

    ws.onclose = () => {
      setStatus('disconnected', 'Disconnected');
      if (isGenerating) {
        isGenerating = false;
        updateUI();
      }
    };

    ws.onerror = () => {
      authError.textContent = '连接失败，请检查地址和密码';
      connectBtn.disabled = false;
      connectBtn.textContent = '连接';
    };
  }

  function handleMessage(msg) {
    switch (msg.type) {
      case 'auth':
        if (msg.success) {
          authScreen.classList.add('hidden');
          chatScreen.classList.remove('hidden');
          setStatus('connected', 'Connected');
          messageInput.focus();
          addSystemMessage('已连接到远程 Claude Code');
          fetchMode();
        } else {
          authError.textContent = msg.error || '密码错误';
          connectBtn.disabled = false;
          connectBtn.textContent = '连接';
        }
        break;

      case 'session_info':
        if (msg.sessionId) {
          sessionId = msg.sessionId;
          localStorage.setItem('vibe-session-id', sessionId);
        }
        break;

      case 'chat_message':
        if (msg.role === 'user') {
          const isOwn = lastSentContent && msg.content === lastSentContent
            && (Date.now() - lastSentTime < 5000);
          if (!isOwn) {
            addUserMessage(msg.content);
          }
          lastSentContent = null;
          if (!isGenerating) {
            isGenerating = true;
            turnStartTime = Date.now();
            setStatus('thinking', 'Thinking...');
            updateUI();
          }
        } else if (msg.role === 'assistant') {
          addAssistantMessage(msg.content);
        }
        break;

      case 'new_chat':
        messagesEl.innerHTML = '';
        currentAssistantEl = null;
        currentBubble = null;
        streamingText = '';
        sessionId = null;
        localStorage.removeItem('vibe-session-id');
        totalTokens = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };
        totalCost = 0;
        addSystemMessage('已开始新对话');
        break;

      case 'claude':
        processClaudeEvent(msg.event);
        break;

      case 'stderr':
        if (msg.data && !msg.data.includes('DeprecationWarning')) {
          console.warn('[Claude stderr]', msg.data);
        }
        break;

      case 'done':
        finishGeneration();
        break;

      case 'stopped':
        addSystemMessage('已停止生成');
        finishGeneration();
        break;

      case 'error':
        addSystemMessage(`❌ ${msg.data}`);
        finishGeneration();
        break;
    }
  }

  // ========== Claude Event Processing ==========
  function processClaudeEvent(event) {
    if (!event) return;

    switch (event.type) {
      case 'content_block_start':
        if (event.content_block?.type === 'text') {
          ensureAssistantMessage();
        }
        if (event.content_block?.type === 'tool_use') {
          showToolUse(event.content_block);
        }
        break;

      case 'content_block_delta':
        if (event.delta?.type === 'text_delta' && event.delta.text) {
          receivedStreaming = true;
          appendText(event.delta.text);
        }
        break;

      case 'content_block_stop':
        break;

      case 'message_start':
        if (!isGenerating) {
          isGenerating = true;
          turnStartTime = Date.now();
          setStatus('thinking', 'Thinking...');
          updateUI();
        }
        break;

      case 'message_stop':
        break;

      case 'assistant':
        if (event.message?.content) {
          ensureAssistantMessage();
          let fullText = '';
          for (const block of event.message.content) {
            if (block.type === 'text' && block.text) {
              fullText += block.text;
            }
            if (block.type === 'tool_use') {
              showToolUse(block);
            }
          }
          if (fullText && !receivedStreaming) {
            const newText = fullText.substring(streamingText.length);
            if (newText) {
              streamingText += newText;
              const rawHtml = marked.parse(streamingText);
              currentBubble.innerHTML = DOMPurify.sanitize(rawHtml);
              scrollToBottom();
            }
          }
        }
        break;

      case 'system':
        if (event.subtype === 'init') {
          if (event.session_id) {
            sessionId = event.session_id;
            localStorage.setItem('vibe-session-id', sessionId);
          }
          if (event.model) currentModel = event.model;
        }
        break;

      case 'result':
        if (event.result && !receivedStreaming) {
          ensureAssistantMessage();
          appendText(event.result);
        }
        if (event.session_id) {
          sessionId = event.session_id;
          localStorage.setItem('vibe-session-id', sessionId);
        }

        const usage = event.usage || {};
        const turnInput = usage.input_tokens || 0;
        const turnOutput = usage.output_tokens || 0;
        const turnCacheRead = usage.cache_read_input_tokens || 0;
        const turnCacheWrite = usage.cache_creation_input_tokens || 0;
        const turnCost = event.total_cost_usd || 0;
        const duration = event.duration_ms || 0;
        const apiDuration = event.duration_api_ms || 0;
        const ttft = event.ttft_ms || 0;

        totalTokens.input += turnInput;
        totalTokens.output += turnOutput;
        totalTokens.cacheRead += turnCacheRead;
        totalTokens.cacheWrite += turnCacheWrite;
        totalCost += turnCost;

        if (currentAssistantEl) {
          showMetadata({
            input: turnInput,
            output: turnOutput,
            cacheRead: turnCacheRead,
            cacheWrite: turnCacheWrite,
            cost: turnCost,
            duration: duration,
            apiDuration: apiDuration,
            ttft: ttft,
            model: event.model || currentModel,
            stopReason: event.stop_reason
          });
        }

        const durStr = duration > 1000 ? (duration / 1000).toFixed(1) + 's' : duration + 'ms';
        setStatus('connected', `${turnOutput} tokens · $${turnCost.toFixed(4)} · ${durStr}`);

        finishGeneration();
        break;
    }
  }

  // ========== UI Helpers ==========
  function ensureAssistantMessage() {
    if (currentAssistantEl) return;

    const msgEl = document.createElement('div');
    msgEl.className = 'message assistant';
    msgEl.innerHTML = `
      <div class="role">Claude</div>
      <div class="bubble"></div>
    `;
    messagesEl.appendChild(msgEl);
    currentAssistantEl = msgEl;
    currentBubble = msgEl.querySelector('.bubble');
    streamingText = '';
    scrollToBottom();
  }

  function appendText(text) {
    ensureAssistantMessage();
    streamingText += text;
    const rawHtml = marked.parse(streamingText);
    currentBubble.innerHTML = DOMPurify.sanitize(rawHtml);
    scrollToBottom();
  }

  function showToolUse(toolBlock) {
    ensureAssistantMessage();
    const toolEl = document.createElement('div');
    toolEl.className = 'tool-use collapsed';

    const name = toolBlock.name || 'tool';
    const input = toolBlock.input || {};
    const summary = getToolSummary(name, input);

    toolEl.innerHTML = `
      <div class="tool-use-header">${name}: ${summary}</div>
      <div class="tool-use-content">${escapeHtml(JSON.stringify(input, null, 2))}</div>
    `;

    toolEl.querySelector('.tool-use-header').addEventListener('click', () => {
      toolEl.classList.toggle('collapsed');
    });

    currentBubble.appendChild(toolEl);
    scrollToBottom();
  }

  function showMetadata(stats) {
    if (!currentAssistantEl) return;

    const metaEl = document.createElement('div');
    metaEl.className = 'msg-meta';

    const durStr = stats.duration > 1000
      ? (stats.duration / 1000).toFixed(1) + 's'
      : stats.duration + 'ms';

    const ttftStr = stats.ttft > 1000
      ? (stats.ttft / 1000).toFixed(1) + 's'
      : stats.ttft + 'ms';

    metaEl.innerHTML = `
      <span class="meta-item" title="输入 tokens">📥 ${formatNumber(stats.input)}</span>
      <span class="meta-item" title="输出 tokens">📤 ${formatNumber(stats.output)}</span>
      ${stats.cacheRead ? `<span class="meta-item meta-cache" title="缓存读取">💾 ${formatNumber(stats.cacheRead)}</span>` : ''}
      <span class="meta-item meta-cost" title="费用">💰 $${stats.cost.toFixed(4)}</span>
      <span class="meta-item meta-time" title="首 token: ${ttftStr} / 总耗时: ${durStr}">⏱ ${durStr}</span>
      ${stats.model ? `<span class="meta-item meta-model" title="${stats.model}">🤖 ${stats.model.split('/').pop()}</span>` : ''}
      ${stats.stopReason && stats.stopReason !== 'end_turn' ? `<span class="meta-item meta-warn">⚠ ${stats.stopReason}</span>` : ''}
    `;

    currentAssistantEl.appendChild(metaEl);
    scrollToBottom();
  }

  function formatNumber(n) {
    if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
    return n.toString();
  }

  function getToolSummary(name, input) {
    switch (name) {
      case 'read': return input.file_path || '';
      case 'write': return input.file_path || '';
      case 'edit': return input.file_path || '';
      case 'bash': return (input.command || '').substring(0, 60);
      case 'grep': return input.pattern || '';
      case 'glob': return input.pattern || '';
      default: return JSON.stringify(input).substring(0, 60);
    }
  }

  function finishGeneration() {
    isGenerating = false;
    receivedStreaming = false;
    currentAssistantEl = null;
    currentBubble = null;
    streamingText = '';
    updateUI();
  }

  function addSystemMessage(text) {
    const msgEl = document.createElement('div');
    msgEl.className = 'message system';
    msgEl.innerHTML = `
      <div class="role">System</div>
      <div class="bubble">${escapeHtml(text)}</div>
    `;
    messagesEl.appendChild(msgEl);
    scrollToBottom();
  }

  function addUserMessage(text) {
    const msgEl = document.createElement('div');
    msgEl.className = 'message user';
    msgEl.innerHTML = `
      <div class="role">You</div>
      <div class="bubble">${escapeHtml(text)}</div>
    `;
    messagesEl.appendChild(msgEl);
    scrollToBottom();
  }

  function addAssistantMessage(text) {
    const msgEl = document.createElement('div');
    msgEl.className = 'message assistant';
    const rawHtml = marked.parse(text);
    msgEl.innerHTML = `
      <div class="role">Claude</div>
      <div class="bubble">${DOMPurify.sanitize(rawHtml)}</div>
    `;
    messagesEl.appendChild(msgEl);
    scrollToBottom();
  }

  function setStatus(state, text) {
    statusDot.className = 'status-dot';
    if (state === 'thinking') statusDot.classList.add('thinking');
    if (state === 'disconnected') statusDot.classList.add('disconnected');
    statusText.textContent = text;
  }

  function updateUI() {
    stopBtn.classList.toggle('hidden', !isGenerating);
    sendBtn.disabled = isGenerating;
  }

  function scrollToBottom() {
    requestAnimationFrame(() => {
      messagesEl.scrollTop = messagesEl.scrollHeight;
    });
  }

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  // ========== Send Message ==========
  let lastSendTime = 0;

  function sendMessage() {
    const text = messageInput.value.trim();
    if (!text || isGenerating) return;

    const now = Date.now();
    if (now - lastSendTime < 500) return;
    lastSendTime = now;

    isGenerating = true;
    addUserMessage(text);
    lastSentContent = text;
    lastSentTime = Date.now();
    turnStartTime = Date.now();

    const msg = { type: 'chat', content: text };
    if (sessionId) msg.sessionId = sessionId;
    if (currentProject) msg.workDir = currentProject;
    ws.send(JSON.stringify(msg));

    messageInput.value = '';
    messageInput.style.height = 'auto';
    setStatus('thinking', 'Thinking...');
    updateUI();
  }

  sendBtn.addEventListener('click', sendMessage);
  messageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  messageInput.addEventListener('input', () => {
    messageInput.style.height = 'auto';
    messageInput.style.height = Math.min(messageInput.scrollHeight, 120) + 'px';
  });

  stopBtn.addEventListener('click', () => {
    if (ws && isGenerating) {
      ws.send(JSON.stringify({ type: 'stop' }));
    }
  });

  newChatBtn.addEventListener('click', () => {
    if (ws) {
      ws.send(JSON.stringify({ type: 'new_chat' }));
    }
  });

  // ========== Sessions Panel ==========
  sessionsBtn.addEventListener('click', () => {
    sessionsPanel.classList.toggle('hidden');
    if (!sessionsPanel.classList.contains('hidden')) {
      loadSessions();
    }
  });

  sessionsCloseBtn.addEventListener('click', () => {
    sessionsPanel.classList.add('hidden');
  });

  const searchInput = document.getElementById('sessions-search-input');
  let searchTimeout = null;

  if (searchInput) {
    searchInput.addEventListener('input', () => {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(() => {
        loadSessions(searchInput.value.trim());
      }, 300);
    });

    searchInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        clearTimeout(searchTimeout);
        loadSessions(searchInput.value.trim());
      }
    });
  }

  async function loadSessions(query) {
    sessionsList.innerHTML = '<div class="sessions-loading">加载中...</div>';
    try {
      const url = query
        ? `${BASE_PATH}/api/sessions?q=${encodeURIComponent(query)}`
        : `${BASE_PATH}/api/sessions`;
      const resp = await fetch(url);
      const sessions = await resp.json();
      if (sessions.currentDir) {
        localStorage.setItem('vibe-current-dir', sessions.currentDir);
      }
      renderSessions(sessions);
    } catch {
      sessionsList.innerHTML = '<div class="sessions-loading">加载失败</div>';
    }
  }

  function renderSessions(data) {
    const projects = data.projects || [];
    const totalProjects = data.totalProjects || 0;

    if (!projects.length) {
      sessionsList.innerHTML = '<div class="sessions-loading">暂无历史会话</div>';
      return;
    }

    sessionsList.innerHTML = '';

    if (totalProjects > projects.length) {
      const countEl = document.createElement('div');
      countEl.className = 'sessions-count';
      countEl.textContent = `显示 ${projects.length}/${totalProjects} 个项目`;
      sessionsList.appendChild(countEl);
    }

    for (const proj of projects) {
      const groupEl = document.createElement('div');
      groupEl.className = 'project-group';

      const headerEl = document.createElement('div');
      headerEl.className = 'project-header';

      let headerHtml = `
        <div class="project-header-content">
          <span class="project-name">${escapeHtml(proj.displayName)}</span>
          <span class="project-path">${escapeHtml(proj.project)}</span>
        </div>
        <div class="project-header-meta">
          ${proj.hasMemory ? '<span class="memory-badge" title="有记忆文件">🧠</span>' : ''}
          <span class="project-count">${proj.sessions.length} 个会话</span>
          <span class="project-arrow">▸</span>
        </div>
      `;
      headerEl.innerHTML = headerHtml;

      const listEl = document.createElement('div');
      listEl.className = 'project-sessions collapsed';

      if (proj.hasMemory && proj.memoryFiles && proj.memoryFiles.length > 0) {
        const memoryEl = document.createElement('div');
        memoryEl.className = 'memory-files';
        memoryEl.innerHTML = '<div class="memory-title">📁 记忆文件</div>';
        for (const file of proj.memoryFiles) {
          const fileEl = document.createElement('div');
          fileEl.className = 'memory-file';
          fileEl.textContent = file.replace('.md', '');
          memoryEl.appendChild(fileEl);
        }
        listEl.appendChild(memoryEl);
      }

      if (proj.sessions.length > 0) {
        for (const s of proj.sessions) {
          const item = document.createElement('div');
          item.className = 'session-item' + (s.sessionId === sessionId ? ' active' : '');

          const timeStr = formatTime(s.lastTimestamp);
          const preview = s.lastMessage.length > 50 ? s.lastMessage.slice(0, 50) + '...' : s.lastMessage;

          item.innerHTML = `
            <div class="session-preview">${escapeHtml(preview)}</div>
            <div class="session-meta">
              <span>${s.messageCount} 条</span>
              <span>${timeStr}</span>
            </div>
          `;

          item.addEventListener('click', () => {
            resumeSession(s.sessionId, preview, s.project);
          });

          listEl.appendChild(item);
        }
      } else {
        const emptyEl = document.createElement('div');
        emptyEl.className = 'session-empty';
        emptyEl.textContent = '暂无会话记录';
        listEl.appendChild(emptyEl);
      }

      headerEl.addEventListener('click', () => {
        listEl.classList.toggle('collapsed');
        headerEl.querySelector('.project-arrow').textContent =
          listEl.classList.contains('collapsed') ? '▸' : '▾';
      });

      groupEl.appendChild(headerEl);
      groupEl.appendChild(listEl);
      sessionsList.appendChild(groupEl);
    }
  }

  function resumeSession(sid, preview, project) {
    if (currentProject) {
      saveCurrentProjectMessages();
    }

    currentProject = project || '';
    sessionId = sid;
    localStorage.setItem('vibe-session-id', sid);
    localStorage.setItem('vibe-current-project', currentProject);

    if (projectSessions.has(currentProject)) {
      restoreProjectMessages(currentProject);
    } else {
      messagesEl.innerHTML = '';
      currentAssistantEl = null;
      currentBubble = null;
      streamingText = '';

      addSystemMessage(`✅ 会话已恢复，上下文已保留`);
      if (project) {
        addSystemMessage(`📁 项目: ${project.split(/[/\\]/).pop()}`);
      }
    }

    sessionsPanel.classList.add('hidden');
  }

  function saveCurrentProjectMessages() {
    if (!currentProject) return;
    const messages = messagesEl.innerHTML;
    projectSessions.set(currentProject, {
      sessionId: sessionId,
      messages: messages
    });
  }

  function restoreProjectMessages(project) {
    const data = projectSessions.get(project);
    if (data) {
      messagesEl.innerHTML = data.messages;
      sessionId = data.sessionId;
      scrollToBottom();
    }
  }

  function formatTime(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    const now = new Date();
    const diff = now - d;

    if (diff < 60000) return '刚刚';
    if (diff < 3600000) return Math.floor(diff / 60000) + ' 分钟前';
    if (diff < 86400000) return Math.floor(diff / 3600000) + ' 小时前';
    if (diff < 604800000) return Math.floor(diff / 86400000) + ' 天前';
    return d.toLocaleDateString('zh-CN');
  }

  // ========== Mode Toggle ==========
  let currentModeUnsafe = false;

  async function fetchMode() {
    try {
      const resp = await fetch(`${BASE_PATH}/api/mode`);
      const data = await resp.json();
      currentModeUnsafe = data.allowUnsafe;
      updateModeBtn();
    } catch {}
  }

  function updateModeBtn() {
    if (currentModeUnsafe) {
      modeBtn.textContent = '⚡ Unsafe';
      modeBtn.className = 'mode-btn mode-unsafe';
      modeBtn.title = '点击切换为 Safe 模式（需确认）';
    } else {
      modeBtn.textContent = '🛡️ Safe';
      modeBtn.className = 'mode-btn mode-safe';
      modeBtn.title = '点击切换为 Unsafe 模式（跳过确认）';
    }
  }

  modeBtn.addEventListener('click', async () => {
    const newVal = !currentModeUnsafe;
    try {
      const resp = await fetch(`${BASE_PATH}/api/mode`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ allowUnsafe: newVal })
      });
      const data = await resp.json();
      currentModeUnsafe = data.allowUnsafe;
      updateModeBtn();
      addSystemMessage(currentModeUnsafe ? '已切换为 Unsafe 模式（工具调用不再弹确认）' : '已切换为 Safe 模式（工具调用需确认）');
    } catch {
      addSystemMessage('❌ 切换模式失败');
    }
  });

  // ========== Connection state persistence ==========
  const savedPassword = sessionStorage.getItem('vibe-password');
  if (savedPassword) {
    passwordInput.value = savedPassword;
  }

  const originalConnect = connect;
  connectBtn.removeEventListener('click', connect);

  connectBtn.addEventListener('click', () => {
    sessionStorage.setItem('vibe-password', passwordInput.value);
    connect();
  });
})();
