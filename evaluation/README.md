# Evaluation

## Phase 8 reproducibility suite

`run_phase8.py` 从冻结 suite manifest 编排视觉、音频、事实核查、融合、消融和鲁棒性实验。没有授权标签集的实验必须显式写成 `not_applicable` 和原因；指标、表格、ROC 图和 `failure_cases.csv` 只从保留的 raw prediction JSONL 自动生成。

```powershell
.\.venv\Scripts\python.exe evaluation\run_phase8.py --suite-manifest evaluation\phase8_suite.celebdf_v2_20260715.json --output-dir evaluation\phase8-runs\phase8-celeb-df-v2-20260715-final
```

本次阶段 8 真实重算见 `docs/PHASE8_EVALUATION.md`。输出目录被 Git 忽略；不要提交 raw predictions 或派生评测产物。

`run_visual_baseline.py` 从固定、带标签的帧 manifest 生成原始预测 JSONL；它不下载数据集。`compute_metrics.py` 只从该预测 JSONL 重算 accuracy、precision、recall、F1、ROC-AUC 和混淆矩阵。

manifest 为 JSONL，每行至少具有：

```json
{"sample_id":"sample-001","label":0,"frames":[{"path":"frames/sample-001.png","timestamp_seconds":0.0,"timestamp_quality":"exact"}],"sha256":"optional-input-sha256"}
```

`label=0` 表示 real，`label=1` 表示 fake。帧路径相对 `--dataset-root`；测试集必须经过授权、固定且不含用户上传媒体。`evaluation/datasets/`、`evaluation/work/`、预测与指标输出均被 Git 忽略。

```powershell
.\.venv\Scripts\python.exe evaluation\run_visual_baseline.py `
  --manifest evaluation\datasets\frozen-manifest.jsonl `
  --dataset-root evaluation\datasets `
  --work-dir evaluation\datasets\work `
  --output evaluation\meso4.predictions.jsonl --device auto
.\.venv\Scripts\python.exe evaluation\compute_metrics.py `
  --predictions evaluation\meso4.predictions.jsonl `
  --output evaluation\meso4.metrics.json
```

Celeb-DF v2 的固定视频流程使用 `build_celeb_df_v2_manifest.py` 和 `run_celeb_df_v2.py`。后者拒绝将 work、预测、配置或汇总写入原始数据根目录，并固定使用每 2 秒 1 帧、256×256、最多 150 帧/视频的规则。实际命令和结果记录于 `docs/PHASE3_EVALUATION.md`。

Celeb-DF v2 官方 518 视频评测已实际运行；产物和指标见 `docs/PHASE3_EVALUATION.md`。由于数据目录没有随附可复核的派生元数据发布条款，manifest、抽帧、预测、配置和指标只保存在 Git 外的 `<temporary-dir>\mingjian-celeb-df-v2-evaluation-20260714`。

