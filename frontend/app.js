/**
 * MiniCode 前端交互逻辑
 * WebSocket 连接 + 消息渲染 + 工具卡片 + 审批流
 * 严格遵循 DESIGN.md §10 WebSocket 协议 + §15 UI 交互规范
 */

// ══════════════════════════════════════════════════════════════
//  状态管理
// ══════════════════════════════════════════════════════════════

const state = {
    ws: null,
    connected: false,
    streaming: false,
    currentAssistantEl: null,   // 当前正在流式渲染的助手消息元素
    currentTextEl: null,        // 当前文本容器
    currentToolCalls: [],
    iterCount: 0,
    toolCallCount: 0,
    artifactCount: 0,
    sidebarVisible: true,
};

// ══════════════════════════════════════════════════════════════
//  DOM 引用
// ══════════════════════════════════════════════════════════════

const $ = (sel) => document.querySelector(sel);
const messageList = $('#messageList');
const userInput = $('#userInput');
const btnSend = $('#btnSend');
const btnSidebar = $('#btnSidebar');
const contextSidebar = $('#contextSidebar');
const welcomeScreen = $('#welcomeScreen');

// ══════════════════════════════════════════════════════════════
//  WebSocket 连接
// ══════════════════════════════════════════════════════════════

function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${location.host}/ws`;

    state.ws = new WebSocket(wsUrl);

    state.ws.onopen = () => {
        state.connected = true;
        console.log('[WS] Connected');
    };

    state.ws.onclose = () => {
        state.connected = false;
        console.log('[WS] Disconnected, reconnecting in 3s...');
        setTimeout(connectWebSocket, 3000);
    };

    state.ws.onerror = (err) => {
        console.error('[WS] Error:', err);
    };

    state.ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            handleServerEvent(msg);
        } catch (e) {
            console.error('[WS] Parse error:', e);
        }
    };
}

// ══════════════════════════════════════════════════════════════
//  发送消息
// ══════════════════════════════════════════════════════════════

function sendMessage() {
    const text = userInput.value.trim();
    if (!text || state.streaming) return;

    // 隐藏欢迎屏
    if (welcomeScreen) {
        welcomeScreen.style.display = 'none';
    }

    // 添加用户消息气泡
    appendUserMessage(text);

    // 发送 WebSocket 消息
    if (state.ws && state.connected) {
        state.ws.send(JSON.stringify({
            type: 'user_message',
            content: text,
        }));
    }

    // 清空输入框
    userInput.value = '';
    autoResizeInput();

    // 准备助手回复区域
    startAssistantMessage();
    state.streaming = true;
    updateSendButton();
}

function interruptGeneration() {
    if (state.ws && state.connected) {
        state.ws.send(JSON.stringify({ type: 'interrupt' }));
    }
    state.streaming = false;
    updateSendButton();
    removeCursor();
}

// ══════════════════════════════════════════════════════════════
//  服务器事件处理（DESIGN.md §10）
// ══════════════════════════════════════════════════════════════

function handleServerEvent(msg) {
    switch (msg.type) {
        case 'text_chunk':
            handleTextChunk(msg.content);
            break;
        case 'tool_call':
            handleToolCall(msg);
            break;
        case 'tool_result':
            handleToolResult(msg);
            break;
        case 'approval_request':
            handleApprovalRequest(msg);
            break;
        case 'done':
            handleDone(msg);
            break;
        case 'error':
            handleError(msg);
            break;
        case 'context_compacted':
            showToast(`📦 ${msg.summary}`);
            break;
        case 'skill_activated':
            handleSkillActivated(msg);
            break;
    }
}

function handleTextChunk(content) {
    if (!state.currentTextEl) return;
    state.currentTextEl.textContent += content;
    scrollToBottom();
}

function handleToolCall(msg) {
    state.toolCallCount++;
    $('#toolCallCount').textContent = state.toolCallCount;

    const card = createToolCallCard(msg.id, msg.name, msg.args);
    if (state.currentAssistantEl) {
        state.currentAssistantEl.querySelector('.assistant-content').appendChild(card);
    }
    scrollToBottom();
}

function handleToolResult(msg) {
    const card = document.getElementById(`tool-${msg.id}`);
    if (card) {
        // 更新状态
        const statusEl = card.querySelector('.tool-status');
        statusEl.className = 'tool-status success';
        statusEl.innerHTML = '✓';

        // 添加结果预览
        const body = card.querySelector('.tool-body');
        body.textContent = msg.summary || '(无输出)';

        // artifact 链接
        if (msg.artifact_id) {
            state.artifactCount++;
            $('#artifactCount').textContent = state.artifactCount;
            const link = document.createElement('div');
            link.className = 'artifact-link';
            link.textContent = `📄 查看完整 artifact →`;
            body.appendChild(link);
        }
    }
    scrollToBottom();
}

function handleApprovalRequest(msg) {
    const overlay = $('#approvalOverlay');
    const modal = $('#approvalModal');
    overlay.style.display = 'flex';

    let diffHtml = '';
    if (msg.diff) {
        diffHtml = `<div class="diff-view">${renderDiff(msg.diff)}</div>`;
    }

    modal.innerHTML = `
        <div class="approval-card" style="max-width:600px;width:100%;">
            <div class="approval-header">⚠ 需要审批：${msg.tool_name}</div>
            <div style="padding:var(--space-3);font-size:var(--text-sm);color:var(--text-secondary);">
                ${JSON.stringify(msg.args, null, 2)}
            </div>
            ${diffHtml}
            <div class="approval-actions">
                <button class="btn-approve" onclick="handleApproval('${msg.tool_call_id}', 'approve')">✓ 批准</button>
                <button class="btn-reject" onclick="handleApproval('${msg.tool_call_id}', 'reject')">✗ 拒绝</button>
                <button class="btn-reject-guide" onclick="handleApprovalWithGuide('${msg.tool_call_id}')">✎ 带意见拒绝</button>
            </div>
        </div>
    `;
}

function handleApproval(toolCallId, action, guidance) {
    if (state.ws && state.connected) {
        const msg = {
            type: 'approval',
            tool_call_id: toolCallId,
            action: action,
        };
        if (guidance) msg.guidance = guidance;
        state.ws.send(JSON.stringify(msg));
    }
    $('#approvalOverlay').style.display = 'none';
}

function handleApprovalWithGuide(toolCallId) {
    const guidance = prompt('请输入拒绝原因或修改建议：');
    if (guidance !== null) {
        handleApproval(toolCallId, 'reject', guidance);
    }
}

function handleDone(msg) {
    state.streaming = false;
    state.iterCount++;
    $('#iterCount').textContent = state.iterCount;

    removeCursor();
    updateSendButton();

    // 添加元数据
    if (state.currentAssistantEl && msg.usage) {
        const meta = state.currentAssistantEl.querySelector('.assistant-meta');
        if (meta) {
            const now = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
            const tokens = (msg.usage.input_tokens || 0) + (msg.usage.output_tokens || 0);
            meta.textContent = `${now} · ${tokens > 0 ? (tokens / 1000).toFixed(1) + 'k tokens' : ''}`;
        }
    }

    // 渲染 Markdown
    if (state.currentTextEl) {
        state.currentTextEl.innerHTML = renderMarkdown(state.currentTextEl.textContent);
    }

    state.currentAssistantEl = null;
    state.currentTextEl = null;
    scrollToBottom();
}

function handleError(msg) {
    state.streaming = false;
    updateSendButton();
    removeCursor();

    const errorDiv = document.createElement('div');
    errorDiv.className = 'error-card';
    errorDiv.innerHTML = `⚠ ${msg.message || '未知错误'}`;

    if (state.currentAssistantEl) {
        state.currentAssistantEl.querySelector('.assistant-content').appendChild(errorDiv);
    } else {
        messageList.appendChild(errorDiv);
    }

    state.currentAssistantEl = null;
    state.currentTextEl = null;
    scrollToBottom();
}

function handleSkillActivated(msg) {
    showToast(`⚡ 已激活 Skill: ${msg.skill_name}`);
}

// ══════════════════════════════════════════════════════════════
//  UI 渲染
// ══════════════════════════════════════════════════════════════

function appendUserMessage(text) {
    const el = document.createElement('div');
    el.className = 'message message-user';
    const now = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    el.innerHTML = `
        <div>
            <div class="user-bubble">${escapeHtml(text)}</div>
            <div class="msg-time" style="text-align:right">${now}</div>
        </div>
        <div class="user-avatar">U</div>
    `;
    messageList.appendChild(el);
    scrollToBottom();
}

function startAssistantMessage() {
    const el = document.createElement('div');
    el.className = 'message message-assistant';
    el.innerHTML = `
        <div class="assistant-avatar">◆</div>
        <div class="assistant-content">
            <div class="assistant-name">MiniCode</div>
            <div class="assistant-text"><span class="streaming-cursor"></span></div>
            <div class="assistant-meta"></div>
        </div>
    `;
    messageList.appendChild(el);
    state.currentAssistantEl = el;
    state.currentTextEl = el.querySelector('.assistant-text');
    scrollToBottom();
}

function createToolCallCard(id, name, args) {
    const card = document.createElement('div');
    card.className = 'tool-call-card';
    card.id = `tool-${id}`;

    let argsPreview = '';
    if (args) {
        const first = Object.entries(args)[0];
        if (first) {
            let val = String(first[1]);
            if (val.length > 40) val = val.substring(0, 40) + '…';
            argsPreview = val;
        }
    }

    card.innerHTML = `
        <div class="tool-header" onclick="this.parentElement.querySelector('.tool-body').classList.toggle('open')">
            <div class="tool-header-left">
                <span class="tool-icon">▶</span>
                <span class="tool-name">${escapeHtml(name)}</span>
                <span class="tool-args-preview">${escapeHtml(argsPreview)}</span>
            </div>
            <span class="tool-status running"><span class="spinner">⟳</span></span>
        </div>
        <div class="tool-body"></div>
    `;

    return card;
}

function renderDiff(diff) {
    if (!diff) return '';
    return diff.split('\n').map(line => {
        if (line.startsWith('+') && !line.startsWith('+++')) {
            return `<div class="diff-line-add">${escapeHtml(line)}</div>`;
        } else if (line.startsWith('-') && !line.startsWith('---')) {
            return `<div class="diff-line-remove">${escapeHtml(line)}</div>`;
        } else if (line.startsWith('@@')) {
            return `<div class="diff-line-header">${escapeHtml(line)}</div>`;
        }
        return `<div class="diff-line-context">${escapeHtml(line)}</div>`;
    }).join('');
}

/** 简单 Markdown 渲染 */
function renderMarkdown(text) {
    if (!text) return '';
    let html = escapeHtml(text);

    // 代码块
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
        return `<pre><code>${code}</code>${lang ? `<span class="code-lang">${lang}</span>` : ''}</pre>`;
    });

    // 行内代码
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

    // 加粗
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

    // 斜体
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');

    // 链接
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" style="color:var(--link)">$1</a>');

    // 标题
    html = html.replace(/^### (.+)$/gm, '<h3 style="font-size:15px;margin:12px 0 8px;color:var(--text-primary)">$1</h3>');
    html = html.replace(/^## (.+)$/gm, '<h2 style="font-size:16px;margin:16px 0 8px;color:var(--text-primary)">$1</h2>');
    html = html.replace(/^# (.+)$/gm, '<h1 style="font-size:18px;margin:20px 0 12px;color:var(--text-primary)">$1</h1>');

    // 列表
    html = html.replace(/^- (.+)$/gm, '<li style="margin-left:16px;list-style:disc">$1</li>');

    // 段落
    html = html.replace(/\n\n/g, '</p><p>');
    html = '<p>' + html + '</p>';
    html = html.replace(/<p><\/p>/g, '');

    return html;
}

function removeCursor() {
    const cursors = document.querySelectorAll('.streaming-cursor');
    cursors.forEach(c => c.remove());
}

// ══════════════════════════════════════════════════════════════
//  工具函数
// ══════════════════════════════════════════════════════════════

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function scrollToBottom() {
    requestAnimationFrame(() => {
        messageList.scrollTop = messageList.scrollHeight;
    });
}

function updateSendButton() {
    if (state.streaming) {
        btnSend.innerHTML = '<span>■</span>';
        btnSend.classList.add('interrupt-btn');
        btnSend.onclick = interruptGeneration;
    } else {
        btnSend.innerHTML = '<span class="send-icon">↵</span>';
        btnSend.classList.remove('interrupt-btn');
        btnSend.onclick = sendMessage;
    }
}

function autoResizeInput() {
    userInput.style.height = 'auto';
    userInput.style.height = Math.min(userInput.scrollHeight, 150) + 'px';
}

function showToast(text) {
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = text;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
}

// ══════════════════════════════════════════════════════════════
//  事件绑定
// ══════════════════════════════════════════════════════════════

// 发送
btnSend.addEventListener('click', sendMessage);

// Enter 发送, Shift+Enter 换行
userInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (state.streaming) {
            interruptGeneration();
        } else {
            sendMessage();
        }
    }
});

// 自动调整输入框高度
userInput.addEventListener('input', autoResizeInput);

// 侧边栏切换
btnSidebar.addEventListener('click', () => {
    state.sidebarVisible = !state.sidebarVisible;
    if (state.sidebarVisible) {
        contextSidebar.classList.remove('collapsed');
    } else {
        contextSidebar.classList.add('collapsed');
    }
});

// ══════════════════════════════════════════════════════════════
//  初始化
// ══════════════════════════════════════════════════════════════

connectWebSocket();
userInput.focus();
