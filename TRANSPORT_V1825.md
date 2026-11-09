# 当前手动互传协议（1.8.28）

手动互传恢复私有仓库 v1.8.17（103f386b807ac9c8a044c462ee1677711593d7a0）的文件协议。

- 局域网：`lan_transfer.py`，现有 TLS 直连；不自动切换远程。
- 远程：`wan_transfer.py` → `webrtc_stream.py` → `p2p/bridge.mjs`，原版 node-datachannel 0.33.6 / libdatachannel 0.24.6；直连不可用时走现有 TURN。远程不切换局域网，不再使用 Syncthing 或手动 R2 文件任务。
- 直接枚举文件元数据后发送，边读边校验，不生成整文件快照、不创建同步目录、不等待索引。
- 四位字母数字接收码、账号绑定、同账号免确认、不同账号接收确认和当前界面保留。账号/会话变更使旧绑定回调失效，未完成绑定的同账号请求延后呈现。
- 两端须一起更新；发现对方仍使用 Syncthing 时明确提示更新，不假装建立可用连接。
- 搁架、剪贴板保持 1.8.27 的云端纯通知调度，代码及云端接口没有改动。

验证：`python p2p_assets.py`；`npm ci --prefix cloud`；离屏执行 `python -m unittest test_original_transfer test_wan_transfer_v1816 test_wan_account_v1819 test_lan_transfer_v181 test_lan_account_v1825`。
`test_original_transfer` 启动真实本地 Workers 服务和两个客户端，验证四位码、接收确认、32 MiB 文件与空目录、原始内容 SHA-256 及本地 ICE 直连。跨平台 CI 继续从应用入口测两端实际保存与系统剪贴板写入。
