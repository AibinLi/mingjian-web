# 阶段 9 威胁建模与安全边界（2026-07-17）

本阶段目标是让明鉴 Web 在干净环境中以最小权限安全启动，并验证高风险输入边界；不新增检测模型、阈值、校准器、真实检索或性能声明。

| 资产 / 边界 | 威胁 | 防护 | 验证 |
| --- | --- | --- | --- |
| 本地主机与浏览器写请求 | Host 伪造、跨站表单/CSRF、远程访问、请求洪泛 | TrustedHost 只允许 loopback/localhost（测试显式允许 testserver）；状态修改必须同源 Origin/Referer 或本地 CLI 无头请求；仅容器模式精确允许默认路由/显式 Docker 网关 IP；速率、活跃任务和上传并发受限 | 恶意 Host、恶意 Origin multipart、无 Origin CLI、同源前端和模拟 Docker 网关测试 |
| 上传入口与本地磁盘 | 伪装容器、压缩/派生物膨胀、路径穿越、磁盘滥用 | Content-Length 快速拒绝加 ASGI 原始体计数（含分块/无长度），再进行 10 MiB 文件流式上限、首 4 KiB 魔数 + MIME/扩展名 + FFprobe 容器复核；对象键只由 SHA-256 服务端生成 | 超限、无 Content-Length、并发上传、魔数和对象键回归 |
| FFmpeg/FFprobe | shell 注入、网络协议取数、失控子进程、耗尽资源、取消后残留 | 无 shell、仅命令白名单、`file,pipe` 协议白名单、网络协议前缀拒绝、空环境、stdin 关闭、stderr 截断；Windows 直接进程组启动，POSIX exec-only helper 设置 CPU/AS/文件/FD rlimit，不设置 NPROC；Compose `pids_limit` 处理容器级 PID/线程硬界，所有内部命令显式限制 codec/filter 线程为 2；每命令和整任务超时、取消杀死进程组 | 网络协议拒绝、Windows/Posix 分支、无 NPROC 单元测试、线程参数和真实媒体回归 |
| URL 引用 | SSRF、DNS rebinding、回环/内网/链路本地/云元数据 | 默认不联网；只接受 IDNA/大小写/IPv6/末尾点规范化的无凭据 HTTP(S) 默认端口引用，拒绝内网、回环、链路本地、保留/共享地址、`.local` 和 metadata 主机名；没有 URL 下载器 | 私网、metadata、尾点、IDNA、IPv6 与整数回环拒绝测试 |
| ASR/OCR/网页文本 | 提示注入、工具调用诱导、伪造引用 | 所有文本以有界数据保存；不存在 LLM 提示入口；检索仅为可替换适配器，默认 `FakeSearchAdapter` 不联网；引用必须是结构化 HTTP(S) URL、域名一致、时间戳和正文逐字片段，Mock 明示 | 既有 `test_claim_verification.py` 的注入与引用契约测试；健康接口公开 `data_only_tool_allowlist` |
| 用户媒体与诊断 | 长期保存、密钥/令牌泄漏到日志或响应 | 默认 15 分钟留存（范围 60 秒至 24 小时）；启动和每 30 秒清理已终态任务、源对象与派生物；删除接口同样清理；统一日志脱敏过滤器；`.env`/数据/模型缓存被忽略 | 留存主动删除、令牌与 URL 凭据脱敏测试 |
| 容器/入口网络 | Docker Desktop internal 网络端口不可达、入口外网化、伪造 Host/客户端 IP | API 无 ports、仅连接固定 `internal:true` 子网；固定 digest 的非 root Nginx gateway 是唯一 `127.0.0.1:8000` 入口，连接 bridge ingress 与 internal；网关保留 Host、清空 X-Forwarded-*，API 仅信任固定 gateway IP `172.30.0.2`；两者均只读、无 capabilities、资源受限 | 已真实验证双容器 healthy、宿主页/health、受控上传/删除、安全头和 Host/Origin/SSRF/超限拒绝；API 外部网络访问失败 |

残余风险：该 localhost 单用户 Compose 封装不是多租户生产平台；SQLite 和本地卷不提供租户隔离或高可用。URL 任务仍是离线引用，真实 URL 下载尚未实现；如未来需要，必须先取得业务/许可/留存授权并实现可验证的已解析 IP 绑定与真实对端校验。Docker 镜像不携带 Meso4/AST 权重；模型缺失会如实降级为 `unavailable`，不会变为低风险。

