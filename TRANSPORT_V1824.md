# 同账号直连与传输库评估（2026-11-04）

当前继续使用已有的原生 libdatachannel / node-datachannel、Qt WebSocket 和 OpenSSL TLS。每台设备启动一个共享原生进程，为其余在线同账号设备建立独立 WebRTC 连接。经私有 ICE 地址、一次性 nonce 和证书指纹验证后优先使用局域网 TLS，否则使用 WebRTC；直连不可用时使用云端下载和离线补齐。直连送达与云端备份分别显示真实状态。

服务端校验两端账号、设备、会话、授权 nonce、连接标识后转发 SDP / ICE，文件字节不经过信令服务。连接定期续租，账号切换、撤销、退出或链路失效会停止任务。对端只能请求已登记的摘要和合法字节范围，不能指定本机路径。完整 SHA256 校验后原子发布。搁架提前送达与云端确认去重，冲突保留实际内容，移出搁架保留外部原文件。

## 现成库

| 库 | 能力与集成条件 | 结论 |
|---|---|---|
| [libdatachannel / node-datachannel](https://github.com/murat-dogan/node-datachannel) | 已有依赖，原生 WebRTC、ICE、STUN、TURN；Windows 和两种 Mac 架构预编译包；MPL-2.0 | 本次复用并优化，未新增传输依赖。 |
| [Iroh](https://github.com/n0-computer/iroh-ffi) | Rust QUIC、打洞、中继、连接迁移；官方 Python 绑定；MIT / Apache-2.0。官方 Python README 列出 Windows x64、Apple Silicon wheel，未列 Intel Mac | 值得独立对比；尚需验证 Intel Mac 构建、冻结打包及自有发现、中继、账号授权接入。本次未集成或测速。 |
| [aioquic](https://aioquic.readthedocs.io/en/latest/) | Python QUIC，Sans-IO / asyncio 接口；BSD-3-Clause | 还需接入 Qt 事件循环、账号、发现、打洞及中继，没有证据证明替换原生库能提速。 |
| [MsQuic](https://github.com/microsoft/msquic/blob/main/docs/Platforms.md) | 微软 QUIC，默认 Windows Schannel 依赖较新的系统 TLS；Windows 10 可单独构建 OpenSSL 版本；MIT | 本项目 Python、Windows 10/11 和两种 Mac 架构的集成构建成本较高。 |

## 已实施优化

采用 [Syncthing BEP 的请求/响应流水线模式](https://docs.syncthing.net/specs/bep-v1.html)，每次最多四个 1 MiB 请求在途。每条连接的请求数、队列字节和后台任务有界，剪贴板使用高优先级并保留请求机会。复用库实现的加密、拥塞控制和打洞。

OpenSSL 对同一 TLS 对象的操作需要串行化，见 [官方线程约束](https://docs.openssl.org/master/man7/openssl-threads/)。本次将 TLS 调用设为非阻塞，在锁外等待 IO，解决全双工读写偶发断连。

## 实测范围

`bench_peer_throughput_v1824.py <输出 JSON>` 使用真实 WebRTC 或指纹验证的 TLS、16 MiB 随机原始文件，分别注入 0 / 40 ms 每块处理延迟。每种场景重复三次并校验字节。首轮中位吞吐：TLS 15.69 → 22.40 MiB/s，WebRTC 8.94 → 16.15 MiB/s；注入处理延迟时分别为 9.70 → 20.65、6.63 → 13.39 MiB/s。修复 TLS 并发操作后另执行三轮完整场景。

这些是同一 Windows 主机的对比；处理延迟不等于物理公网 RTT，不能当作真实两台设备的局域网或公网速度。未实测 Iroh、aioquic 或 MsQuic 的速度。

`cloud/test/peer-runtime.mjs` 启动隔离的真实 Worker/D1/R2，并通过 `FD_QA_PYTHON` 或 `FD_QA_EXE` 启动三个生产 Qt 客户端，验证同账号全连接、云端字节上传暂停时先收到原始文件和剪贴板、随后备份确认，以及退出账号撤销连接。使用隔离剪贴板，不接触系统剪贴板。
