# DeepfakeBench Meso4 记录

- 名称：DeepfakeBench Meso4
- 版本：DeepfakeBench release `v1.0.1`，文件 `meso4_best.pth`
- 来源：https://github.com/SCLBD/DeepfakeBench/releases/tag/v1.0.1
- 许可证：CC BY-NC 4.0。使用前须确认竞赛规则允许非商业许可证；本项目不将其描述为商业许可。
- 下载日期：2026-07-14，经用户明确授权。
- 本地缓存：`models/cache/deepfakebench/meso4_best.pth`（Git 忽略）
- 发布资产大小：117,943 bytes；本地实际大小：117,943 bytes。
- 本地 SHA-256：`a362ebbef2658daa551d808fbfccd57f0b5fd34fda46a05996e807cccddff4e0`
- 推理配置：RGB 256×256；归一化 mean/std 均为 `[0.5, 0.5, 0.5]`；默认批量 16；默认超时 30 秒。
- 硬件：本机有 RTX 4070 Laptop GPU（8 GiB），但本次实际安装的是 PyTorch `2.5.1+cpu`，故已验证推理使用 CPU。`auto` 会如实选择当前 PyTorch 可用的设备；显式请求不可用的 CUDA 会返回 `unavailable`，不会伪造 GPU 推理。

运行时依赖：PyTorch 2.5.1（BSD-3-Clause）、Pillow 10.4.0（HPND）和 NumPy 1.26.4（BSD-3-Clause）。它们不包含额外检测权重。

限制：Meso4 是单帧视觉分类基线。项目中的“轨迹”明确为采样视觉帧序列聚合，而不是人脸身份跟踪；分数未经本项目数据校准，不能证明媒体真实或虚假。

