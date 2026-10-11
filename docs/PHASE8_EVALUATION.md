# 阶段 8：真实评测、复现与错误分析

阶段 8 已完成评测流水线与一次真实重算交接；没有开始部署，没有新增检测模型、阈值、校准器、真实检索或产品功能。

## 冻结输入与命令

- Suite manifest：`evaluation/phase8_suite.celebdf_v2_20260715.json`
- 随机种子：`mingjian-phase8-frozen-20260715`
- 复算命令：

```powershell
.\.venv\Scripts\python.exe evaluation\run_phase8.py --suite-manifest evaluation\phase8_suite.celebdf_v2_20260715.json --output-dir evaluation\phase8-runs\phase8-celeb-df-v2-20260715-final
```

该命令覆盖视觉、音频、事实核查、融合、消融和鲁棒性六类实验。只有视觉轴存在授权冻结标签预测文件；其余实验在 suite manifest 中显式 `not_applicable` 并写明原因，不填手工数字。

## 输出产物

输出目录：`evaluation/phase8-runs/phase8-celeb-df-v2-20260715-final`（Git 忽略，保留本地审计产物）

- `phase8_summary.json` SHA-256：`90f2372202fdfb74461c9ba9d0a40e9ff6947ed5d276c81ca54477d921ea9fca`
- `frozen_run.json` SHA-256：`70085e5109ecec804ab2b69a22c7a6fc066e3a54b3d63db73008eccecf6ab651`
- `commands.json` SHA-256：`637423d77ba6604c2c3935870f7c6fcfcf6e477af0494dc2fefe634511f357c4`
- `metrics_table.csv` SHA-256：`e397486d36b9b113cec8e4c59acbfa5935bcf58e0e8496425a9f5b618904a714`
- `failure_cases.csv` SHA-256：`3786799eee5dc48e3d596aeebbeca0988f717024e4f52f3ae92b1d0b67fd84d6`
- 视觉 raw predictions 副本 SHA-256：`4e7e87b508d7e1a27d8ff56afe8f84b5685a703e9aaf60a9a61fdd3a36adb8eb`

`frozen_run.json` 记录命令、环境、Git 提交/dirty 状态、suite manifest SHA、输入 manifest/config/predictions 哈希、输出路径和随机种子。指标表、ROC SVG/CSV 和 failure cases 均由 raw prediction JSONL 自动生成。

## 视觉主实验结果

输入：阶段 3 保留的 Celeb-DF v2 官方 518 条 Meso4 raw predictions。没有删除困难样本，没有修改预测，没有用测试集结果调参。

- 样本：518；real 178，fake 340；completed 518，skipped 0
- 阈值：0.5（冻结配置；未根据阶段 8 结果选择）
- Accuracy：`0.656371`
- Precision：`0.656977`
- Recall：`0.997059`
- F1：`0.792056`
- ROC-AUC：`0.513648`
- EER：`0.488764`
- 误报率 FPR：`0.994382`
- 漏报率 FNR：`0.002941`
- Brier：`0.227362`
- Log Loss：`0.648944`
- ECE 10-bin：`0.046971`
- 延迟：mean `0.040330s`，p50 `0.031s`，p95 `0.047s`，max `3.453s`
- 混淆矩阵：TN=1，FP=177，FN=1，TP=339

校准指标只是 raw score 诊断，不激活任何校准器。Meso4 分数仍是未经校准的 fake-class softmax，不是真实性概率。

## failure_cases.csv

`failure_cases.csv` 按类别保留样本级错误：

- false_positive：177
- false_negative：1
- unable_to_determine：0
- system_failure：0

每行包含 experiment、axis、sample_id、label、predicted_label、risk_score、threshold、status、quality_status、failure_reason、runtime_seconds 和 relative_path。

## 不适用指标说明

- 音频：当前获授权 Celeb-DF v2 官方 518 视频没有音频流；没有授权冻结带标签音频伪造 manifest，因此不报告音频 F1/AUC/EER/校准/延迟性能。
- 事实核查：没有带真实 ASR/OCR/检索来源和标签的冻结事实核查 manifest；现有内容轴夹具是 Mock 流水线测试，不是事实核查性能数据。
- 融合：没有非 Mock 多轴冻结标签 manifest；Mock 内容/来源轴禁止形成冲突或融合信号。
- 消融：至少需要两个真实非 Mock 轴的冻结标签 manifest；当前只有视觉 raw predictions 可用。
- 鲁棒性：没有授权冻结扰动 manifest；阶段 8 未生成扰动样本，也未删除困难样本。

## 验收与限制

- 同一命令可重算阶段 8 表格、图和 failure cases。
- 原始 predictions 与失败样本保留在本地 ignored 输出目录。
- 指标表和图由 raw JSONL 自动生成，没有手工填写。
- 阶段 8 到此交接；未开始部署。

