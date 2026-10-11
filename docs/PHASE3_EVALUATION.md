# 阶段 3：Celeb-DF v2 正式评测

阶段 3 已完成；本文件只记录阶段 3 评测。阶段 4 已在后续获授权对话中完成，用户确认已通过官方申请取得 Celeb-DF v2 使用授权。

## 数据与证据

- 官方名单：`List_of_testing_videos.txt`，518 条唯一文件，缺失数 0。
- real 178、fake 340；官方名单 SHA-256：`07063d46206e011aef9d1ad8d1854f5b87ae519ba6e452b897cc528c3dcfcdc0`。
- manifest SHA-256：`20bb4a4a17e95b83f6b95ad162174a5d8f0a5ca5b83d735706ab0ccd1d75ca77`。
- Meso4 权重 SHA-256：`a362ebbef2658daa551d808fbfccd57f0b5fd34fda46a05996e807cccddff4e0`。
- 预测、manifest、指标、配置、汇总和抽取帧稳定保存于 `<evaluation-artifact-root>\phase3-celeb-df-v2-20260714`，不提交 Git。
- predictions SHA-256：`4e7e87b508d7e1a27d8ff56afe8f84b5685a703e9aaf60a9a61fdd3a36adb8eb`；metrics SHA-256：`dcce5221d0549467c5236a5d6967ab2123528f65df65c4f840ee30808e0526f2`。
- config SHA-256：`1856c1845a9ae71d2267a8d56e257bd8291bac8c6111f3bbd42fcc15c323929a`；summary SHA-256：`7eb38cb831e4d57d9ca107b2c10d533237595898f64457acf2282246c23afaa3`。复制前后五个文件哈希均已匹配。

## 固定配置

FFmpeg 单线程、`fps=1/2`、`scale=256:256:flags=bilinear`、JPEG quality 2、最多 150 帧/视频；Meso4 RGB 256×256、mean/std `[0.5,0.5,0.5]`、批量 16、CPU、每视频模型超时 30 秒。视频风险为最高帧 fake-class softmax 分数。

## 实际结果

- 518/518 完成；跳过 0、解码失败 0、模型不可用 0；预测运行 103.61 秒。
- Accuracy：0.656371；Precision：0.656977；Recall：0.997059；F1：0.792056；ROC-AUC：0.513648。
- 混淆矩阵：TN=1、FP=177、FN=1、TP=339。

指标仅从逐视频预测 JSONL 重算，未修改预测或指标。

## 如实解释

模型几乎全部预测为 fake；Accuracy 接近“始终预测 fake”的类别基准，ROC-AUC 接近随机。Meso4 分数未经校准，不能当作真实性概率，也不得将该模型宣传为可靠的通用深伪检测器。

数据目录未随附可复核的再分发条款，因此即使产物不含媒体，也不提交 Git。阶段 4 已在后续获授权对话中完成冻结与降级交接；这不改变本文件的阶段 3 评测结果。

