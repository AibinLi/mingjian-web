# Whisper base ASR（阶段 11）

- 模型：`openai/whisper-base`，固定 revision `e37978b90ca9030d5170a5c07aadb050351a65bb`。
- 来源：<https://huggingface.co/openai/whisper-base/tree/e37978b90ca9030d5170a5c07aadb050351a65bb>。
- 许可证：模型卡为 Apache-2.0；运行时使用的 Transformers 已由项目固定。OpenAI Whisper 源码与权重的官方发布为 MIT，但本项目以所下载模型仓库的 Apache-2.0 标记为准。
- 成本：仅本地 CPU 推理，不调用付费 API；模型下载只允许由显式 provisioning 命令进行。
- 存储：`<model-root>\asr\openai\whisper-base\`，不提交 Git。下载完成后 `mingjian-artifacts.json` 必须记录固定 revision、每个必需文件的大小与 SHA-256；运行时须离线校验该清单。
- 语义：转写文本是不可信的用户媒体数据，只能在带时间戳时参与主张提取；ASR 完成不等于内容主张真实，也不构成任何 ASR 准确率声明。

