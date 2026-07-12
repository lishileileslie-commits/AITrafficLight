# AI 红绿灯

Windows 桌面悬浮窗，一眼看出每个项目里的 AI 编码助手当前在干什么。

每个项目一张半透明磨砂玻璃卡片，左边一组三色灯，右边是项目名和正在处理的内容：

| 灯色 | 含义 |
| --- | --- |
| 🟢 绿 | AI 正在运行 |
| 🟡 黄 | 需要你处理（等待批准/输入） |
| 🔴 红 | 已完成或空闲 |

项目列表来自 VSCode 当前打开的文件夹（自动发现）。灯色来自各项目根目录下的 `.ai-status.json`，没有该文件则默认红灯。

## 工作原理

AI 编码助手（Claude Code / Codex）的 hook 在会话状态变化时调用 `status_hook.py`，它把状态写进当前项目的 `.ai-status.json`；悬浮窗 `app.py` 轮询这些文件并重绘灯色。

```
Claude Code hooks ──► status_hook.py ──► <项目>/.ai-status.json ──► app.py（悬浮窗）
```

卡片上的文字是**工作概念**（如"代码提交""发布上线""工作流修复"），由 `status_hook.py` 里的 `ACTION_PATTERNS` 从你的输入里归纳出来，而不是回显你的原话。

除了 hook 写入的状态，`app.py` 还会读 Codex 的 rollout 文件和 Claude 的 transcript 修改时间做兜底判断，避免长时间思考被误判成空闲。

渲染用 Pillow 超采样抗锯齿 + 高斯柔光，显示用 Win32 分层窗口做逐像素 alpha 透明。

## 环境要求

- Windows
- Python 3.11+
- Pillow

```powershell
pip install Pillow
```

## 运行

```powershell
pythonw app.py
```

用 `pythonw`（而不是 `python`）启动，这样不会留一个黑色控制台窗口。开机自启可以用仓库里的 `启动红绿灯.vbs`。

## 配置 hook

在 Claude Code 的 `settings.json` 里把三个事件映射到 `status_hook.py`：

| Hook 事件 | 命令 |
| --- | --- |
| `UserPromptSubmit` | `python status_hook.py green` |
| `Notification` | `python status_hook.py yellow` |
| `Stop` | `python status_hook.py red` |

## 悬浮窗操作

- **拖动**：按住卡片拖到任意位置，位置会被记住。
- **设置**：鼠标悬停到窗口右下角，会浮出透明度和大小滑块。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `app.py` | 悬浮窗本体：发现项目、读状态、绘制并显示卡片 |
| `status_hook.py` | AI 助手的 hook 入口，把会话状态归纳后写入 `.ai-status.json` |
| `启动红绿灯.vbs` | 开机自启脚本（无窗口启动 `pythonw app.py`） |

`.ai-status.json`、`standup_log.json`、`standup_ui.json`、`widget.log` 都是运行时生成的本地状态，已在 `.gitignore` 中排除。
