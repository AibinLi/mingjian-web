# 接口契约

> 2026-07-28 加固更新：六个 `ENABLE_*` 开关默认关闭。真实 OCR item 包含 `frame_index/text/confidence/bbox/timestamp_seconds`；YuNet `faces.json` 在模型运行但无人脸时为 `status=available, tracks=[]`，每条轨迹保存对齐的 `frame_indices/bboxes/landmarks/confidences`。Whisper 结果稳定包含 `transcript/language/segments/confidence/limitations`。可选模块异常使用脱敏、限长且不含完整路径或堆栈的 `error_trace`。`aggregate_assessment` 同时输出 `configured_coverage` 与 `applicable_coverage`；联网故障为 `network_error` 且不参与加权。

> 阶段 11 进行中更新（2026-07-19）：完成报告新增根 `internet_verification`（`internet-verification-result-v1`）与 `aggregate_assessment`（`aggregate-review-scorecard-v1`），并分别持久化同名 JSON 工件。联网核验只接受 DeepSeek 返回的主张/引用索引，API 必须对已保存 `mock=false` 引用重新校验；无真实引用时 `risk_score=null`。综合报告为四个独立复核风险指数、权重、有效覆盖率、总体值和复核路由，固定 `automatic_action=none`、`truth_label=none`。`GET /api/v1/jobs/{id}/artifacts/{name}` allow-list 已加入 `internet_verification` 与 `aggregate_assessment`；刷新 LLM 接口会同步重算两项工件。

> 阶段 7 更新（2026-07-15，当前接口）：`GET /health.phase` 为 `7-traceable-web-report`。静态 Web 页只读取并展示持久化报告，绝不在浏览器计算或改写模型结论、阈值或 recommendation。完成任务可下载原始报告 JSON，并通过本地只读接口访问已登记的标准化媒体、关键帧及有限 JSON 审计工件。页面必须显式呈现 `unavailable`、`not_applicable`、`failed`、`cancelled`、Mock 与缺失派生物；它们不是低风险结论。PDF 导出未实现（P1）。分析模型栈仍为阶段 6 的 `PhaseSixReporter`，阶段 7 没有新增模型、阈值、校准或正式实验。

> 阶段 6 更新（2026-07-15，历史模型与报告字段）：`GET /health` 当时的 phase 为 `6-calibration-quality-three-axis-recommendations`，并返回各模型校准器可用性与 `recommendation_ruleset=phase6-rules-v1`。文件报告新增 `phase6.calibration`、`phase6.quality` 和 `phase6.recommendation`，并持久化 `phase6_quality.json`、`phase6_recommendation.json`。校准器仅在独立 calibration role、官方测试路径零重叠、统一可见人物身份全局划分、工件/权重哈希完整、`deployment_status=active` 且冻结后 evaluation-only 验收通过时加载；否则 `status=unavailable`，原始模型分数仍保留并明确未经校准。Mock 内容/来源轴不产生冲突或融合信号。recommendation 只有复核/补证据建议，`automatic_action=none`、`legal_or_policy_determination=none`，三轴继续独立保存。

> 阶段 3 更新（2026-07-14，优先于以下阶段 2 报告说明）：`GET /health` 的 phase 为 `3-visual-meso4-baseline`，并返回 Meso4 模型可用性元数据。文件任务完成报告的根 `mock=false`，但 `partial_mock_axes=["content","provenance"]`；`axes.authenticity.mock=false` 且只报告模型风险/不可用状态，绝不输出“真/假”断言。`results.artifacts` 新增 `visual_detection.json`，其 schema 为 `visual-detector-result-v1`，含 `frame_risks`、`track_risks`（明确的 `sampled_visual_frame_sequence`）、`video_risk`、`evidence`、输入质量、模型版本、权重 SHA-256 和运行配置。URL 引用与无帧输入的真实性轴为 `unavailable`/“无法判断”，不是低风险。
>
> 阶段 4 更新（2026-07-15，优先于上述说明）：`GET /health` 的 phase 为 `4-audio-antispoofing-sync-baseline`，并分别返回视觉、音频与同步基线描述。报告新增 `audio_model`（`audio-detector-result-v1`）和 `audio_visual_sync`（`audio-visual-sync-result-v1`）。音频模型结果包含 `whole_audio_risk`、VAD `segment_risks`、`evidence.suspicious_segments`、输入质量、模型版本、许可证与运行配置；其 `model.frozen_artifacts` 逐个记录 AST 三个工件的预期/实际大小、SHA-256 和有效性。推理前任一冻结工件、运行时或显式伪造类标签不可用时为 `unavailable`，不加载或输出风险；分数未经校准。没有音频时音频模型和 VAD 是 `not_applicable`，风险为 `null`，不是低风险。同步基线仅在单脸、足够语音和有效运动样本条件下输出未经校准的筛查风险、候选偏移与异常区间；无音频/视频为 `not_applicable`，无脸、短语音、多脸、低清或依赖缺失为 `unavailable`，不作同步事实断言。官方 518 样本无音频，真实同步 `completed` 可用性验收尚未完成。

> 阶段 5 更新（2026-07-15，历史接口）：`GET /health` 当时的 phase 为 `5-auditable-claim-verification`。文件完成报告新增根 `claim_verification` 与本地工件 `claim_verification.json`（`claim-verification-result-v1`）；`axes.content` 独立保存 `claims`，不与媒体真实性或来源可靠性融合。每条主张有原文、时间追溯、类型、实体、查询、引用与状态。默认受控 `FakeSearchAdapter` 不联网，测试 Mock 引用只验证流水线；阶段 6 已进一步保证 Mock 内容/来源不参与真实冲突或融合规则。

> 正式评测 CLI：`evaluation/build_celeb_df_v2_manifest.py` 只读取 Celeb-DF v2 官方名单并生成带原始视频 SHA-256 的 JSONL manifest；`evaluation/run_celeb_df_v2.py` 强制其 work、预测、配置和汇总目录位于数据集根目录之外，使用固定 FFmpeg 抽帧规则生成逐视频 `celeb-df-v2-prediction-jsonl-v1`；`evaluation/compute_metrics.py` 只消费预测 JSONL，并在输出中保存每个运行状态的计数。它们不读取 URL、不下载模型或数据，也不修改原始数据。

## 已实现

### `GET /health`

```json
{"status":"ok","service":"mingjian-web-api","phase":"7-traceable-web-report"}
```

这不是分析能力、模型能力或系统就绪度声明。

### 任务 API：`/api/v1/jobs`

所有任务持久化在本地 SQLite。状态只有 `queued`、`running`、`completed`、`failed`、`cancelled`；刷新页面或重启服务不会丢失任务元数据。Worker 启动时会把中断前处于 `running` 的任务恢复为 `queued`。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/v1/jobs/upload` | 以 multipart `file` 创建文件任务。允许 JPG/JPEG、PNG、WebP、MP3、WAV、M4A、MP4、MOV、WebM，最大 10 MiB。服务端核对扩展名、声明 MIME 与魔数，生成 SHA-256 和对象键；用户文件名不参与路径。 |
| `POST` | `/api/v1/jobs` | 以 `{"url":"https://…"}` 创建 URL 引用任务。仅绝对 HTTP(S)，拒绝凭据、localhost、`.local`、私网/回环 IP；不请求或抓取 URL。 |
| `GET` | `/api/v1/jobs` | 按创建时间倒序列出任务。 |
| `GET` | `/api/v1/jobs/{id}` | 查询任务。 |
| `POST` | `/api/v1/jobs/{id}/cancel` | 取消 queued/running 任务。 |
| `POST` | `/api/v1/jobs/{id}/retry` | 将 failed/cancelled 任务置回 queued。 |
| `DELETE` | `/api/v1/jobs/{id}` | 删除非 running 任务及其无引用的本地对象。 |
| `GET` | `/api/v1/jobs/{id}/report` | 仅在 completed 返回报告，否则 `409`。 |
| `GET` | `/api/v1/jobs/{id}/media` | 返回已登记的标准化媒体派生物，仅用于本地报告回看；没有派生物为 `404`。 |
| `GET` | `/api/v1/jobs/{id}/frames/{frame_index}` | 返回 `frame_index.json` 显式登记的一张关键帧；不会接受调用方提供的文件路径。 |
| `GET` | `/api/v1/jobs/{id}/artifacts/{name}` | 返回 allow-list 中的 JSON 审计工件（包括 ASR、OCR、质量和模型结果）；stderr、任意路径及二进制文件不暴露。 |

完成报告外层为 `mock: false`，但 `content` 与 `provenance` 轴仍为 `mock: true`。`authenticity` 轴独立保留视觉和音频模态结果，绝不合成为真实性概率或“真/假”结论；可用风险信号、`unavailable` 与 `not_applicable` 均须如实展示。

文件任务的 Job 响应新增可选 `results`，完成报告新增 `preprocess`（同一持久化结果）。其中有 `input_features`、`quality_gates`、受控命令记录和相对路径 `artifacts`。可用工件包括标准化媒体、`metadata.json`、`shot_list.json`、`frame_index.json`、`faces.json`、`vad.json`、`asr.json`、`ocr.json`、`visual_detection.json`、`audio_detection.json`、`audio_visual_sync.json` 与各命令 stderr；它们均位于本地 `derived/<job-id>/`，不含用户文件名。失败任务也保留可用的输入特征、命令及 stderr 工件。URL 引用任务的 `preprocess.status` 为 `skipped`，不会访问网络。

`frame_index.json` 的每帧含 `timestamp_seconds`、`timestamp_quality` 和工件路径。`timestamp_quality=exact` 仅表示该时间来自 FFmpeg `showinfo pts_time`；无法取到足量 PTS 时必须为 `estimated`，且时间戳按递增顺序并限制在媒体时长内。`shot_list.json.keyframe_index` 指向时间上最接近的采样关键帧。任何预算超限均返回 `derived_size_exceeded`，不保留标准化媒体、帧或场景图。

所有 `quality_gates` 只描述可用性或限制，例如 `degraded`、`unavailable` 与 `not_applicable`；它们不是任何真实性、内容或来源风险评分。VAD 是本地能量分段，未提供 ASR、说话人或真实性结论；人脸、ASR、OCR 仍是可替换 Mock 适配器。音画同步默认使用 OpenCV Haar 单脸定位、下半区运动与音频能量相关性筛查；它要求稳定单脸与足够语音，输出的未经校准风险只供复核，不是同步事实或真实性概率。

报告页用 `video`/`audio` 回看标准化媒体，并以报告中已保存的 `timestamp_seconds` 定位关键帧、视觉可疑时段、音频/同步异常区间以及 ASR/OCR 主张来源。当前 Meso4 schema 没有热力图字段，页面会明确写为“未提供”，不生成伪热力图。主张卡展示原文、状态、时间追溯、保存的查询及已校验引用；`mock=true` 的受控引用会明确标记为非真实外部证据。

## 规划但未实现

生产级队列、鉴权、租户隔离、对象存储、URL 抓取，以及学习型唇读/活跃说话人同步检测均未实现。AST 权重不可用时音频模型不会推理。替换 Worker 或存储实现时必须保持三轴独立，且不得将 Mock 字段作为模型结果。

