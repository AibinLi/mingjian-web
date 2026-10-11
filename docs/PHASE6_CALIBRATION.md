# 阶段 6：校准证据与最终状态

阶段 6 已完成，阶段 7 也已完成报告 UI 与只读报告接口；阶段 8 视觉评测、复现和错误分析已经完成，音频、事实核查、融合、消融和鲁棒性在阶段 8 中保持 `not_applicable`。本文件记录校准治理，不把内部校准指标冒充正式测试性能。阶段 7 没有新增模型、阈值、校准或正式实验。

## 测试集泄漏工件隔离

早期 `meso4-phase6-platt-v1.json` 使用了阶段 3 官方 518 条测试预测（SHA-256 `4e7e87b508d7e1a27d8ff56afe8f84b5685a703e9aaf60a9a61fdd3a36adb8eb`），因此无效。它已移至 `<evaluation-artifact-root>\invalid-test-leakage\phase3-celeb-df-v2-20260714` 并附说明，仅作审计证据；禁止运行时、指标、报告和比赛宣传使用。

## 身份隔离独立校准集 v2

- 稳定目录：`<evaluation-artifact-root>\phase6-celeb-df-v2-calibration-subject-disjoint-v2`
- manifest SHA-256：`944e85689080250ef2b939625b0fd3b62e3d932b7010d52b1d310e11e868a8d3`
- predictions SHA-256：`f7883250b2679204e9dece5851e88a886fbaa200a4f1ee3033e53d4776a60abb`
- 数据：400 条，real 200、fake 200；sample_id 400 个且唯一；400 个源文件 SHA-256 全部匹配；400 条预测全部 `completed`、非 Mock。
- 官方名单 SHA-256：`07063d46206e011aef9d1ad8d1854f5b87ae519ba6e452b897cc528c3dcfcdc0`；实际路径重叠 0。
- 身份规则：Celeb-real `idX_*` 与 Celeb-synthesis `idX_idY_*` 都映射到 `celeb/idX`；所有身份组全局固定哈希划分。拟合 327 条/178 个身份组，验证 73 条/38 个身份组，身份交集 0。
- 阶段 3 官方测试没有参与拟合、阈值选择或参数生成。

## 冻结候选与内部 holdout

- 候选 artifact SHA-256：`a90c0572651fac5b163dc5abd40a0a1963971227f0f3b2c588787a7da01c8cd0`
- 参数：intercept `0.542489591903`，slope `-0.813714768671`；冻结后不再修改。
- 内部 holdout 校准前：Brier `0.287117`，Log Loss `0.787779`，ROC-AUC `0.382353`。
- 内部 holdout 校准后：Brier `0.240618`，Log Loss `0.673191`，ROC-AUC `0.617647`。

这些只是独立校准集内部诊断，不是最终测试性能。

## 官方 518 evaluation-only 与拒绝结论

参数冻结后，对阶段 3 官方预测只做一次 evaluation-only 评价；没有拟合、阈值选择或参数修改。固定验收规则为：Brier 与 Log Loss 若同时变差则拒绝。

- 应用前：Brier `0.227362`，Log Loss `0.648944`，ROC-AUC `0.513648`。
- 应用后：Brier `0.255410`，Log Loss `0.704207`，ROC-AUC `0.486352`。
- 结论：Brier 与 Log Loss 同时变差，`deployment_status=rejected`。
- 最终 rejected artifact SHA-256：`c9f4760e46a2e3f951a78f4419ff6cf97f2de13d28f842619df40ba0d0b78665`。
- evaluation SHA-256：`d090f3ac5cbb3c2374b39e79cdd4e9d3561f05dfe7abd86968ac95460e9f044e`。

`CalibrationRegistry` 拒绝 candidate/rejected/inactive 工件。当前 `active-calibrators` 目录没有通过验收的校准器，因此 Web 处于“校准不可用”降级状态，只展示原始未校准 Meso4 分数及限制。

