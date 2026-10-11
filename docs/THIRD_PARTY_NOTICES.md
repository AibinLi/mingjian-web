# 第三方组件、模型、数据与许可清单

## OpenCV Zoo YuNet（未 provision）

- 代码中记录官方 OpenCV Zoo 项目页，仓库未包含 ONNX 文件且运行时禁止下载。
- 适用模型许可文本、实际文件 SHA-256 与 CPU smoke 尚未核验；完成前 `ENABLE_FACE=false`，不得作可用性或性能声明。

> 本清单用于参赛归档，不替代原始许可证文本或主办方的合规审核。外部模型、数据和 wheel 不随仓库提交；提交前须确认竞赛规则与各许可兼容。

| 项目 | 用途与版本 | 来源 | 许可/状态 | 归档处理 |
| --- | --- | --- | --- | --- |
| DeepfakeBench Meso4 | `v1.0.1`，`meso4_best.pth` | https://github.com/SCLBD/DeepfakeBench/releases/tag/v1.0.1 | CC BY-NC 4.0；需确认竞赛允许非商业条款 | 权重不入 Git；SHA-256 `a362ebbef2658daa551d808fbfccd57f0b5fd34fda46a05996e807cccddff4e0`。 |
| AST ASVspoof2019 Synthetic Voice Detection | `MattyB95/AST-ASVspoof2019-Synthetic-Voice-Detection` | https://huggingface.co/MattyB95/AST-ASVspoof2019-Synthetic-Voice-Detection | 发布页标注 MIT | 三个冻结文件在仓库外；本项目未报告其性能。 |
| OpenCV Haar / opencv-python-headless | `4.10.0.84` | https://pypi.org/project/opencv-python-headless/4.10.0.84/ | OpenCV Apache-2.0；包装脚本 MIT；随附 FFmpeg LGPL-2.1 | wheel 不入 Git；SHA-256 `afcf28bd1209dd58810d33defb622b325d3cbe49dcd7a43a902982c33e5fad05`。 |
| Celeb-DF v2 | 官方 518 测试名单 | 数据使用授权由参赛方持有 | 本仓库只记录“已获授权使用”；数据具体许可证/协议文本**未随仓库提供** | 原始视频、帧和派生物不提交；提交前附授权证明或按主办方要求说明。 |
| PyTorch | `2.5.1` | https://pytorch.org/ | BSD-3-Clause | Python 依赖，见 `backend/requirements.txt`。 |
| Pillow | `10.4.0` | https://python-pillow.github.io/ | HPND | Python 依赖。 |
| NumPy | `1.26.4` | https://numpy.org/ | BSD-3-Clause | Meso4 运行依赖。 |
| FFmpeg | Compose：Debian `7:5.1.9-0+deb12u1`；本地烟雾：Gyan.dev 构建 | https://ffmpeg.org/ | 按具体构建的许可证履行；本地 Gyan 构建启用 GPL | 本地构建下载归档 SHA-256 未在本会话独立复核，列为复现限制。 |
| Nginx unprivileged | `1.27.4-alpine` 固定 digest | https://hub.docker.com/r/nginxinc/nginx-unprivileged | 以镜像随附许可为准 | 固定 digest 写入 `docker-compose.yml`。 |
| FastAPI、Uvicorn、httpx、python-multipart、Transformers | 见 `backend/requirements.txt` | 各项目官方发布页 | 以各发行包随附许可为准 | 现场归档应导出环境的许可证清单，不将虚拟环境整体提交。 |

未采用 SyncNet 预训练权重：虽然代码仓库许可可见，但预训练权重许可未单独确认，因此未下载、未集成、未作性能宣称。

## 原创性材料待签清单

- [ ] 参赛队签字的原创性声明（项目自编代码、文档和配置的创作归属）。
- [ ] 成员、指导教师、学校和联系方式的官方报名表字段。
- [ ] Celeb-DF v2 使用授权/协议的可提交副本或主办方认可的授权说明。
- [ ] Meso4 CC BY-NC 4.0 与竞赛规则兼容性的确认。
- [ ] 如主办方要求，依赖许可证导出、源代码开源说明和第三方组件清单。

以上材料在当前仓库中不存在，不能伪造为“已签署”或“已提交”。

