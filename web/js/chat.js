/**
 * Spirit Agent Dashboard — 聊天界面
 */

const ChatPage = {
    // 当前流式消息元素
    _streamElement: null,
    _streamBuffer: '',
    _isStreaming: false,

    init() {
        this._bindEvents();
        this._loadSessions();
    },

    _bindEvents() {
        const input = document.getElementById('chat-input');
        const sendBtn = document.getElementById('btn-send');
        const newBtn = document.getElementById('btn-new-session');
        const resetBtn = document.getElementById('btn-reset-session');
        const interruptBtn = document.getElementById('btn-interrupt');
        const modelSelect = document.getElementById('model-select');

        // 发送消息
        if (input) {
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    this.sendMessage();
                }
            });
            // 自动调整高度
            input.addEventListener('input', () => {
                input.style.height = 'auto';
                input.style.height = Math.min(input.scrollHeight, 150) + 'px';
            });
        }

        if (sendBtn) sendBtn.addEventListener('click', () => this.sendMessage());
        if (newBtn) newBtn.addEventListener('click', () => this.newSession());
        if (resetBtn) resetBtn.addEventListener('click', () => this.resetSession());
        if (interruptBtn) interruptBtn.addEventListener('click', () => this.interruptSession());
        if (modelSelect) {
            modelSelect.addEventListener('change', () => {
                const model = modelSelect.value;
                document.getElementById('chat-model').textContent = model;
            });
        }
    },

    // ── 发送消息 ──
    sendMessage() {
        const input = document.getElementById('chat-input');
        const text = input?.value?.trim();
        if (!text) return;

        // 清除欢迎消息
        this._clearWelcome();

        // 显示用户消息
        this._addMessage('user', text);
        input.value = '';
        input.style.height = 'auto';

        // 准备流式接收
        this._streamBuffer = '';
        this._isStreaming = true;

        // 发送 WebSocket 消息
        const model = document.getElementById('model-select')?.value || 'gpt-4o';
        sendWsMessage({
            type: 'chat',
            message: text,
            session_id: SpiritApp.sessionId,
            model: model,
        });
    },

    // ── 流式文本追加 ──
    appendStreamText(text) {
        this._streamBuffer += text;

        if (!this._streamElement) {
            // 创建助手消息元素
            this._streamElement = this._addMessage('assistant', '', true);
        }

        // 更新内容（简单 Markdown 渲染）
        const el = this._streamElement.querySelector('.message-text');
        if (el) {
            el.innerHTML = this._renderMarkdown(this._streamBuffer);
        }

        // 滚动到底部
        this._scrollToBottom();
    },

    finalizeStream() {
        this._isStreaming = false;
        this._streamElement = null;
        this._streamBuffer = '';
    },

    // ── 工具调用显示 ──
    showToolCall(toolName, args) {
        this._clearWelcome();
        const messagesEl = document.getElementById('chat-messages');
        if (!messagesEl) return;

        const div = document.createElement('div');
        div.className = 'tool-call';
        div.innerHTML = `
            <div class="tool-call-header">🔧 ${this._escapeHtml(toolName)}</div>
            <div class="tool-call-args">${this._escapeHtml(JSON.stringify(args || {}, null, 2))}</div>
        `;
        messagesEl.appendChild(div);
        this._scrollToBottom();
    },

    showToolResult(toolName, result) {
        // 工具结果通过 WebSocket 的 tool_call_result 事件处理
        // 这里可以添加更丰富的展示
    },

    showError(code, message) {
        this._clearWelcome();
        this._addMessage('assistant', `⚠️ 错误 [${code}]: ${message}`);
        this.finalizeStream();
    },

    // ── 会话管理 ──
    newSession() {
        SpiritApp.sessionId = null;
        const messagesEl = document.getElementById('chat-messages');
        if (messagesEl) {
            messagesEl.innerHTML = `
                <div class="welcome-message">
                    <h2>👻 Spirit Agent</h2>
                    <p>新会话已创建，开始对话吧！</p>
                </div>
            `;
        }
        document.getElementById('chat-session-id').textContent = '';
        this._loadSessions();
    },

    resetSession() {
        if (SpiritApp.sessionId) {
            sendWsMessage({ type: 'reset' });
            this.newSession();
        }
    },

    interruptSession() {
        if (SpiritApp.sessionId) {
            sendWsMessage({ type: 'interrupt' });
        }
    },

    // ── 加载会话列表 ──
    async _loadSessions() {
        try {
            const data = await apiGet('/sessions');
            const list = document.getElementById('session-list');
            if (!list) return;

            if (!data.sessions || data.sessions.length === 0) {
                list.innerHTML = '<div style="padding:12px;color:var(--text-muted);font-size:13px;text-align:center;">暂无会话</div>';
                return;
            }

            list.innerHTML = data.sessions.map(s => `
                <div class="session-item ${s.id === SpiritApp.sessionId ? 'active' : ''}"
                     onclick="ChatPage.switchSession('${s.id}')">
                    <div class="session-title">${s.model || 'Session'} — ${s.id?.substring(0, 8)}</div>
                    <div class="session-meta">${s.message_count || 0} 条消息</div>
                </div>
            `).join('');
        } catch (e) {
            console.debug('加载会话列表失败:', e);
        }
    },

    switchSession(sessionId) {
        SpiritApp.sessionId = sessionId;
        sendWsMessage({ type: 'chat', message: '', session_id: sessionId });
        document.getElementById('chat-session-id').textContent = sessionId.substring(0, 8);
        this._loadSessions();
    },

    // ── 辅助方法 ──
    _clearWelcome() {
        const messagesEl = document.getElementById('chat-messages');
        if (messagesEl) {
            const welcome = messagesEl.querySelector('.welcome-message');
            if (welcome) welcome.remove();
        }
    },

    _addMessage(role, text, returnElement = false) {
        const messagesEl = document.getElementById('chat-messages');
        if (!messagesEl) return null;

        const div = document.createElement('div');
        div.className = `message ${role}`;
        const avatar = role === 'user' ? '👤' : '👻';
        const roleName = role === 'user' ? '你' : 'Spirit';

        div.innerHTML = `
            <div class="message-avatar">${avatar}</div>
            <div class="message-content">
                <div class="message-role">${roleName}</div>
                <div class="message-text">${this._renderMarkdown(text)}</div>
            </div>
        `;

        messagesEl.appendChild(div);
        this._scrollToBottom();

        return returnElement ? div : null;
    },

    _scrollToBottom() {
        const messagesEl = document.getElementById('chat-messages');
        if (messagesEl) {
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }
    },

    _escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    },

    _renderMarkdown(text) {
        if (!text) return '';
        // 简单的 Markdown 渲染
        let html = this._escapeHtml(text);
        // 代码块
        html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
        // 行内代码
        html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
        // 粗体
        html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
        // 斜体
        html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
        // 换行
        html = html.replace(/\n/g, '<br>');
        return html;
    },
};
