# 最终归档与复现清单

## 应提交的仓库内容

- 源码：`backend/`、`frontend/`、`evaluation/`、`tests/`、`deploy/`、`Dockerfile`、`docker-compose.yml`。
- 启动与安全说明：`README.md`、`deploy/README.md`、`.env.example`。
- 工程与实验文档：`docs/ARCHITECTURE.md`、`docs/INTERFACES.md`、`docs/THREAT_MODEL.md`、`docs/PHASE3_EVALUATION.md`、`docs/PHASE6_CALIBRATION.md`、`docs/PHASE8_EVALUATION.md`、`docs/TESTING.md`。
- 最终材料：`docs/FINAL_SUBMISSION_REPORT.md`、`docs/DEFENSE_QA.md`、`docs/DEMO_RUNBOOK.md`、`docs/REHEARSAL_LOG.md`、`docs/THIRD_PARTY_NOTICES.md` 和本文件。
- 交接记录：`docs/CURRENT_STATE.md`、`docs/TASKS.md`、`docs/DECISIONS.md`、`docs/HANDOFF.md`、`docs/OPEN_QUESTIONS.md`。

## 不应提交的内容

- 用户媒体、原始 Celeb-DF 数据、抽取帧、任务数据库和派生物。
- Meso4、AST、OpenCV wheel 等未获再分发授权或体积大的第三方工件。
- 密钥、`.env`、运行日志中可识别信息、虚拟环境和 Docker 卷。
- 测试泄漏校准工件；它们只能保留在仓库外 `invalid-test-leakage` 审计目录，禁止运行时、指标、报告和宣传使用。

## 复现最小步骤

1. 校验已获授权的模型/数据工件与 `models/*.md` 中 SHA-256 一致；没有工件时，系统应降级而不是下载。
2. 运行 `docker compose up --build -d`，再运行 `docker compose ps` 和 `curl.exe -sS http://127.0.0.1:8000/health`。
3. 如拥有 `<authorized-evaluation-artifacts>\phase3-celeb-df-v2-20260714` 中的授权冻结输入，运行：

   ```powershell
   .\.venv\Scripts\python.exe evaluation\run_phase8.py --suite-manifest evaluation\phase8_suite.celebdf_v2_20260715.json --output-dir evaluation\phase8-runs\phase10-rehearsal-phase8
   ```

4. 执行 `docs/DEMO_RUNBOOK.md` 中的现场案例；结束后执行 `docker compose down -v` 清理演示数据。

## 冻结实验证据

- suite manifest SHA-256：`1e2ca588cab5956a7894d75a8a9dc59ee035879440b932fbc96be21da35317a6`。
- 原始视觉预测 SHA-256：`4e7e87b508d7e1a27d8ff56afe8f84b5685a703e9aaf60a9a61fdd3a36adb8eb`。
- 视觉指标 SHA-256：`e2e4d38496a425f9e2e471456252842b334aa15e8e9a36020ded61a559db1d6a`。
- 冻结 Phase 8 输出与命令记录被 `.gitignore` 排除；归档包应在受控存储中保留其 JSON/CSV、哈希和访问说明，而非提交授权数据。

## 提交前人工核对

- [ ] 主办方作品报告模板已提供，且已将 `FINAL_SUBMISSION_REPORT.md` 逐栏粘贴并保留限定语。
- [ ] 所有队伍身份字段、原创性声明和数据授权材料由实际权利人填写/签字。
- [ ] 不含受限模型、数据、用户媒体、密钥或测试泄漏工件。
- [ ] `docs/THIRD_PARTY_NOTICES.md` 已随包，且许可与竞赛规则兼容性已人工确认。
- [ ] 现场机器已完成一次离线 Docker 启动和授权媒体演示。


