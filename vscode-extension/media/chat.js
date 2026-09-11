/**
 * Spirit Agent — Webview 端交互逻辑。
 *
 * 运行在 VSCode Webview 的沙箱环境中，负责：
 * - 消息发送/接收
 * - Markdown 渲染（简易版）
 * - 代码块复制按钮
 * - 工具调用卡片
 * - 编辑提案交互
 * - @mention 文件引用
 * - 自动滚动
 */

// @ts-nocheck — Webview 沙箱环境，无 TypeScript 类型
(function () {
  const vscode = acquireVsCodeApi();
  const messagesEl = document.getElementById("messages");
  const inputEl = document.getElementById("input");
  const sendBtn = document.getElementById("send-btn");
  const contextBar = document.getElementById("context-bar");

  let currentAssistantEl = null;
  let currentToolEl = null;

  // ── 消息发送 ────────────────────────────────────────────────

  function send() {
    const text = inputEl.value.trim();
    if (!text) return;
    addMessage("user", text);
    vscode.postMessage({ type: "chat", message: text });
    inputEl.value = "";
    inputEl.style.height = "auto";
    if (sendBtn) sendBtn.disabled = true;
  }

  if (sendBtn) sendBtn.addEventListener("click", send);
  if (inputEl) {
    inputEl.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    });
    // 自动调整输入框高度
    inputEl.addEventListener("input", function () {
      inputEl.style.height = "auto";
      inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + "px";
      // 检测 @mention
      checkMention(inputEl);
    });
  }

  // ── 消息渲染 ────────────────────────────────────────────────

  function addMessage(role, text) {
    const el = document.createElement("div");
    el.className = "message " + role;
    if (role === "assistant") {
      el.innerHTML = renderMarkdown(text);
      addCopyButtons(el);
    } else {
      el.textContent = text;
    }
    messagesEl.appendChild(el);
    scrollToBottom();
    return el;
  }

  function appendDelta(text) {
    if (!currentAssistantEl) {
      currentAssistantEl = addMessage("assistant", "");
    }
    currentAssistantEl.innerHTML = renderMarkdown(
      (currentAssistantEl._rawText || "") + text
    );
    currentAssistantEl._rawText = (currentAssistantEl._rawText || "") + text;
    addCopyButtons(currentAssistantEl);
    scrollToBottom();
  }

  // ── 简易 Markdown 渲染 ──────────────────────────────────────

  function renderMarkdown(text) {
    if (!text) return "";
    let html = text;

    // 代码块 ```lang\n...\n```
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, function (_, lang, code) {
      const escaped = escapeHtml(code.trim());
      return (
        '<div class="code-block-wrapper"><pre><code class="language-' +
        lang +
        '">' +
        escaped +
        "</code></pre></div>"
      );
    });

    // 行内代码 `...`
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");

    // 粗体 **text**
    html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");

    // 斜体 *text*
    html = html.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, "<em>$1</em>");

    // 链接 [text](url)
    html = html.replace(
      /\[([^\]]+)\]\(([^)]+)\)/g,
      '<a href="#" data-url="$2">$1</a>'
    );

    // 标题 ### text
    html = html.replace(/^### (.+)$/gm, "<h3>$1</h3>");
    html = html.replace(/^## (.+)$/gm, "<h2>$1</h2>");
    html = html.replace(/^# (.+)$/gm, "<h1>$1</h1>");

    // 列表 - item
    html = html.replace(/^- (.+)$/gm, "• $1");

    // 换行
    html = html.replace(/\n/g, "<br>");

    return html;
  }

  function escapeHtml(text) {
    return text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // ── 代码块复制按钮 ──────────────────────────────────────────

  function addCopyButtons(container) {
    const wrappers = container.querySelectorAll(".code-block-wrapper");
    wrappers.forEach(function (wrapper) {
      if (wrapper.querySelector(".copy-btn")) return;
      const btn = document.createElement("button");
      btn.className = "copy-btn";
      btn.textContent = "Copy";
      btn.addEventListener("click", function () {
        const code = wrapper.querySelector("code");
        if (code) {
          navigator.clipboard.writeText(code.textContent).then(function () {
            btn.textContent = "Copied!";
            setTimeout(function () {
              btn.textContent = "Copy";
            }, 2000);
          });
        }
      });
      wrapper.appendChild(btn);
    });
  }

  // ── 工具调用卡片 ────────────────────────────────────────────

  function addToolCall(name, args) {
    const el = document.createElement("div");
    el.className = "tool-call";
    let html = '<span class="status-icon">⏳</span>';
    html += '<span class="name">' + escapeHtml(name) + "</span>";
    if (args) {
      const preview = JSON.stringify(args);
      html +=
        ' <span class="args">(' +
        escapeHtml(preview.length > 80 ? preview.slice(0, 80) + "..." : preview) +
        ")</span>";
    }
    el.innerHTML = html;
    messagesEl.appendChild(el);
    currentToolEl = el;
    scrollToBottom();
  }

  function updateToolResult(name, ok, duration) {
    if (!currentToolEl) return;
    const icon = ok ? "✅" : "❌";
    currentToolEl.innerHTML =
      '<span class="status-icon">' +
      icon +
      "</span>" +
      '<span class="name">' +
      escapeHtml(name) +
      "</span>" +
      ' <span class="args">(' +
      duration.toFixed(1) +
      "s)</span>";
    currentToolEl = null;
  }

  // ── 编辑提案卡片 ────────────────────────────────────────────

  function addEditProposal(data) {
    const el = document.createElement("div");
    el.className = "edit-proposal";
    const fileName = data.file_path.split(/[\\/]/).pop() || data.file_path;

    let html = '<div class="header">';
    html += '<span class="file-path">' + escapeHtml(fileName) + "</span>";
    if (data.description) {
      html += '<span class="description">' + escapeHtml(data.description) + "</span>";
    }
    html += "</div>";

    // 简易 diff 预览
    html += '<div class="diff-preview">';
    html += buildSimpleDiff(data.original_content, data.new_content);
    html += "</div>";

    // 操作按钮
    html += '<div class="actions">';
    html += '<button class="accept" data-edit-id="' + data.edit_id + '">Accept</button>';
    html += '<button class="reject" data-edit-id="' + data.edit_id + '">Reject</button>';
    html += "</div>";

    el.innerHTML = html;

    // 绑定按钮事件
    el.querySelector(".accept").addEventListener("click", function () {
      vscode.postMessage({
        type: "editDecision",
        edit_id: data.edit_id,
        accepted: true,
      });
      el.querySelector(".actions").innerHTML =
        '<div class="status">✅ 已接受</div>';
    });

    el.querySelector(".reject").addEventListener("click", function () {
      vscode.postMessage({
        type: "editDecision",
        edit_id: data.edit_id,
        accepted: false,
      });
      el.querySelector(".actions").innerHTML =
        '<div class="status">❌ 已拒绝</div>';
    });

    messagesEl.appendChild(el);
    scrollToBottom();
  }

  function buildSimpleDiff(original, modified) {
    const origLines = (original || "").split("\n");
    const modLines = (modified || "").split("\n");
    let html = "";

    // 简易对比：标记新增和删除的行
    const maxLines = Math.max(origLines.length, modLines.length);
    for (let i = 0; i < Math.min(maxLines, 30); i++) {
      const oLine = origLines[i];
      const mLine = modLines[i];
      if (oLine === undefined) {
        html += '<div class="added">+ ' + escapeHtml(mLine) + "</div>";
      } else if (mLine === undefined) {
        html += '<div class="removed">- ' + escapeHtml(oLine) + "</div>";
      } else if (oLine !== mLine) {
        html += '<div class="removed">- ' + escapeHtml(oLine) + "</div>";
        html += '<div class="added">+ ' + escapeHtml(mLine) + "</div>";
      }
    }
    if (maxLines > 30) {
      html += "<div>... 还有 " + (maxLines - 30) + " 行</div>";
    }
    return html;
  }

  // ── @mention 文件引用（简化版）──────────────────────────────

  function checkMention(input) {
    const value = input.value;
    const atMatch = value.match(/@(\w*)$/);
    if (atMatch) {
      // 可以弹出文件选择器（简化版暂不实现）
    }
  }

  // ── 上下文栏 ────────────────────────────────────────────────

  function updateContextBar(ctx) {
    if (!contextBar) return;
    if (!ctx || !ctx.activeFile) {
      contextBar.style.display = "none";
      return;
    }
    contextBar.style.display = "flex";
    const fileName = ctx.activeFile.split(/[\\/]/).pop();
    let html = '<span class="file-chip">📄 ' + escapeHtml(fileName) + "</span>";
    if (ctx.selection) {
      html += '<span class="file-chip">📝 已选中代码</span>';
    }
    contextBar.innerHTML = html;
  }

  // ── 工具函数 ────────────────────────────────────────────────

  function scrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // ── 接收后端消息 ────────────────────────────────────────────

  window.addEventListener("message", function (event) {
    const msg = event.data;

    switch (msg.type) {
      case "streamDelta":
        appendDelta(msg.text);
        break;

      case "streamEnd":
        currentAssistantEl = null;
        if (sendBtn) sendBtn.disabled = false;
        break;

      case "toolStart":
        addToolCall(msg.toolName, msg.args);
        break;

      case "toolResult":
        updateToolResult(msg.toolName, msg.ok, msg.duration || 0);
        break;

      case "editProposal":
        addEditProposal(msg);
        break;

      case "context":
        updateContextBar(msg.context);
        break;

      case "status":
        // 可选：显示状态
        break;

      case "error":
        addMessage("assistant", "❌ " + msg.message);
        if (sendBtn) sendBtn.disabled = false;
        break;

      case "session":
        // 会话更新
        break;
    }
  });
})();
