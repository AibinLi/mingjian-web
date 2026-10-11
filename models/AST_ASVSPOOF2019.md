# AST ASVspoof2019 音频反欺骗基线

- 模型：`MattyB95/AST-ASVspoof2019-Synthetic-Voice-Detection`。
- 来源：<https://huggingface.co/MattyB95/AST-ASVspoof2019-Synthetic-Voice-Detection>。
- 发布页许可：MIT（2026-07-14 只读核验）。
- 发布页规模：86.2M F32 参数；本地冻结工件如下：

| 文件 | 大小（bytes） | SHA-256 |
| --- | ---: | --- |
| `model.safetensors` | 344,790,040 | `df8c7cd04c6b81a4f02756cbef777662d0092cf33ab23b14eea0df74c7978db6` |
| `config.json` | 791 | `703a05b1c488954226ccd5fc107517de732eb1444c2700a64270786998725933` |
| `preprocessor_config.json` | 297 | `8d04ba5a9c6fca5d39d0de2b1fd05ecf79deb589fbba279728bbebac39934231` |
- 缓存：`<model-root>\audio\MattyB95\AST-ASVspoof2019-Synthetic-Voice-Detection\`；不写入仓库或 C 盘。可用 `MINGJIAN_AUDIO_MODEL_DIR` 覆盖。
- 成本：本地 CPU 推理，不调用付费 API。

## 当前可用性

2026-07-14 用户已将 `config.json`、`model.safetensors` 与
`preprocessor_config.json` 下载至 D 盘。已验证 `transformers==4.57.6`，本地
`config.json` 明确映射 `0=Bonafide`、`1=Spoof`；`Spoof` 是本基线的伪造类标签。
实际本地临时音频冒烟推理已返回 VAD 片段级和整段风险结果。

适配器在每次推理前逐个核验上述三个文件的存在、大小与 SHA-256；任何一项不匹配均返回
`unavailable` 且不加载模型。`from_pretrained(..., local_files_only=true)` 禁止远程下载；
模型与 processor 在同一服务适配器中惰性加载一次并缓存。版本字段故意写作“本地冻结工件+
三个 SHA-256”，因为本项目没有独立确认 Hugging Face 精确 commit，绝不把 `revision main`
伪装为冻结版本。该固定模型默认使用已核验的 `Spoof` 标签，也可用
`MINGJIAN_AUDIO_FAKE_LABEL` 覆盖；加载时始终与本地 `id2label` 校验。模型卡中的自报指标
不是本项目性能指标。

