/**
 * Spirit Agent Dashboard — 知识库浏览
 */

const KnowledgePage = {
    _memoraBase: '/memora',  // 通过 Spirit 反向代理
    _activated: false,

    init() {
        this._bindEvents();
    },

    onActivate() {
        if (!this._activated) {
            this._activated = true;
            this.checkConnection();
            this.loadDocuments();
        }
    },

    _bindEvents() {
        const searchInput = document.getElementById('kb-search-input');
        const searchBtn = document.getElementById('kb-search-btn');
        const uploadBtn = document.getElementById('kb-upload-btn');
        const fileInput = document.getElementById('kb-file-input');

        if (searchBtn) searchBtn.addEventListener('click', () => this.searchDocuments());
        if (searchInput) {
            searchInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') this.searchDocuments();
            });
        }

        if (uploadBtn) {
            uploadBtn.addEventListener('click', () => fileInput?.click());
        }
        if (fileInput) {
            fileInput.addEventListener('change', () => this.uploadFiles(fileInput.files));
        }

        // 快捷操作按钮
        document.querySelectorAll('.kb-action-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const action = btn.dataset.action;
                if (action === 'list') this.loadDocuments();
                else if (action === 'stats') this.loadStats();
                else if (action === 'open-memora') window.open('http://127.0.0.1:8080', '_blank');
            });
        });
    },

    // ── 连接检查 ──
    async checkConnection() {
        const statusEl = document.getElementById('kb-connection-status');
        try {
            const resp = await fetch(`${this._memoraBase}/health`);
            const data = await resp.json();
            if (statusEl) {
                if (data.status === 'connected') {
                    statusEl.textContent = '✅ Memora 已连接';
                    statusEl.style.color = 'var(--success)';
                } else {
                    statusEl.textContent = '⚠️ Memora 状态: ' + data.status;
                    statusEl.style.color = 'var(--warning)';
                }
            }
        } catch (e) {
            if (statusEl) {
                statusEl.textContent = '❌ Memora 未连接 (端口 8080)';
                statusEl.style.color = 'var(--danger)';
            }
        }
    },

    // ── 加载文档列表 ──
    async loadDocuments() {
        const listEl = document.getElementById('kb-doc-list');
        if (!listEl) return;

        listEl.innerHTML = '<div style="padding:12px;color:var(--text-muted);">加载中...</div>';

        try {
            const resp = await fetch(`${this._memoraBase}/api/v1/documents/?limit=50`);
            const data = await resp.json();
            const docs = data.documents || data || [];

            if (docs.length === 0) {
                listEl.innerHTML = '<div style="padding:12px;color:var(--text-muted);text-align:center;">知识库为空</div>';
                return;
            }

            listEl.innerHTML = docs.map(doc => `
                <div class="kb-doc-item" onclick="KnowledgePage.viewDocument('${doc.document_id || doc.id}')">
                    <div class="doc-title">${this._escapeHtml(doc.title || '未命名')}</div>
                    <div class="doc-meta">
                        ${doc.file_type || ''} · ${doc.created_at ? new Date(doc.created_at).toLocaleDateString() : ''}
                        ${(doc.tags || []).map(t => `<span style="color:var(--accent)">#${t}</span>`).join(' ')}
                    </div>
                </div>
            `).join('');
        } catch (e) {
            listEl.innerHTML = `<div style="padding:12px;color:var(--danger);">加载失败: ${e.message}</div>`;
        }
    },

    // ── 查看文档详情 ──
    async viewDocument(docId) {
        const contentEl = document.getElementById('kb-content');
        if (!contentEl) return;

        contentEl.innerHTML = '<div style="padding:24px;color:var(--text-muted);">加载中...</div>';

        try {
            const resp = await fetch(`${this._memoraBase}/api/v1/documents/${docId}`);
            const doc = await resp.json();

            contentEl.innerHTML = `
                <div class="kb-doc-detail">
                    <h2>${this._escapeHtml(doc.title || '未命名')}</h2>
                    <div class="doc-info">
                        <span>📄 ${doc.file_type || '未知类型'}</span>
                        <span>📅 ${doc.created_at ? new Date(doc.created_at).toLocaleString() : ''}</span>
                        ${(doc.tags || []).map(t => `<span style="color:var(--accent)">#${t}</span>`).join(' ')}
                    </div>
                    <div class="doc-body">
                        ${doc.content ? this._renderContent(doc.content) : '<p style="color:var(--text-muted)">无法预览此文件类型</p>'}
                    </div>
                </div>
            `;
        } catch (e) {
            contentEl.innerHTML = `<div style="padding:24px;color:var(--danger);">加载失败: ${e.message}</div>`;
        }
    },

    // ── 搜索文档 ──
    async searchDocuments() {
        const input = document.getElementById('kb-search-input');
        const query = input?.value?.trim();
        if (!query) return;

        const contentEl = document.getElementById('kb-content');
        if (!contentEl) return;

        contentEl.innerHTML = '<div style="padding:24px;color:var(--text-muted);">搜索中...</div>';

        try {
            const resp = await fetch(`${this._memoraBase}/api/v1/documents/search`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ query, limit: 10, score_threshold: 0.5 }),
            });
            const data = await resp.json();
            const results = data.results || [];

            if (results.length === 0) {
                contentEl.innerHTML = `
                    <div class="kb-search-results">
                        <h3>搜索结果: "${this._escapeHtml(query)}"</h3>
                        <p style="color:var(--text-muted);margin-top:12px;">未找到相关文档</p>
                    </div>
                `;
                return;
            }

            contentEl.innerHTML = `
                <div class="kb-search-results">
                    <h3>搜索结果: "${this._escapeHtml(query)}" (${results.length} 条)</h3>
                    ${results.map(r => `
                        <div class="kb-search-result-item" onclick="KnowledgePage.viewDocument('${r.document_id || ''}')">
                            <div class="result-title">${this._escapeHtml(r.title || '未命名')}</div>
                            <div class="result-score">相关度: ${(r.score * 100).toFixed(1)}%</div>
                            <div class="result-content">${this._escapeHtml((r.content || '').substring(0, 300))}...</div>
                        </div>
                    `).join('')}
                </div>
            `;
        } catch (e) {
            contentEl.innerHTML = `<div style="padding:24px;color:var(--danger);">搜索失败: ${e.message}</div>`;
        }
    },

    // ── 上传文件 ──
    async uploadFiles(files) {
        if (!files || files.length === 0) return;

        for (const file of files) {
            const formData = new FormData();
            formData.append('file', file);
            formData.append('title', file.name.replace(/\.[^.]+$/, ''));

            try {
                await fetch(`${this._memoraBase}/api/v1/documents/upload`, {
                    method: 'POST',
                    body: formData,
                });
            } catch (e) {
                console.error('上传失败:', file.name, e);
            }
        }

        // 刷新列表
        this.loadDocuments();
    },

    // ── 统计信息 ──
    async loadStats() {
        const contentEl = document.getElementById('kb-content');
        if (!contentEl) return;

        try {
            const resp = await fetch(`${this._memoraBase}/api/v1/stats/`);
            const stats = await resp.json();

            contentEl.innerHTML = `
                <div style="max-width:600px;">
                    <h3>📊 知识库统计</h3>
                    <div style="margin-top:16px;">
                        ${Object.entries(stats).map(([k, v]) => `
                            <div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);">
                                <span style="color:var(--text-secondary)">${k}</span>
                                <span style="color:var(--text-primary);font-weight:600">${v}</span>
                            </div>
                        `).join('')}
                    </div>
                </div>
            `;
        } catch (e) {
            contentEl.innerHTML = `<div style="padding:24px;color:var(--danger);">获取统计失败: ${e.message}</div>`;
        }
    },

    // ── 辅助 ──
    _escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text || '';
        return div.innerHTML;
    },

    _renderContent(content) {
        // 简单渲染：将纯文本/Markdown 转为 HTML
        let html = this._escapeHtml(content);
        html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
        html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
        html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
        html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
        html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
        html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
        html = html.replace(/\n/g, '<br>');
        return html;
    },
};
