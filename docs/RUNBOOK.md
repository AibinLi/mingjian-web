# 比赛运行手册

本文描述当前 Windows 本机真实 E2E 的启动、演示与故障确认方式。它不替代安装、模型许可或 Secret 管理流程，也不表示项目已经完成异机便携包验收。

## 运行边界

- 项目目录：`<repo-root>`
- 网页入口：<http://127.0.0.1:8000>
- 前端由 Main API 静态提供，不启动 Vite 或 5173 服务。
- 当前脚本启动四个本地 PowerShell 进程，不提供生产级守护或自动重启。

## 演示前检查

在比赛现场启动前人工确认：

1. 项目目录和 `.venv` 完整存在，依赖已提前安装；现场不要下载或升级依赖。
2. `ffmpeg`、`ffprobe` 和 `tesseract` 在当前服务进程可见的 PATH 中。
3. Meso4 与 Whisper 本地模型缓存完整；运行时不会自动下载模型。
4. `.env` 存在，并在需要联网演示时配置有效 `TAVILY_API_KEY` 和 `DEEPSEEK_API_KEY`。
5. `.env` 未进入 Git，Key 不出现在截图、日志或演示材料中。
6. 端口 8000、8003、8005、8006 没有被其他程序占用。
7. 使用团队有权演示的短视频；不要使用未授权媒体、用户数据或评测集原始视频。
8. 已确认现场网络和代理允许访问 Tavily 与 DeepSeek。基础报告不依赖联网，但刷新链依赖。

## 启动

在 PowerShell 中执行：

```powershell
cd <repo-root>
.\scripts\start-all.ps1
```

脚本会尝试启动：

| 窗口标题 | 端口 | 服务 |
|---|---:|---|
| `05-ASR-8003` | 8003 | Whisper Worker |
| `03-Tavily-8005` | 8005 | Tavily Worker |
| `01-DeepSeek-8006` | 8006 | DeepSeek Worker |
| `04-Main-8000` | 8000 | Main API + Web |

如果端口已经处于监听状态，脚本会跳过对应服务。**端口已监听不等于该端口上的进程一定是正确的明鉴服务**，仍需执行健康检查。

## 健康检查

在第五个 PowerShell 窗口中检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8003/health
Invoke-RestMethod http://127.0.0.1:8005/health
Invoke-RestMethod http://127.0.0.1:8006/health
```

重点确认：

- 8000 返回 `service=mingjian-web-api`。
- 8000 的 feature flags 中 ASR、OCR、RETRIEVAL、LLM 为启用；FACE、SYNC 按当前启动脚本为关闭。
- 8003 能报告 Whisper Worker 健康状态和硬超时配置。
- 8005 能报告 Tavily 是否 configured，但不能回显 Key。
- 8006 能报告 DeepSeek 是否 configured，但不能回显 Key。

健康接口成功只表示服务及配置边界可响应，不代表模型质量、联网证据充分或整条 E2E 已完成。

## 比赛演示步骤

### 1. 打开系统

访问 <http://127.0.0.1:8000>。不要打开 5173；当前没有独立前端服务。

### 2. 创建基础任务

1. 选择已授权视频文件。
2. 如果知道原始公开页面，可填写来源 URL。该 URL 只作为来源声明保存，系统不会从该 URL 下载视频。
3. 提交上传并等待任务完成。

### 3. 讲解初始报告

初始报告应重点展示：

- 媒体基础信息及 FFmpeg/FFprobe 处理状态。
- Meso4 视觉风险信号及其未校准限制。
- Whisper ASR 与 Tesseract OCR 的可定位文本。
- 从 ASR/OCR 提取的 Claim。
- 三轴原始状态、质量门控、限制和初始 Aggregate。

此时不要声称已经调用 Tavily 或 DeepSeek。首次上传不会自动执行联网增强。

### 4. 显式刷新联网研判

在已完成报告顶部点击“重新生成 DeepSeek 研判”。等待页面显示研判已更新。

该操作会使用已保存的 ASR/OCR 结果，通过 Tavily 重新执行 Claim Verification，随后依次进入 Provenance Verification、构造结构化证据包、DeepSeek 辅助研判、Internet Verification 与 Aggregate 重算。

### 5. 讲解联网结果

- 检查引用 URL、标题、域名、时间和逐字片段。
- 明确 DeepSeek 只提供辅助研判，不替代三轴或人工复核。
- `insufficient_evidence` 是证据不足，不是联网失败。
- `network_error` 才表示 DNS、连接、代理或超时问题。
- Aggregate 是复核风险指数，不是伪造概率。
- 来源可靠性只覆盖声明 URL 和已取得证据，不能证明完整传播链。

## 常见故障

### 端口已占用或启动脚本跳过服务

使用以下命令确认监听进程：

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8000,8003,8005,8006
```

不要仅因为端口存在就继续演示；结合四个 `/health` 响应确认服务身份。当前脚本不会替换或重启已经监听的进程。

### Main 页面无法打开

- 确认 8000 服务窗口没有退出。
- 检查 `http://127.0.0.1:8000/health`，不要改用 5173。
- 检查 `.venv`、Python 导入错误、FFmpeg/Tesseract PATH 和本地数据目录权限。

### ASR 返回不可用或超时

- 检查 8003 `/health`。
- 确认 Whisper 本地缓存存在且完整。
- 当前 Worker 硬超时默认 60 秒，Main 外层 ASR 超时默认 70 秒；CPU、长音频或复杂音频可能超过边界。
- 超时或缺模型不是低风险证据。

### OCR 不可用

- 确认 Main 窗口能找到 `tesseract`。
- 低清、快速切换、小字号、遮挡、旋转或不匹配语言都会降低识别质量。

### Tavily 或 DeepSeek 显示 `not_configured`

- 检查对应 Worker 的 `.env` Key 是否存在，但不要在终端回显 Key。
- Main API 不应读取这两个 Key；不要把 Key 临时注入 Main 进程。

### 联网刷新返回 `network_error`

- 检查现场网络、DNS、代理、TLS 和供应商可达性。
- 基础报告已经完成时，不要删除或覆盖它；联网失败不应抹掉基础结果。

### 联网刷新返回 `insufficient_evidence`

这通常表示检索和校验完成，但没有足够直接、合格、非 Mock 的引用。它不是网络故障，也不证明主张为假。

### FFmpeg 超时或媒体处理失败

- 当前单条 FFmpeg/FFprobe 命令使用固定时间边界，复杂但合法媒体可能失败。
- 使用较短、常见编码、已获授权的演示视频。
- 不要在比赛现场修改超时、转码参数或代码；保留错误状态并切换到预先验证的授权样本。

## 停止服务

当前没有 `stop-all.ps1` 或可靠 PID 管理。演示结束后，在四个服务窗口中使用 `Ctrl+C` 停止对应进程并关闭窗口，再确认端口不再监听。

不要使用未经核对目标的批量结束进程命令。运行数据和媒体的清理应作为单独受控操作处理，本运行手册不自动删除它们。

## 演示口径

- 说“风险信号”和“辅助研判”，不说“自动判真伪”。
- 说“已经在当前本机跑通 E2E”，不说“任意电脑解压即可运行”。
- 说“显式刷新后调用 Tavily/DeepSeek”，不说“上传自动联网分析”。
- 说“证据不足”，不把它解释为“联网失败”或“内容虚假”。
- 说“Aggregate 复核指数”，不说“综合伪造概率”。

能力边界见 [CURRENT_STATUS.md](CURRENT_STATUS.md)，风险与限制见 [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md)。



