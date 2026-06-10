# 聊天页面右侧文件面板

## Context

用户在通过 Hermes 对话执行任务时，agent 会创建/写入文件（`write_file`、`edit_file`、terminal 输出等），用户也会上传文件。目前这些文件不可见，用户无法方便地查看和访问。需要在聊天页面右侧添加一个文件面板，实时显示任务过程中产生的文件，并支持点击下载。

## 方案

### 1. Broker 添加文件列表 API

**文件**: `/opt/hermes-platform/hermes_broker.py`

在现有 `GET /api/files/{filename}` 下载接口旁，添加 `GET /api/files` 列表接口：

- 认证方式复用 `_proc_for_request`
- 扫描 `proc.work_dir/uploads/` 目录，列出所有文件
- 返回 `[{name, size, mtime}]` JSON 列表
- 过滤隐藏文件（以 `.` 开头）和目录
- 文件名取 basename，防止路径遍历信息泄露

```python
@app.get("/api/files")
async def list_files(request: Request):
    """List uploaded files (JWT auth)."""
    proc = await _proc_for_request(request)
    uploads = Path(proc.work_dir) / "uploads"
    result = []
    if uploads.is_dir():
        for f in sorted(uploads.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_file() and not f.name.startswith('.'):
                stat = f.stat()
                result.append({
                    "name": f.name,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                })
    return result
```

> **Nginx 路由**: 已有 `location ~ ^/api/(sessions|upload|files|...)` 规则将 `/api/files` 代理到 broker，无需修改 nginx。

> **安全说明**: 现有 `GET /api/files/{filename}` 下载接口直接拼接路径。列表接口仅返回 basename，不暴露目录结构。但下载接口本身应增加路径校验（`if '..' in filename or '/' in filename`），这是独立修复项，本方案不涉及。

### 2. 前端添加右侧文件面板

**文件**: `/opt/hermes-platform/chat.html`

#### 面板样式（与 settings 面板一致）

现有 settings 面板使用 `position: absolute; inset: 0` 全屏覆盖模式（见 `chat.html:246`）。文件面板采用相同的覆盖模式，但宽度固定为 300px，仅覆盖右侧区域：

```css
/* ── Files panel (right-side overlay) ── */
.files-panel {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: 300px;
  z-index: 10;
  display: flex;
  flex-direction: column;
  background: var(--sidebar);
  border-left: 1px solid var(--border);
  transform: translateX(100%);
  transition: transform 0.2s ease;
}
.files-panel.open {
  transform: translateX(0);
}
.files-panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  font-weight: 600;
  font-size: 14px;
}
.files-panel-body {
  flex: 1;
  overflow-y: auto;
  padding: 8px 0;
}
.file-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 16px;
  cursor: pointer;
  font-size: 13px;
  border-bottom: 1px solid var(--border);
}
.file-item:hover {
  background: var(--hover);
}
.file-item-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  margin-right: 8px;
}
.file-item-meta {
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
}
```

#### HTML 结构

在 `chat-area` 容器内（与 `settings-col` 同级），添加：

```html
<div class="files-panel" id="filesPanel">
  <div class="files-panel-header">
    <span>Files</span>
    <button onclick="toggleFilesPanel()" style="background:none;border:none;color:var(--fg);cursor:pointer;font-size:18px">&times;</button>
  </div>
  <div class="files-panel-body" id="filesPanelBody">
    <div style="padding:16px;color:var(--muted);font-size:13px">No files yet</div>
  </div>
</div>
```

#### 切换按钮

在 header 工具栏（settings 按钮旁边）添加文件面板按钮：

```html
<button onclick="toggleFilesPanel()" title="Files" style="background:none;border:none;color:var(--fg);cursor:pointer;font-size:16px">📁</button>
```

#### JS 逻辑

```javascript
let filesPanelOpen = false;

function toggleFilesPanel() {
  const panel = $('filesPanel');
  filesPanelOpen = !filesPanelOpen;
  panel.classList.toggle('open', filesPanelOpen);
  if (filesPanelOpen) refreshFilesList();
}

function refreshFilesList() {
  fetch('/api/files', { headers: { 'Authorization': 'Bearer ' + wsToken } })
    .then(r => r.json())
    .then(files => {
      const body = $('filesPanelBody');
      if (!files.length) {
        body.innerHTML = '<div style="padding:16px;color:var(--muted);font-size:13px">No files yet</div>';
        return;
      }
      body.innerHTML = files.map(f => {
        const size = f.size < 1024 ? f.size + ' B' :
                     f.size < 1048576 ? (f.size/1024).toFixed(1) + ' KB' :
                     (f.size/1048576).toFixed(1) + ' MB';
        const time = new Date(f.mtime * 1000).toLocaleTimeString();
        return `<div class="file-item" onclick="window.open('/api/files/${encodeURIComponent(f.name)}?token=${wsToken}')">
          <span class="file-item-name">${esc(f.name)}</span>
          <span class="file-item-meta">${size} · ${time}</span>
        </div>`;
      }).join('');
    })
    .catch(() => {});
}
```

#### 自动刷新触发点

1. **`tool.complete` 事件**（`chat.html:1236`）：当工具名包含 `write`、`edit`、`save` 时，调用 `refreshFilesList()`。注意：agent 写入的文件路径在 work_dir 任意位置，`/api/files` 仅列出 `uploads/` 目录，因此仅对 agent 写入 uploads 的场景有效（如通过 broker upload 路径写入的文件）。对于 agent 写入其他位置的文件，本方案暂不覆盖（需 Hermes 侧支持才能追踪）。
2. **`uploadFiles` 成功后**：上传完成后调用 `refreshFilesList()`。
3. **面板打开时**：`toggleFilesPanel()` 展开时调用 `refreshFilesList()`。

#### Escape 关闭

在现有 Escape 键监听（`chat.html:2993`）中追加文件面板关闭逻辑：

```javascript
if (filesPanelOpen) toggleFilesPanel();
```

## 修改文件清单

| 文件 | 改动 |
|------|------|
| `hermes_broker.py` | 添加 `GET /api/files` 列表接口 |
| `chat.html` | 添加文件面板 HTML + CSS + JS（约 80 行） |
| nginx config | 无需修改 |

## 验证

1. 重启 broker：`kill $(pgrep -f hermes_broker) && sleep 2 && nohup python3 hermes_broker.py > /tmp/broker.log 2>&1 &`
2. 刷新 chat 页面
3. 点击 header 工具栏文件按钮，确认面板从右侧滑出
4. 上传文件，确认列表自动刷新
5. 让 agent 写入文件（write_file 工具），确认列表刷新（仅 uploads 目录内的文件）
6. 点击文件项，确认能下载/打开
7. 按 Escape，确认面板关闭
8. 刷新页面后重新打开，确认列表正常加载

## 局限性

- 仅列出 `uploads/` 目录文件，agent 通过 `write_file` 写入 work_dir 其他位置的文件不可见。后续可通过 Hermes 侧事件（如文件写入通知）或扫描整个 work_dir 来扩展。
