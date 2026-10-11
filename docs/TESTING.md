# 测试记录

## 2026-08-01 release r2 打包校验

- 使用 `powershell -ExecutionPolicy Bypass -File .\scripts\build-release.ps1` 从开发仓库白名单重新构建 release；构建脚本明确包含 `RUN_ME_FIRST.txt`，并拒绝覆盖已有 ZIP。
- r2 原始 ZIP 检查结果：290 个文件条目，大小 1,089,509,707 bytes，SHA-256 `9bc3cc7e94749c864a4d3fb4fb12f95ba2618e47c30b9379f893c0e946196c85`。入口文件全部存在；`.env`/`.venv`/`.git`/缓存字节码/运行数据/用户视频/真实 Key 排除命中 0；`manifests`、`wheels`、`models`、`tools`、`runtime` 均存在。该检查只针对打包内容，不执行业务逻辑或供应商调用。

## 2026-08-01 Demo Replay 回归修复

- 使用 SHA-256 `baa814883bda7b2608836765cbb1fee5545e1664534151535405db4bacbed713` 的固定视频，在 `DEMO_MODE=true`、仅 8000/8003 监听的开发仓库服务中复现 `/refresh-llm` HTTP 409：`demo_replay_claim_fingerprint_mismatch`。
- 当前 ASR 13 条 claims 的 fingerprint 仍为旧值 `860591f3...`；OCR `completed` 后增加 2 条不可核验 claims，完整 fingerprint 为 `0e285e61...`。Replay 与当前流水线的 11 条 query 文本和顺序完全相同，无缺失或多余 query。
- 更新受控样例后，同一真实任务刷新得到 Replay sample `iphone20-conflicting-20260730`、DeepSeek `completed/replayed=true/live_llm_performed=false`、Internet Verification `completed/conflicting/risk_score=70.0/live_retrieval_performed=false`，当前 Aggregate 重算 `overall_score=83.73`。
- 新增 Replay 定向测试及 Claim/Aggregate/OCR/health 相关回归结果为 `42 passed in 4.99s`。第一次正式 `tests/` 全套为 `165 passed, 2 skipped, 1 failed in 50.67s`：ASR 环境覆盖测试在设置环境后错误断言了模块导入时缓存的默认常量；改为调用与生产初始化相同的 `_asr_timeout_from_env()` 后重跑为 `166 passed, 2 skipped in 51.71s`，生产 ASR 超时逻辑未改变。一次未限定目录的 pytest 在收集既有受限 `tmp/pytest-*` 时权限失败，未计作测试结果。
- Online 模式启动 8000/8003/8005/8006 并读取本地 health 成功；没有调用 Tavily 搜索或 DeepSeek 分析接口，没有泄露 Key。验证结束后四服务均已停止。
- 重建 `release/mingjian-web-demo-20260801.zip` 后使用 ZIP 结构 API 核验 288 个条目：修复后的 Replay、`demo_replay.py`、setup 与 Demo/Online 启动脚本均存在，敏感/运行目录规则命中 0。包大小 1,089,508,143 bytes，SHA-256 `93c7c188dc540cc252b409ca6810ee8f79586675479249a35a4fda81db60ef4f`。

## 2026-07-28 刷新路径真实检索接线

- 主张兼容字段、Tavily 日期降级、刷新时真实检索适配器和健康接口开关回归通过；新增 8005 Tavily Worker 的 `/health`、缺少 Key 与 DNS 故障测试。DeepSeek 大证据包压缩修复后的完整可运行范围结果为 `153 passed, 2 skipped, 2 deselected, 1 warning in 32.29s`。测试使用内存 HTTP transport，不产生 Tavily/DeepSeek 付费调用；真实联网成功 smoke 仍待单独授权样本验收。
- 修复 API/Worker 分进程开关传播：适配器会在 internal-only 请求中传递 API 已计算的启用状态，Worker 保留自身开关供直接 smoke 使用。定向回归为 `30 passed, 1 warning in 8.87s`；未发送真实 Tavily/DeepSeek 请求。
- DeepSeek 8006 模块入口、`/analyze` 兼容端点、缺少 Key 的 `not_configured` 响应，以及 refresh-only LLM 健康检查回归通过：`56 passed, 1 warning in 17.52s`。未发送真实 DeepSeek 请求。
- 多主张、多引用中文证据包的两级压缩回归通过：`29 passed, 1 warning in 6.49s`。该测试验证适配器不会因本地长度边界跳过 Worker 请求。

## 2026-07-28 可选模块稳定性加固

- 静态执行 `python -m py_compile ...` 和 `python -m compileall -q backend/app` 均通过；`git diff --check` 通过，仅出现工作区既有 LF/CRLF 提示。
- 定向执行主张刷新持久化、聚合、阶段 9 安全、ASR/OCR/来源和联网 Worker 用例，结果为 `60 passed, 1 warning in 8.02s`。
- 安装锁定的 `tenacity==9.0.0` 到临时 `tmp/test-deps` 后执行全套：首次为 `135 passed, 2 skipped, 3 failed`。修复新增人脸质量门控文案并补齐刷新覆盖持久化、`network_error` 排除和错误脱敏回归后，排除两个需要 Torch 的既有 Meso4 用例再执行，最终结果为 `138 passed, 2 skipped, 2 deselected, 1 warning in 22.41s`。
- 两个 deselect 的真实原因是当前系统 Python 没有安装 `torch`，分别为 Meso4 detector 与视觉 evaluation runner；权重文件仍存在且 SHA-256 匹配。未为测试安装或改变 Torch/Transformers。两个 skip 仍是需要显式本地 FFmpeg 授权的真实媒体 smoke。
- 当前环境没有 `docker` 命令，因此未运行 `docker compose config --quiet`、镜像构建或容器 smoke。YuNet ONNX、容器内 Whisper、OpenCV 同步 completed 和真实联网成功链路仍未验收，不得据此报告性能或覆盖率。
- 2026-07-28 ASR 集成修复后，定向回归 `39 passed, 1 warning in 12.99s`。随后使用既有 13.6 秒音频工件，经 `RemoteAsrAdapter` 和预处理外层子进程真实调用当前 localhost Worker，约 30 秒返回 `status=completed`、英文 transcript 和 2 个时间戳片段；该 smoke 只验证路径、超时和 HTTP 集成，不构成 ASR 准确率或吞吐声明。
- 同步 Worker 环境超时、HTTP 诊断和回归测试后再次执行全套可运行范围，结果为 `141 passed, 2 skipped, 2 deselected, 1 warning in 25.90s`；两个 deselect 仍仅因当前测试解释器没有安装 Torch。

## 2026-07-19 联网属实性与四维综合评分

- 实际执行 `.\.venv\Scripts\python.exe -m pytest tests\test_aggregate_assessment.py tests\test_claim_verification.py tests\test_phase_six.py tests\test_health.py tests\test_jobs.py -q`，结果为 `64 passed in 23.72s`。
- 回归覆盖：真实引用索引范围/归属复核、Mock 引用不生成低风险分数、统一维度公式、可配置跨维度权重、65% 覆盖门槛、关键联网维度缺失强制人工干预、两个新工件持久化与 allow-list 读取、健康接口评分配置声明。
- 首次收集发现当前 Python 3.9 环境无法让 FastAPI 求值路由参数的 `str | None`；已将上传接口的可选原始链接注解改为 `Optional[str]` 后重新执行并通过。该兼容性修正不改变接口字段或安全边界。
- 本地浏览器在临时数据目录和 `127.0.0.1:8011` 创建一个不联网 URL 引用任务，完成报告实际显示“联网搜索属实性核验”和“四维综合评分报告”；四个维度均无有效证据时显示 0% 覆盖、总体值无法计算和“需人工干预”。页面控制台无 error。测试后已停止服务并删除临时任务目录。
- 随后实际执行 `.\.venv\Scripts\python.exe -m pytest tests -q -rs`，结果为 `126 passed, 2 skipped in 15.14s`；补充零权重和重叠区间配置防错测试后再次执行全套，最终为 `127 passed, 2 skipped in 14.70s`。两个 skip 均为需要显式本地工具授权的真实 FFmpeg/FFprobe smoke。`py_compile`、`docker compose config --quiet` 与 `git diff --check` 同时通过，差异检查仅报告既有 LF/CRLF 转换提示。
- 该测试只验证状态、公式、持久化和 UI，不构成真实联网事实核查、来源判断、融合阈值或真假分类性能评测。

## 2026-07-18 DeepSeek 报告辅助研判链路

- 实际执行 `python -m pytest tests/test_claim_verification.py tests/test_jobs.py tests/test_health.py -q -rA`、`python -m compileall -q backend/app` 与 `docker compose config --quiet`，命令成功结束；新增测试覆盖有界证据包不包含本地对象键、LLM 结果工件持久化、可选原始公开链接登记与健康接口的分析器声明。
- 实际执行 `docker compose up -d --build --wait` 后 `deepseek`、`api`、`gateway` 均为 `healthy`。`/health` 返回 `llm_report_analysis.adapter=DeepSeekReportAnalysisAdapter` 与 `raw_media_sent=false`。
- 在不含用户媒体的最小证据包上，internal-only Worker 返回 `status=completed`、`has_analysis=true`；随后创建一个不下载的 `https://example.org/original-post` URL 引用任务，任务完成后报告保存 `deepseek_analysis`，`llm_status=completed`、`provider=DeepSeek`、`has_analysis=true`。该烟雾仅验证调用与持久化，不构成内容、来源或模型性能结论。
- 后续对一个真实媒体报告的刷新发现，该服务在长中文证据包上会把生成结果替换为问号；最小中文回显测试并不复现。修复为 ASCII JSON 转义、仅提交规范化媒体信号和强制 ASCII 英文研判。实际创建不下载的 `https://example.org/llm-readable-smoke` 任务后，报告返回 `llm_status=completed`、`ascii_only=true`、`question_marks=0`。UI 已将研判置于首屏，并增加 `POST /api/v1/jobs/{job_id}/refresh-llm` 以只刷新该工件。
- 在用户要求中文正文后，已将过长中文生成改为“极短英文四点摘要 + 至多两段短英文→简体中文翻译”。实际新建 `https://example.org/chinese-summary-smoke` 任务后，以 UTF-8 读取报告得到 `analysis_language=zh-CN`、中文字符 115、问号 0。PowerShell 管道若按本机代码页解码 UTF-8，会产生终端假性乱码；浏览器 JSON 解析不受此影响。

## 阶段 9 最终安全验收（2026-07-17）

实际执行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q -rs
.\.venv\Scripts\python.exe -c "import os,sys,pytest; os.environ['MINGJIAN_REAL_MEDIA_SMOKE']='1'; sys.exit(pytest.main(['tests','-q','-rs']))"
docker compose config --quiet
```

最终代码实际以 `MINGJIAN_REAL_MEDIA_SMOKE=1` 运行完整套件，结果为 `121 passed, 2 warnings in 40.78s`；两条 warning 均为既有 AST Mel 滤波器配置警告。Windows 真实 FFmpeg/FFprobe 回归已通过，未出现 Python 辅助进程导致的 0xC0000005。回归覆盖本地主机 Host、同源写请求、ASGI 无长度/并发上传、Windows/POSIX FFmpeg 启动分支、协议白名单、Docker 网关来源和 URL 规范化/离线边界；所有安全夹具只验证防御，不包含可传播攻击工具。

实际本地端到端烟雾：以 Uvicorn 监听 `127.0.0.1:8010`，上传一个受控 6,864-byte PNG 后任务在真实 FFprobe/FFmpeg 下达到 `completed`，随后 `DELETE /api/v1/jobs/{id}` 返回 `204` 并清理该任务。模型缺失/不可用状态仍按既有契约如实降级，未据此声明模型性能。

最终 gateway/API Compose 验收已通过。API 镜像 SHA-256 `6f4fd7169c3cd0a3b5e7af6f2bb9ba140f56ad0b6d014e624c8afec14596b6ee`，大小 520,057,002 bytes；运行环境为 `torch==2.5.1+cpu`、`torch.version.cuda is None`、FFmpeg `5.1.9-0+deb12u1`、API UID/GID 10001。两个容器均 healthy、read-only、`cap_drop=ALL` 与 `no-new-privileges`；API 无宿主机 ports、只连接 internal 网络，固定 digest 非 root gateway 唯一发布 `127.0.0.1:8000`。页面与 `/health` 返回 200，安全响应头正常，`url_fetch=disabled_by_default`，视觉校准为 `unavailable`，API 外部网络访问失败。受控 PNG 从 queued 到 completed，`preprocess=completed`，真实性轴 `unavailable`；DELETE 返回 204，之后查询 404，任务/对象文件均为 0。恶意 Host 400、恶意 Origin multipart 403、回环 SSRF URL 422、12 MiB 上传 413。`docker compose down -v` 已成功删除容器、网络和测试数据卷；日志未包含密钥或用户媒体内容。

## 2026-07-18 MP4 标准化内存回归

- 短 H.264/AAC MP4 在原 768 MiB POSIX `RLIMIT_AS` 下稳定复现：`x264 [error]: malloc of size 5552768 failed`，随后 `Error submitting video frame to the encoder`。
- 使用同一受控 FFmpeg 命令、移除旧单进程地址空间限制后成功标准化；对刚生成的标准 MP4 施加 1 GiB 地址空间限制再次转码也成功。容器当时仅使用 114.1 MiB / 2 GiB，确认不是 Docker 容器总内存耗尽。
- 已将 helper 上限改为 1 GiB，并新增单元断言，确保地址空间限制仍会施加且不会回退到 768 MiB。

## 2026-07-18 Compose Meso4 只读挂载验证

- `docker compose config --quiet` 通过；解析结果确认 `models/cache/deepfakebench` 以只读 bind mount 映射到 `/app/models/cache/deepfakebench`。
- 强制重建 API 容器后 API/gateway 均为 `healthy`；`GET /health` 实际返回 `visual_detector.weight_available=true`，文件大小为 117,943 bytes，SHA-256 为冻结值 `a362ebbef2658daa551d808fbfccd57f0b5fd34fda46a05996e807cccddff4e0`。该检查只验证本地工件可见性和完整性，不构成新的性能评测。

## 2026-07-18 Compose AST 只读挂载验证

- `docker compose config --quiet` 通过；`test_audio_phase_four.py` 与 `test_phase9_security.py` 实际结果为 `43 passed`。
- 强制重建 API 容器后 `GET /health` 实际返回 AST 三个冻结工件均为 `exists=true`、`valid=true`，并分别匹配记录的大小和 SHA-256；没有下载模型、启用联网检索或修改校准状态。

## 阶段 8 评测复现与回归（2026-07-15）

实际执行：

```powershell
.\.venv\Scripts\python.exe evaluation\run_phase8.py --suite-manifest evaluation\phase8_suite.celebdf_v2_20260715.json --output-dir evaluation\phase8-runs\phase8-celeb-df-v2-20260715-final
.\.venv\Scripts\python.exe -m pytest tests -q -rs
.\.venv\Scripts\python.exe -c "import os,sys,pytest; os.environ['PATH']=r'<ffmpeg-bin>;' + os.environ.get('PATH',''); os.environ['MINGJIAN_REAL_MEDIA_SMOKE']='1'; sys.exit(pytest.main(['tests','-q','-rs']))"
.\.venv\Scripts\python.exe -m py_compile backend\app\main.py backend\app\preprocess.py backend\app\visual_detector.py backend\app\audio_detector.py backend\app\audio_visual_sync.py backend\app\claim_verification.py backend\app\phase_six.py evaluation\run_phase8.py evaluation\run_visual_baseline.py evaluation\run_celeb_df_v2.py
```

结果：阶段 8 命令完成，视觉 raw prediction SHA-256 为 `4e7e87b508d7e1a27d8ff56afe8f84b5685a703e9aaf60a9a61fdd3a36adb8eb`，`metrics_table.csv` 与 `failure_cases.csv` 已由 raw JSONL 自动生成。最终完整真实 FFmpeg smoke 回归为 `87 passed, 2 warnings in 36.46s`；两条 warning 仍为既有 AST Mel 滤波器配置提示。`py_compile` 通过。

阶段 0、1、2、3、4、5、6、7 已完成。以下是实际运行记录；未报告任何带标签的音频、同步、主张分类、检索或事实核验性能指标。

## 阶段 7 报告 UI 回归（2026-07-15）

实际执行：

```powershell
.\.venv\Scripts\python.exe -c "import os,sys,pytest; os.environ['PATH']=r'<ffmpeg-bin>;' + os.environ.get('PATH',''); os.environ['MINGJIAN_REAL_MEDIA_SMOKE']='1'; sys.exit(pytest.main(['tests','-q','-rs']))"
.\.venv\Scripts\python.exe -m py_compile backend\app\main.py backend\app\preprocess.py backend\app\visual_detector.py backend\app\audio_detector.py backend\app\audio_visual_sync.py backend\app\claim_verification.py backend\app\phase_six.py
git diff --check
```

最终真实回归为 `75 passed, 2 warnings in 36.45s`。两条 warning 均为已知 AST Mel 滤波器配置警告，来自真实媒体 smoke 测试；没有 skipped、failed 或 error。阶段 7 的回归覆盖 PNG、JPEG、WebP、WAV 四种受控上传案例的上传→完成报告、ASR/OCR JSON 审计接口、标准化媒体回看接口以及图像/视频关键帧接口；URL 引用任务则覆盖完成报告无标准化媒体时的明确降级说明。

可重复浏览器验收步骤：

1. 启动 API 与前端静态服务，创建并打开一个完成的 URL 引用任务报告，确认页面不出现 `undefined`，并显示“没有可播放的标准化媒体：可能是URL引用、预处理失败或派生物已清理。”。
2. 打开一个完成的文件任务报告，确认标准化媒体、视觉关键帧、质量说明、ASR/OCR 差异、主张卡和审计信息可见。
3. 在 390px 视口下重复打开 URL 与文件报告，确认布局单列可读，时间跳转按钮在无播放器时不报错。
4. 使用缺失关键帧、缺失 ASR/OCR 或缺失 `phase6_quality` 工件的完成任务报告做只读检查，确认页面以缺失/不可用文案降级，浏览器 console 无 error。

本次浏览器实测覆盖 URL 引用完成任务报告、文件完成任务报告和 390px 响应式布局，console 无 error，URL 报告无 `undefined`。此浏览器检查是 UI 回归，不构成新的模型实验或性能评测。

## 阶段 6 最终真实回归（2026-07-15）

实际按指定环境运行完整套件：

```powershell
.\.venv\Scripts\python.exe -c "import os,sys,pytest; os.environ['PATH']=r'<ffmpeg-bin>;' + os.environ.get('PATH',''); os.environ['MINGJIAN_REAL_MEDIA_SMOKE']='1'; sys.exit(pytest.main(['tests','-q','-rs']))"
```

同配置 JUnit 记录为：`75 passed`、`0 failures`、`0 errors`，总时间 `35.028s`。两条实际 warning 来自两个真实媒体测试中的同一 AST Mel 滤波器配置警告：

```text
UserWarning: At least one mel filter has all zero values. The value for
`num_mel_filters` (128) may be set too high. Or, the value for
`num_frequency_bins` (257) may be set too low.
```

阶段 6 新覆盖：校准器规范哈希复算、非有限参数/非法阈值/模型 key 和权重不匹配、candidate/rejected 拒绝、阶段 3 正式预测哈希拒绝、官方名单原文件哈希和实际路径重叠复算、重复 sample_id/Mock/manifest 不一致拒绝、统一可见人物身份全局划分且拟合/验证交集为 0、Mock conflicting 隔离、缺失模态非低风险，以及 VAD 不可用/无语音/短语音/足够语音状态。独立证据复算为 400 条、200 real/200 fake、400 个源哈希匹配、400 completed 非 Mock、官方测试路径重叠 0。

冻结候选内部 holdout 指标仅作校准诊断；官方 518 只作参数冻结后的 evaluation-only 验收。候选令 Brier `0.227362 -> 0.255410`、Log Loss `0.648944 -> 0.704207`，已标为 `rejected`，没有活动校准器。

## 阶段 5 可审计主张核验回归（2026-07-15）

最终实际执行（同一 Python pytest 进程设置真实 FFmpeg 与真实媒体烟雾环境）：

```powershell
.\.venv\Scripts\python.exe -c "import os,sys,pytest; os.environ['PATH']=r'<ffmpeg-bin>;' + os.environ.get('PATH',''); os.environ['MINGJIAN_REAL_MEDIA_SMOKE']='1'; sys.exit(pytest.main(['tests','-q','-rs']))"
```

实际结果：`54 passed, 2 warnings`。两条警告都来自真实媒体两项测试中的 `transformers.audio_utils` AST Mel 滤波器配置：

```text
UserWarning: At least one mel filter has all zero values. The value for
`num_mel_filters` (128) may be set too high. Or, the value for
`num_frequency_bins` (257) may be set too low.
```

本次收尾会以本文件后续记录的指定同进程真实回归为准。阶段 5 测试使用带时间戳的受控 ASR/OCR、主张和引用夹具，覆盖五种主张状态（支持、反对、冲突、背景、暂无证据）、时间追溯、ISO-8601 发布时间/抓取时间、URL/域名/逐字片段拒绝、恶意字幕指令隔离，以及 `claim_verification.json` 工件与来源可靠性独立 Mock 轴。它们不代表默认用户上传流程具备真实 ASR/OCR、真实外部证据或事实核查能力；Mock 内容轴不得用于阶段 6 校准或真实融合。

## 阶段 4 AST 与 Haar/运动同步筛查回归（2026-07-14）

实际执行（在同一 Python pytest 进程内设置环境，以规避执行器不传递 PowerShell 临时
环境的限制）：

```powershell
.\.venv\Scripts\python.exe -c "import os, sys, pytest; os.environ['PATH']=r'<ffmpeg-bin>;' + os.environ.get('PATH',''); os.environ['MINGJIAN_REAL_MEDIA_SMOKE']='1'; sys.exit(pytest.main(['tests','-q','-rs']))"
```

本次实现后实际执行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_audio_phase_four.py tests\test_health.py tests\test_jobs.py -q
```

2026-07-15 最终实际执行本节首条同进程命令，完整套件结果为 `46 passed, 3 warnings`。
其中新增/覆盖：AST 三个冻结文件的错误权重、错误 config、缺失 processor config 的拒绝，
processor/model 单次 CPU 惰性加载缓存，以及真实 FFmpeg 媒体闭环。3 条警告均为：

```text
UserWarning: At least one mel filter has all zero values. The value for
`num_mel_filters` (128) may be set too high. Or, the value for
`num_frequency_bins` (257) may be set too low.
```

该警告来自 `transformers.audio_utils`，未被静默忽略。

该次运行覆盖：Meso4 视觉回归、阶段 4 报告/工件、真实 FFmpeg/FFprobe 的 WAV/MP3
压缩、白噪声混合、`atempo` 变速、背景音乐混合、0.12 秒短音频和无音频 MP4；无音频
音频与同步结果均为 `not_applicable`。有音频但无可用单脸的真实黑色视频同步结果为
`unavailable`，且 `risk_score=null`；没有把缺失条件映射为低风险。AST 权重存在时测试强制
完成推理。此测试不构成音频或同步模型效果评测。

## 阶段 3 历史收尾验收（2026-07-14）

实际执行：

```powershell
$env:Path='<ffmpeg-bin>;' + $env:Path
$env:MINGJIAN_REAL_MEDIA_SMOKE='1'
.\.venv\Scripts\python.exe -m pytest tests -q -rs
```

该节保留阶段 3 的历史命令背景；阶段 4 当前最终记录以本文件上方 2026-07-15 的同进程
`46 passed, 3 warnings` 为准。后者覆盖真实 FFmpeg/FFprobe 媒体烟雾、真实 Meso4 CPU 推理、
AST 冻结校验、真实性/Mock 三轴报告、Meso4 权重降级、评测 manifest/抽帧规则、预测 JSONL
和指标重算。

同时执行相关 Python 文件的 `py_compile`、`git diff --check` 与 `git status --short`；具体最终命令和输出以本次收尾记录为准。

## 阶段 3 正式评测

用户已授权使用 Celeb-DF v2。实际评测 518/518 完成，real 178、fake 340，运行 103.61 秒。指标仅从原始预测 JSONL 重算：Accuracy 0.656371、Precision 0.656977、Recall 0.997059、F1 0.792056、ROC-AUC 0.513648；TN=1、FP=177、FN=1、TP=339。

模型几乎全部预测为 fake，Accuracy 接近始终预测 fake 的类别基准，ROC-AUC 接近随机。Meso4 分数未经校准，不能作为真实性概率或可靠通用检测能力的证据。详见 `docs/PHASE3_EVALUATION.md`。

