/**
 * Spirit Agent Dashboard — 主应用逻辑 + 路由
 */

// ============================================================
// 全局配置
// ============================================================
const SpiritApp = {
    // API 地址（同源，不需要完整 URL）
    apiBase: '',
    // WebSocket 地址
    wsUrl: `ws://${location.host}/ws/chat`,
    // 当前页面
    currentPage: 'chat',
    // WebSocket 连接
    ws: null,
    // 当前会话 ID
    sessionId: null,
    // 重连定时器
    reconnectTimer: null,
    // 重连间隔 ms
    reconnectInterval: 3000,
};

// ============================================================
// 路由
// ============================================================
function navigateTo(page) {
    // 隐藏所有页面
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    // 显示目标页面
    const target = document.getElementById(`page-${page}`);
    if (target) {
        target.classList.add('active');
    }
    // 更新导航高亮
    document.querySelectorAll('.nav-link').forEach(link => {
        link.classList.toggle('active', link.dataset.page === page);
    });
    SpiritApp.currentPage = page;

    // 页面切换回调
    if (page === 'knowledge' && typeof KnowledgePage !== 'undefined') {
        KnowledgePage.onActivate();
    } else if (page === 'settings' && typeof SettingsPage !== 'undefined') {
        SettingsPage.onActivate();
    }
}

// Hash 路由
function handleHashChange() {
    const hash = location.hash || '#/';
    const route = hash.replace('#/', '').replace('#', '');
    const page = route || 'chat';
    navigateTo(page);
}

// ============================================================
// WebSocket 连接管理
// ============================================================
function connectWebSocket() {
    if (SpiritApp.ws && SpiritApp.ws.readyState <= 1) {
        return; // 已连接或正在连接
    }

    const ws = new WebSocket(SpiritApp.wsUrl);

    ws.onopen = () => {
        console.log('[WS] 连接已建立');
        updateConnectionStatus('connected');
        // 清除重连定时器
        if (SpiritApp.reconnectTimer) {
            clearInterval(SpiritApp.reconnectTimer);
            SpiritApp.reconnectTimer = null;
        }
    };

    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleWsMessage(data);
        } catch (e) {
            console.warn('[WS] 消息解析失败:', e);
        }
    };

    ws.onclose = () => {
        console.log('[WS] 连接已断开');
        updateConnectionStatus('disconnected');
        // 自动重连
        if (!SpiritApp.reconnectTimer) {
            SpiritApp.reconnectTimer = setInterval(() => {
                console.log('[WS] 尝试重连...');
                connectWebSocket();
            }, SpiritApp.reconnectInterval);
        }
    };

    ws.onerror = (err) => {
        console.error('[WS] 错误:', err);
    };

    SpiritApp.ws = ws;
}

function sendWsMessage(data) {
    if (SpiritApp.ws && SpiritApp.ws.readyState === WebSocket.OPEN) {
        SpiritApp.ws.send(JSON.stringify(data));
    } else {
        console.warn('[WS] 未连接，消息未发送');
    }
}

function updateConnectionStatus(status) {
    const badge = document.getElementById('connection-status');
    if (!badge) return;
    if (status === 'connected') {
        badge.className = 'status-badge status-connected';
        badge.textContent = '● 已连接';
    } else {
        badge.className = 'status-badge status-disconnected';
        badge.textContent = '● 未连接';
    }
}

// ============================================================
// WebSocket 消息处理
// ============================================================
function handleWsMessage(data) {
    const type = data.type;

    switch (type) {
        case 'session':
            SpiritApp.sessionId = data.session_id;
            const sidEl = document.getElementById('chat-session-id');
            if (sidEl) sidEl.textContent = data.session_id?.substring(0, 8) || '';
            break;

        case 'message_chunk':
            if (typeof ChatPage !== 'undefined') {
                ChatPage.appendStreamText(data.text || '');
            }
            break;

        case 'message_stop':
            if (typeof ChatPage !== 'undefined') {
                ChatPage.finalizeStream();
            }
            break;

        case 'tool_call_start':
            if (typeof ChatPage !== 'undefined') {
                ChatPage.showToolCall(data.tool_name, data.args);
            }
            break;

        case 'tool_call_result':
            if (typeof ChatPage !== 'undefined') {
                ChatPage.showToolResult(data.tool_name, data.result);
            }
            break;

        case 'status':
            // 可显示 "thinking" / "idle" 等状态
            break;

        case 'error':
            if (typeof ChatPage !== 'undefined') {
                ChatPage.showError(data.code, data.message);
            }
            break;

        case 'pong':
            // 心跳响应
            break;
    }
}

// ============================================================
// API 调用辅助
// ============================================================
async function apiGet(path) {
    const resp = await fetch(`${SpiritApp.apiBase}/api${path}`);
    if (!resp.ok) throw new Error(`API 错误: ${resp.status}`);
    return resp.json();
}

async function apiPost(path, data) {
    const resp = await fetch(`${SpiritApp.apiBase}/api${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    });
    if (!resp.ok) throw new Error(`API 错误: ${resp.status}`);
    return resp.json();
}

async function apiDelete(path) {
    const resp = await fetch(`${SpiritApp.apiBase}/api${path}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error(`API 错误: ${resp.status}`);
    return resp.json();
}

// ============================================================
// 初始化
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    // 路由
    window.addEventListener('hashchange', handleHashChange);
    handleHashChange();

    // WebSocket 连接
    connectWebSocket();

    // 心跳
    setInterval(() => {
        sendWsMessage({ type: 'ping' });
    }, 30000);

    // 初始化各页面
    if (typeof ChatPage !== 'undefined') ChatPage.init();
    if (typeof KnowledgePage !== 'undefined') KnowledgePage.init();
    if (typeof SettingsPage !== 'undefined') SettingsPage.init();
});
