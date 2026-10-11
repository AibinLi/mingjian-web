# 当前状态

本文是明鉴 Web 当前能力与运行状态的 canonical 来源。阶段开发记录、旧交接和旧任务清单只用于历史审计；发现冲突时，以本文、[ARCHITECTURE.md](ARCHITECTURE.md)、[RUNBOOK.md](RUNBOOK.md) 和实际代码为准。

状态更新时间：2026-08-01。当前真实 E2E 基线已保存为：

- Git branch：`codex/e2e-baseline-20260729`
- Git commit：`1da8712ac34252296bf98531aa1cdd9cb613837e`
- Git tag：`e2e-baseline-20260729`

## 状态定义

| 状态 | 含义 |
|---|---|
| `REAL` | 当前本机 E2E 中真实执行，主要交付行为完整可用 |
| `REAL_BUT_LIMITED` | 真实执行，但适用范围、质量、性能、依赖或证据仍有明确限制 |
| `EXPERIMENTAL` | 有真实实现或输出，但缺少足够验收、标注评测或稳定语义，不应作确定性声明 |
| `MOCK / INTERFACE_ONLY` | 只验证接口、流程或状态契约，不构成现实能力或证据 |
| `UNAVAILABLE` | 当前演示环境缺少必要模型、工件、依赖或可用输入，不能形成该能力结果 |

## 能力矩阵

| 能力 | 状态 | 当前实现 | 不应声称 |
|---|---|---|---|
| Meso4 | `REAL_BUT_LIMITED` | 使用已核验本地权重执行视觉帧风险推理 | 不是伪造概率、真实性概率或可靠通用检测器 |
| Whisper ASR | `REAL_BUT_LIMITED` | 8003 独立 Worker、本地 CPU 模型、带时间戳片段 | 未形成 ASR 准确率或速度保证 |
| Tesseract OCR | `REAL_BUT_LIMITED` | Main 预处理链调用本地 Tesseract，保存帧与时间位置 | 不是任意画质、字体和语言下的可靠全文识别 |
| AST 音频反欺骗 | `UNAVAILABLE` | 接口和加载校验仍在代码中 | 当前模型工件不完整，不能声称已产生可用音频伪造信号 |
| Audio-Visual Sync | `EXPERIMENTAL` | Haar 单脸、下半脸运动与音频能量相关性筛查 | 不是 SyncNet、唇读或同步事实判定；当前启动链默认关闭 |
| Claim Extraction | `REAL_BUT_LIMITED` | 从可定位的 ASR/OCR 文本提取事实型主张并保留来源位置 | 没有文本或时间位置时不能推断视频语义 |
| Tavily | `REAL_BUT_LIMITED` | 8005 Worker 在显式刷新时检索并保存结构化引用 | 首次上传不调用；检索结果不自动证明主张 |
| DeepSeek | `REAL_BUT_LIMITED` | 8006 Worker 接收有界结构化证据包并返回辅助研判 | 不接收原始媒体，不替代三轴结论或人工裁决 |
| Internet Verification | `EXPERIMENTAL` | 校验 DeepSeek 返回的主张/引用索引，只接受已保存非 Mock 引用 | 没有正式事实核查性能评测，不是最终事实裁决器 |
| Source Reliability | `REAL_BUT_LIMITED` | 使用用户声明的来源 URL 与刷新检索记录生成来源证据 | 不能从文件字节恢复完整出处、账号身份或传播链 |
| Aggregate Assessment | `EXPERIMENTAL` | 计算四维复核风险指数、有效权重覆盖率和人工复核路由 | 不是伪造概率、联合模型或自动真伪标签 |
| Report | `REAL` | 8000 页面展示已持久化报告、引用、限制、工件并支持 JSON 下载 | 浏览器不重新计算模型结论；PDF 导出尚未实现 |

## 当前服务

| 端口 | 服务 | 当前用途 |
|---:|---|---|
| 8000 | Main API + Web | 唯一网页入口、任务处理和报告 |
| 8003 | Whisper Worker | ASR |
| 8005 | Tavily Worker | 显式报告刷新检索 |
| 8006 | DeepSeek Worker | 显式报告刷新辅助研判 |

网页入口为 <http://127.0.0.1:8000>。前端由 8000 静态提供，不需要单独启动 5173 或 Vite。

## 当前 E2E 行为

### 2026-08-01 Demo Replay 回归修复

- 固定样例 `iphone20-conflicting-20260730` 的媒体 SHA-256 仍为 `baa814883bda7b2608836765cbb1fee5545e1664534151535405db4bacbed713`。OCR 超时修复后，当前流水线在原 13 条 ASR claims 外新增两条可定位但不可核验的 OCR claims（`= -`、`eee POTEET =`），使 claim fingerprint 从 `860591f3...` 变为 `0e285e61...`；11 条可核验查询及顺序未变化。
- 受控 Replay 已更新当前 claim fingerprint 和 allow-list 文件哈希，媒体 SHA、文件哈希、claim fingerprint、query set 与 `llm_analysis.status=completed` 校验均保持 fail-closed。没有放宽匹配，也没有在 Demo 模式启动或调用 Tavily/DeepSeek。
- 指定样例真实 Demo E2E 已通过：Replay sample 为 `iphone20-conflicting-20260730`，冻结 DeepSeek 与 Internet Verification 均为 `completed`，联网结论为冲突、联网反证风险指数 `70.0`；当前 Aggregate 重新计算 `overall_score=83.73`。这是单样例运行验收，不是事实核查、模型准确率或性能声明。
- 已从开发仓库白名单路径重建独立离线包 `release/mingjian-web-demo-20260801-r2.zip`，由 `scripts/build-release.ps1` 构建；文件数 290，大小 1,089,509,707 bytes，SHA-256 `9bc3cc7e94749c864a4d3fb4fb12f95ba2618e47c30b9379f893c0e946196c85`。未覆盖比赛验证副本，且不包含 `.env`、虚拟环境、运行任务数据、缓存字节码或用户视频。

### 2026-08-01 OCR 超时边界

- OCR 仍最多均匀处理 12 张 FFmpeg 派生关键帧，语言保持 `chi_sim+eng`。单帧 Tesseract 上限为 30 秒；内部总预算按 `min(300, 30 + 20 * N)` 计算，OCR 专属外层监督预算按 `min(360, total + 60)` 计算，其他 optional module 仍使用原 10 秒边界。
- B5 同一五帧视频的 release-copy 实测 OCR 为 `completed`：独立外层 smoke 约 11.35 秒，完整 Online 媒体任务处理 5/5 帧并保存 63 条 OCR 项，没有 optional-module timeout。这是运行稳定性验收，不是 OCR 准确率或吞吐性能声明。
- 随后的 B5-B5 复测中，独立 Tavily 检索第一次即成功；同一视频完整 Online E2E 的 OCR、Tavily、DeepSeek、Internet Verification 与 Aggregate 均为 `completed`。Tavily 保存 85 条非 Mock 引用，Internet Verification 风险复核分为 10.0，Aggregate overall score 为 59.5；这些是单次运行验收结果，不是准确率或性能声明。

### 基础链

```text
上传视频 -> FFmpeg/FFprobe -> Meso4 -> Whisper -> Tesseract
         -> Claim Extraction -> 初始报告
```

基础链不调用 Tavily 或 DeepSeek。外部网络或供应商 API 不可用时，已经完成的基础报告仍应保留。

### 联网增强链

```text
已有完成报告
  -> 点击“重新生成 DeepSeek 研判”
  -> Claim Verification / Tavily
  -> Provenance Verification
  -> 构造结构化证据包
  -> DeepSeek 辅助研判
  -> Internet Verification
  -> Aggregate 重算
  -> 保存并重新展示报告
```

## 关键语义

- Meso4 分数只能作为未经校准的风险信号。
- `insufficient_evidence` 表示合格证据不足，不代表联网失败，也不代表主张虚假。
- `network_error` 才表示 DNS、连接、代理或超时导致联网步骤未完成。
- DeepSeek 是辅助研判器，不是事实、真伪或处置裁决器。
- 三轴原始状态始终独立保存；Aggregate 不能覆盖它们。
- 缺失、不可用、Mock 和不适用不是低风险证据。
- 阶段 6 的 `rejected` 校准器仍禁止加载。

## 验证状态

- Demo Replay 定向回归为 `42 passed`；修正 ASR 环境覆盖测试读取导入时缓存常量的问题后，正式 `tests/` 全套为 `166 passed, 2 skipped`。
- Demo 启动实测仅 8000/8003 监听，8005/8006 未启动；Online 启动实测四服务均健康且只读取本地健康接口，未发送 Tavily/DeepSeek 请求。
- Meso4 唯一正式冻结评测是获授权 Celeb-DF v2 官方 518 视频：ROC-AUC `0.513648`，TN=1、FP=177、FN=1、TP=339。模型几乎全部预测为 fake，结果不支持可靠通用检测声明。
- 音频、事实核查、融合、消融和鲁棒性没有获授权冻结标签 manifest，阶段 8 结果为 `not_applicable`，不能报告性能数字。
- 历次真实命令和输出见 [TESTING.md](TESTING.md)，模型评测边界见 [PHASE3_EVALUATION.md](PHASE3_EVALUATION.md) 与 [PHASE8_EVALUATION.md](PHASE8_EVALUATION.md)。

## 当前不在范围内

- 公网或多租户生产部署。
- 自动封禁、自动处置、违法判定或单一真伪标签。
- 真实 URL 视频下载、平台抓取或传播链自动恢复。
- 浏览器插件、移动端、系统级抓流或 Agent Skill。
- 已完成异机验收的便携运行包。
- 模型或依赖的运行时自动下载。

具体操作见 [RUNBOOK.md](RUNBOOK.md)，详细限制见 [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md)。

