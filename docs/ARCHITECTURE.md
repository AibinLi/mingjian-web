# 当前架构

本文描述 `e2e-baseline-20260729` 之后的真实本机 E2E 架构。历史阶段架构保留在 Git 历史和旧交接文档中，但不应再用来描述当前运行方式。

## 部署边界

当前交付是 Windows localhost 单用户比赛演示版本：

- Web 页面和 API 统一由 `127.0.0.1:8000` 提供。
- 前端是 `frontend/index.html`，由 FastAPI 静态挂载，不存在独立 Vite/5173 运行服务。
- Whisper、Tavily、DeepSeek 分别运行在独立本地进程中。
- SQLite、上传对象和分析派生物保存在本地 `backend/data/`，不进入 Git。
- 这不是公网、多租户生产部署，也没有完成异机便携包验收。

## 进程与端口

| 端口 | 进程 | 入口 | 职责 | 凭据边界 |
|---:|---|---|---|---|
| 8000 | Main API + Web | `backend.app.main:app` | 静态前端、任务 API、SQLite、对象存储、本地任务 Worker、报告 | 不读取 Tavily/DeepSeek Key |
| 8003 | Whisper Worker | `backend.app.asr_worker_main` | 本地 Whisper CPU ASR，独立硬超时 | 无外部 API Key |
| 8005 | Tavily Worker | `backend.app.tavily_worker_main` | 完成报告后的受限联网检索 | 只读取 `TAVILY_API_KEY` |
| 8006 | DeepSeek Worker | `backend.app.deepseek_worker` | 有界结构化证据辅助研判 | 只读取 `DEEPSEEK_API_KEY` |

`scripts/start-all.ps1` 依次检查端口并为四个服务打开 PowerShell 窗口。`scripts/run-service.ps1` 会先从每个服务进程移除外部 Key，再只向 Tavily 或 DeepSeek Worker 注入其所需 Key。

## 总体结构

```text
Browser
  |
  | http://127.0.0.1:8000
  v
Main API + Static Web (8000)
  |-- SQLite job metadata
  |-- backend/data/objects + derived artifacts
  |-- local JobWorker thread
  |-- FFprobe / FFmpeg subprocesses
  |-- Meso4 local inference
  |-- Tesseract OCR subprocess
  |-- HTTP -> Whisper Worker (8003)
  |-- explicit refresh only -> Tavily Worker (8005)
  `-- explicit refresh only -> DeepSeek Worker (8006)
```

## 基础上传链

用户上传媒体后，Main API 保存任务并唤醒本地 `JobWorker`。基础任务不会调用 Tavily 或 DeepSeek。

```text
上传媒体
  -> 扩展名 / MIME / 魔数 / 大小边界
  -> SHA-256 对象键与 SQLite 任务
  -> FFprobe 容器和流检查
  -> FFmpeg 标准化、音频、关键帧和派生物
  -> Meso4 视觉风险信号
  -> Whisper Worker ASR
  -> Tesseract OCR
  -> 从带时间位置的 ASR/OCR 提取 Claim
  -> 初始三轴、质量门控和 Aggregate 报告
  -> 持久化并由 Web 展示
```

初始报告中的检索边界使用不联网的 `FakeSearchAdapter`，DeepSeek 分析边界为 disabled。这样基础媒体处理不依赖外部网络或付费 API，也不会因联网失败而无法生成初始报告。

## 显式联网刷新链

联网增强只能对已经完成并持久化的报告执行。用户点击“重新生成 DeepSeek 研判”后，前端调用：

```text
POST /api/v1/jobs/{job_id}/refresh-llm
```

实际处理边界为：

```text
已完成报告和已保存 ASR/OCR
  -> Tavily Worker：重新查询可核验 Claim，并保存真实引用或明确失败状态
  -> Provenance Verification：使用声明的 source_url 与检索记录重建来源证据
  -> 构造有大小上限的结构化证据包
  -> DeepSeek Worker：生成辅助研判和引用索引
  -> InternetVerificationBuilder：仅接受同一主张内、指向已保存非 Mock 引用的合法索引
  -> AggregateAssessmentBuilder：按最新各维度结果重算风险指数和覆盖率
  -> 更新 SQLite 报告及 JSON 工件
  -> Web 重新读取并展示报告
```

DeepSeek Worker 不接收原始视频、关键帧、本地对象路径或 API Key。DeepSeek 输出必须经过后端索引和引用校验，不能凭模型记忆补造引用。

## 状态语义

- `completed`：对应步骤完成；不保证结论正确或证据充分。
- `insufficient_evidence`：步骤完成，但没有足够的合格非 Mock 证据；不等于联网失败，也不等于主张为假。
- `network_error`：DNS、连接、代理或超时导致联网步骤没有完成。
- `not_configured`：对应 Worker 缺少 Key、URL 或启用配置。
- `unavailable`：模型、工具、输入模态或工件不可用。
- `not_applicable`：输入本身不包含该模态，例如无音频媒体。
- `mock=true`：只验证接口或状态契约，不是现实证据或性能结果。

## 报告模型

系统持续独立保存：

1. 媒体真实性：媒体是否存在生成或篡改风险信号。
2. 内容可信度：可定位主张是否被合格引用支持、反对或形成冲突。
3. 来源可靠性：声明来源及传播证据是否充分。

`internet_verification` 是独立的联网主张核验工件，`aggregate_assessment` 是用于复核排序的策略分数卡。Aggregate 不覆盖三轴原始状态，固定 `automatic_action=none`、`truth_label=none`，不能解释为伪造概率或自动裁决。

## 数据与工件

主要本地数据包括：

- SQLite 任务和报告记录。
- SHA-256 派生的上传对象。
- 标准化媒体、音频、关键帧和命令轨迹。
- ASR、OCR、Claim、引用、Provenance、DeepSeek、Internet Verification、Aggregate JSON 工件。

这些运行数据位于 `backend/data/` 并被 Git 忽略。默认代码边界会限制上传、媒体时长、尺寸、帧率、派生物总量、命令输出和任务时间；缺失或失败不会映射为低风险。

## 安全边界

- 文件名不进入对象路径；上传内容经过扩展名、MIME、魔数和媒体容器检查。
- FFmpeg/FFprobe 通过无 shell 参数列表运行，并限制协议、线程、时间和输出。
- 用户声明的 `source_url` 只作为来源引用保存；系统不下载该 URL 指向的视频。
- ASR、OCR、搜索结果和网页片段都是不可信数据，不能改变系统指令或触发任意工具。
- Main API 不读取供应商 Key；Key 只存在于对应联网 Worker 进程。
- 日志和错误轨迹会脱敏凭据及本地路径，但这不替代提交前 Secret 检查。
- 当前 Windows 进程监听与生命周期适合受控本机演示，不构成公网隔离或生产级服务监管。

## 相关文档

- [当前能力状态](CURRENT_STATUS.md)
- [比赛运行手册](RUNBOOK.md)
- [已知限制](KNOWN_LIMITATIONS.md)
- [接口说明](INTERFACES.md)
- [威胁模型](THREAT_MODEL.md)

