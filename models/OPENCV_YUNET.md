# OpenCV YuNet 人脸检测工件

- 文件名：`face_detection_yunet_2023mar.onnx`
- 官方项目页：<https://github.com/opencv/opencv_zoo/blob/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx>
- 运行路径：`MINGJIAN_YUNET_MODEL`，默认 `models/cache/opencv-yunet/face_detection_yunet_2023mar.onnx`
- 下载策略：仅人工 provision；应用和容器运行时禁止自动下载。
- 输出：人脸框、5 个关键点，以及仅按相邻采样帧 IoU 连接的几何 `track_id`。

当前仓库不包含 ONNX 文件，也没有记录已核验 SHA-256。启用前必须由使用者确认 OpenCV Zoo 对该模型文件适用的许可条款、记录实际文件 SHA-256，并完成单模块 CPU smoke。完成这些步骤前 `ENABLE_FACE` 必须保持 `false`，不得把 `tracks=[]` 解释为无人脸事实或低风险证据。

