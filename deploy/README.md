# 阶段 9 部署封装

本目录不单独维护 Compose 副本；根目录的 `Dockerfile` 和 `docker-compose.yml` 是唯一启动封装。阶段 9 已真实完成构建、gateway/API 双容器健康、宿主页/health、受控上传/删除和安全拒绝验收；最终 API 镜像 SHA-256 为 `6f4fd7169c3cd0a3b5e7af6f2bb9ba140f56ad0b6d014e624c8afec14596b6ee`。按根目录 `README.md` 的 `docker compose up --build -d` 启动并访问 `http://127.0.0.1:8000/health`。

该封装仅用于单机安全启动和验证：服务绑定回环地址、内部 Docker 网络禁止容器对外联网、URL 引用默认不抓取、模型不下载。它不宣称多租户、Internet 暴露、生产队列或模型性能能力。

