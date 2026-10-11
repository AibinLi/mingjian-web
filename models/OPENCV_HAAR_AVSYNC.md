# OpenCV Haar 基础音画同步筛查

## 目的与边界

`HaarMotionAudioVisualSyncAdapter` 是阶段 4 的可替换本地同步筛查基线，不是 SyncNet、
唇读模型、活跃说话人识别器、深伪检测器或真实性概率模型。它以 OpenCV 的 Haar 单脸检测
定位人脸，在下半区计算相邻采样帧的运动量，并与标准化单声道 WAV 的短时能量在 ±0.5 秒
偏移内作 Pearson 相关性扫描。

输出的 `risk_score` 只是低相关性的未经校准启发式信号；`anomalous_intervals` 是能定位的
VAD 语音片段，供人工复核，不能表述为音画不一致的事实或伪造结论。

## 依赖、来源与许可

- PyPI 包：`opencv-python-headless==4.10.0.84`
- 来源：<https://pypi.org/project/opencv-python-headless/4.10.0.84/>
- Windows x64 wheel：`opencv_python_headless-4.10.0.84-cp37-abi3-win_amd64.whl`
- 发布大小：38,754,031 bytes
- SHA-256：`afcf28bd1209dd58810d33defb622b325d3cbe49dcd7a43a902982c33e5fad05`
- 许可：OpenCV Apache-2.0；`opencv-python` 包装脚本 MIT；wheel 随附 FFmpeg LGPL-2.1。
- 成本：仅本地 CPU 运行，不调用付费 API。

2026-07-14 已在用户明确许可后下载并校验该哈希。下载 wheel 位于
`<model-root>\sync\opencv-haar\`，运行副本位于其 `python\` 子目录；均不提交 Git。
`MINGJIAN_OPENCV_DIR` 可以覆盖运行副本位置。正式安装方式是从这个已校验 wheel 使用
`pip install --no-deps --target <model-root>\sync\opencv-haar\python <wheel>`；它不在
`backend/requirements.txt` 中，以避免常规虚拟环境安装与 D 盘冻结运行副本产生两套来源。

官方 SyncNet 仓库 <https://github.com/joonson/syncnet_python> 的代码许可证为 MIT，但其官方
下载脚本没有单独明确预训练权重许可证；因此未下载或采用其权重。

## 质量门控

只有满足全部条件时才返回 `completed` 与风险分数：

- 同时有音频和视频；
- 能量 VAD 给出至少 1.5 秒语音；
- 视频至少 320×180；
- 没有多脸候选帧；
- 单脸候选覆盖率至少 70%，且至少 12 个可比较运动样本。

无音频或无视频为 `not_applicable`；低清、短语音、无稳定单脸、多脸、遮挡、无法解码或
依赖缺失为 `unavailable`。这些状态和缺失风险分数都不表示低风险。

背景音乐、镜头运动、剪辑、遮挡、表情弱、VAD 误分段和说话人不在画面内都会影响可用性，
因此测试不得被解释为同步性能指标。

## 真实可用性验收状态

实验性适配器实现、异常区间契约、Mock、`not_applicable`/`unavailable` 和质量门控已完成。
2026-07-15 对获授权 Celeb-DF v2 官方 518 视频清单只读检查得到 `with_audio=0`，因而没有
可用于真实 `completed` 验收的已授权样本。该项验收明确未完成，不伪造完成状态；固定带标签
音频/同步性能评测推迟到阶段 8。

