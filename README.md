# 明鉴 Web / MingJian Web

面向多模态媒体真实性与内容证据审查的本地 Web 原型。项目把视频分析拆分为媒体真实性、内容可信度、来源可靠性和联网核验四条可审计链路，输出复核建议与证据状态，不把单一分数包装成“自动判真伪”。

> **项目定位**：个人作品集与研究演示项目。当前版本是 Windows/localhost 单用户原型，不是公网生产服务，也不承诺任意电脑开箱即用。

## 能力概览

- **媒体处理**：FFmpeg/FFprobe 预处理、关键帧与时间位置管理、文件哈希和派生物审计。
- **视觉分析**：Meso4 视觉风险信号，支持批处理、超时和不可用降级。
- **语音与文字**：可选 Whisper ASR、Tesseract OCR，并保留时间戳和来源位置。
- **证据链**：从 ASR/OCR 文本提取可核验主张，保存查询、引用、支持/反对/冲突/证据不足状态。
- **联网增强**：通过独立 Worker 显式调用 Tavily 和 DeepSeek；首次上传不会自动联网。
- **可审计报告**：持久化三轴状态、质量门控、限制、引用、模型版本和运行配置，并支持 JSON 下载。

## 架构

```text
上传视频
  -> FFmpeg/FFprobe
  -> Meso4 / Whisper / Tesseract
  -> Claim Extraction
  -> 初始报告
  ->（用户显式刷新）Tavily / Provenance / DeepSeek
  -> Internet Verification / Aggregate
  -> 人工复核
```

默认服务端口：8000（Main API + Web）、8003（Whisper）、8005（Tavily）、8006（DeepSeek）。前端由 8000 端口直接提供，不需要单独启动 Vite。

## 快速开始

源码仓库不包含第三方模型、评测数据、用户媒体、密钥或完整便携运行时。先准备 Python 3.11、FFmpeg、Tesseract 以及获得授权的模型文件，再执行：

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
pytest -q
```

启动本地 API：

```powershell
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

浏览器访问 <http://127.0.0.1:8000>。Windows 演示脚本 `start-demo.ps1` 和完整离线依赖安装脚本 `setup.ps1` 面向已经准备好模型、工具和 wheel 的本地发行包；仅克隆源码时请先按脚本中的检查项补齐这些工件。

可选 Docker 入口：

```powershell
docker compose up --build -d
Invoke-RestMethod http://127.0.0.1:8000/health
```

联网 Worker 只从环境变量读取 `TAVILY_API_KEY` 和 `DEEPSEEK_API_KEY`。请使用本地 `.env`，不要把密钥写入代码、日志或提交记录。

## 目录结构

```text
backend/       FastAPI API、任务状态机和分析模块
frontend/      静态 Web 界面
workers/       Worker 说明与运行边界
evaluation/    评测脚本与指标计算
tests/         单元测试和接口契约测试
deploy/        Docker/Nginx 配置
docs/          架构、运行手册、威胁模型和限制
models/        模型来源、版本和许可说明（不含权重）
scripts/       启动、校验和模型准备脚本
```

## 验证与限制

- 测试命令为 `pytest -q`；真实模型、外部 API 和获授权数据是否可用取决于本地环境。
- 唯一正式冻结的视觉基线是获授权 Celeb-DF v2 518 个视频上的 Meso4 评测，ROC-AUC 为 `0.513648`。该结果接近随机，不能据此宣传为可靠的通用深伪检测器。
- Meso4、同步筛查和 Aggregate 均未校准为概率；`insufficient_evidence` 表示证据不足，不等同于内容虚假或网络失败。
- AST、Whisper、Tavily、DeepSeek 和评测数据均有独立的模型、许可、网络或授权前提；缺失时系统应降级并明确展示限制。
- 当前不包含公网部署、多租户、自动处置、真实 URL 媒体下载或完整传播链恢复。

详细说明：

- [架构](docs/ARCHITECTURE.md)
- [运行手册](docs/RUNBOOK.md)
- [当前状态](docs/CURRENT_STATUS.md)
- [已知限制](docs/KNOWN_LIMITATIONS.md)
- [接口说明](docs/INTERFACES.md)
- [测试记录](docs/TESTING.md)
- [第三方组件与许可](docs/THIRD_PARTY_NOTICES.md)

## 公开发布边界

建议只从本目录创建 GitHub 仓库。外层“可执行文件”目录是本地比赛发行包，包含约 1.6 GB 的解释器、工具、wheel 和模型缓存；请通过 GitHub Release、对象存储或授权下载方式单独分发，并先确认第三方许可允许再分发。参赛报告、答辩 PPT、原创性声明和演示视频含有身份或提交上下文，发布前应单独脱敏并确认是否需要公开。

## 许可

当前仓库尚未声明项目整体开源许可证。公开前请补充根目录 `LICENSE`，并在 [docs/THIRD_PARTY_NOTICES.md](docs/THIRD_PARTY_NOTICES.md) 中确认第三方模型、数据和工具的许可边界。


