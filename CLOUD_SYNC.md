# FlickDock 云同步 v1（1.8.0）

当前服务：https://flickdock-cloud.roger000416.workers.dev 。Workers + D1 + 私有 R2；源码保持私有。真实部署已完成注册、Windows/macOS 设备会话、8 MiB 文件上传/下载及完整 SHA256、设备撤销验证。macOS 设备会话是协议测试，不能代替真实 Mac 应用验证。按用户选择，本版不提供邮箱绑定，不发送邮件。

## 账号

用户界面只要求唯一账号与 10–128 字符密码，不要求邮箱/注册验证。服务端对用户名做 NFKC、trim、Unicode casefold，数据库 UNIQUE 防并发重名。客户端先 POST `/v1/auth/challenge` `{username,purpose:register|login}`，得到 UUID challenge_id、32 位 hex 随机盐、固定 `kdf={name:scrypt,N:16384,r:8,p:5,dkLen:32}` 和 5 分钟 expires_at。客户端后台运行 scrypt（32 MiB maxmem）；严格拒绝任意其他参数。POST `/v1/auth/register|login` 发送 `{username,challenge_id,auth_key:64hex,device_name,platform}`，不发送原密码。挑战绑定规范化账号和用途、单次消费。服务端用独立 AUTH_PEPPER 保存 HMAC-SHA256(accountId+salt+auth_key) 验证值。

这属于 TLS 上的客户端密码预哈希，auth_key 等价于密码，必须只在临时内存和 HTTPS 认证请求中出现，不能存盘或记录。服务端不存 auth_key 本身。AUTH_PEPPER 缺失时停止认证。并非 PAKE，不声称派生值泄露后仍不可重放。

成功返回 `{schema_version:1,token,expires_at,device_id,account}`。随机 256 位会话只在服务端存 SHA256，30 天到期。每账户最多 20 个有效设备，过期设备自动退休。客户端 token 放 Windows Credential Manager / macOS Keychain，普通 JSON 仅放账号元数据。每个请求重新校验会话/撤销；撤销后下一请求即 401。

GET `/v1/me` 返回 account/device_id/storage；GET `/v1/devices` 返回 devices（含 current、revoked_at）；DELETE `/v1/devices/{id}` 撤销本账号设备。POST `/v1/auth/logout` 退出当前设备。本版关闭邮箱绑定界面；预留协议：POST `/v1/email/code` `{email}` 成功为 202 `{challenge_id,expires_at}`；POST `/v1/email/bind` `{challenge_id,code}` 绑定。验证码有效 10 分钟、5 次尝试、HMAC 存储、单次使用；发信服务商接受后才显示已发送。未配置返回 503 EMAIL_UNAVAILABLE。没有邮箱找回密码或账号合并功能。

## 文件与协议

所有 JSON 都有 schema_version=1；Bearer 会话不跟随重定向。ID 为小写带连字符 UUID，name 1–255 字符，mime_type 不含 charset 参数。kind=file|text|url，文字/URL 保持原生文字，上传 UTF-8 字节，不能上传本机绝对路径。文件即使以原位置引用加入搁架，也会上传实际字节。图片不缩放、不压缩、不重新编码，预览缩略图不会替代上传内容。

1. POST `/v1/uploads` `{item_id,base_version,kind,name,mime_type,size,sha256}`，返回 upload_id/chunk_size/part_count/expires_at；首次版本 0。
2. 严格按顺序 PUT `/v1/uploads/{id}/parts/{index}`，原始 bytes 和 X-Content-SHA256。默认 1 MiB 分块；服务端核对每块并将完整 SHA256 的增量状态原子持久化。状态 codec 用固定版本 noble 实现，差分测试对比 Node 原生 SHA256，不自写 hash 轮函数。
3. POST `/v1/uploads/{id}/commit` `{}`。最终核对累计大小、完整摘要，以 D1 CAS 事务更新 item/changes；无需单次重新读取整个文件。相同块/commit 重试幂等，不同内容占同块号报冲突。DELETE uploads/id 取消时也使用状态 CAS，不能把已提交文件回收。
4. GET `/v1/sync/changes?cursor=0&limit=100`，返回 changes/cursor/has_more。只有整页应用完才保存账户自己的 cursor。
5. GET `/v1/items/{id}/content` 返回 `{record,chunk_size,part_count}`；逐个 GET `/v1/items/{id}/parts/{index}?version=N` 下载，每块带大小/摘要。客户端对所有块再次做完整 SHA256，原子落盘后展示。版本不匹配返回 409，不混合多个版本。每次分块重新认证，读 lease 防回收竞争。
6. DELETE `/v1/items/{id}` `{base_version}`，保存版本化 tombstone 并立即清理 R2 对象；离线设备回来仍能收到删除。已有读取租约或暂时存储失败时，后台任务重试，删除成功前仍计入占用。删除搁架内容不会删除原文件。
7. GET `/v1/uploads/{id}` 返回所属账号上传的 state、next_part、大小、SHA256、分块信息与到期时间。客户端保留账户内原始快照，重启/网络中断后按服务端进度续传，不从头重复。未提交上传在最后一次成功写入后 72 小时到期，与提交后的大文件保留规则分开。

record={id,version,deleted,kind,name,mime_type,size,sha256,updated_at,expires_at}。每账户独立 cursor、缓存、离线日志、视图；仅首次登录账户导入当前游客搁架，后续账号不会继承上个账号文件。退出恢复独立游客搁架。冲突保留两个版本，不盲目覆盖；中断上传保留账户内断点与原始快照；文件修改/移除时才撤销旧 reservation。已提交但丢失响应可识别，避免重复文件。服务端限流的 retry_after 会持久化，等待额度恢复。

文件夹后台压缩，跨设备安全解压；保留空目录，拒绝链接、路径越界、重复路径和超额解压。上传读句柄允许 Windows 删除/移动原文件。磁盘、KDF、压缩/校验都在后台；正常情况下本机变更立即排队，另一设备最多约一分钟拉取。错误显示在设置内并自动重试。

## 免费额度边界

取消额外的 64 MiB 单文件限制，全部用户共享 8 GiB（不是每用户独享 8 GiB）；pending/旧版本也计入占用，直到 R2 删除成功才释放。每月 R2 A/B 请求预算 800000/8000000，全服务每日 API 请求预算 5000，达到限制则停止相关操作，不改开付费计划。Cloudflare Free 的 D1/Workers 硬限制也可能先触发。额度仅约束本服务，不能保证此账号的其他业务也免费。每分钟清理任务回收取消/过期内容；大于 500 MiB 的已提交文件在提交后 24 小时到期，形成跨设备删除记录并移除云端字节，保留本机原文件。恰好 500 MiB 不属于自动过期范围。传输每日 5000 次请求，删除另有每日 10000 次保留预算，达到传输预算后仍可清理存储。R2 DELETE 免费，不消耗本服务的 A/B 预算。大文件同步时间仍受免费服务实际额度、网络及到期规则限制。

用户选择不绑定邮箱，EMAIL_PROVIDER=disabled，界面不显示绑定入口。R2 公共开发 URL 与自定义公开域名均关闭，只有认证 API 能取文件。

验证：cloud 中 npm ci / npm run typecheck / npm test；客户端 python test_cloud_client_v180.py 启动独立 workerd/D1/R2，测试实际 Qt 网络、后台密码派生、两设备、文件和文字、空文件、越权拒绝及撤销。所有自动 UI 测试 offscreen，不操作用户桌面。