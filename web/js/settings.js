/**
 * Spirit Agent Dashboard — 设置页面
 */

const SettingsPage = {
    init() {
        this._bindEvents();
    },

    onActivate() {
        this._checkServiceStatus();
    },

    _bindEvents() {
        const testBtn = document.getElementById('btn-test-memora');
        if (testBtn) {
            testBtn.addEventListener('click', () => this.testMemoraConnection());
        }
    },

    // ── 测试 Memora 连接 ──
    async testMemoraConnection() {
        const resultEl = document.getElementById('memora-test-result');
        if (!resultEl) return;

        resultEl.textContent = '测试中...';
        resultEl.className = 'test-result';

        const url = document.getElementById('settings-memora-url')?.value || 'http://127.0.0.1:8080';

        try {
            // 通过 Spirit 反向代理检查
            const resp = await fetch('/memora/health');
            const data = await resp.json();

            if (data.status === 'connected') {
                resultEl.textContent = '✅ 连接成功！Memora 服务正常运行';
                resultEl.className = 'test-result success';
            } else {
                resultEl.textContent = `⚠️ 连接异常: ${data.status} — ${data.error || ''}`;
                resultEl.className = 'test-result error';
            }
        } catch (e) {
            resultEl.textContent = `❌ 连接失败: ${e.message}`;
            resultEl.className = 'test-result error';
        }
    },

    // ── 检查服务状态 ──
    async _checkServiceStatus() {
        // Spirit API
        try {
            const resp = await fetch('/api/status');
            const data = await resp.json();
            const spiritEl = document.getElementById('status-spirit');
            if (spiritEl) {
                spiritEl.className = 'status-dot status-ok';
                spiritEl.title = `运行中 · ${data.tool_count || 0} 工具 · ${data.active_sessions || 0} 活跃会话`;
            }
        } catch (e) {
            const spiritEl = document.getElementById('status-spirit');
            if (spiritEl) {
                spiritEl.className = 'status-dot status-error';
                spiritEl.title = '不可用';
            }
        }

        // Memora
        try {
            const resp = await fetch('/memora/health');
            const data = await resp.json();
            const memoraEl = document.getElementById('status-memora');
            if (memoraEl) {
                if (data.status === 'connected') {
                    memoraEl.className = 'status-dot status-ok';
                    memoraEl.title = '已连接';
                } else {
                    memoraEl.className = 'status-dot status-unknown';
                    memoraEl.title = data.error || data.status;
                }
            }
        } catch (e) {
            const memoraEl = document.getElementById('status-memora');
            if (memoraEl) {
                memoraEl.className = 'status-dot status-error';
                memoraEl.title = '不可用';
            }
        }
    },
};
