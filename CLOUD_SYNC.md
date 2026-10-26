# FlickDock 云同步 v1（1.8.3）

当前服务：https://flickdock-cloud.roger000416.workers.dev 。Workers + D1 + 私有 R2；源码保持私有。真实部署已完成注册、Windows/macOS 设备会话、8 MiB 文件上传/下载及完整 SHA256、设备撤销验证。macOS 设备会话是协议测试，不能代替真实 Mac 应用验证。按用户选择，本版不提供邮箱绑定，不发送邮件。

## 账号

用户界面只要求唯一账号与 10–128 字符密码，不要求邮箱/注册验证。服务端对用户名做 NFKC、trim、Unicode casefold，数据库 UNIQUE 防并发重名。客户端先 POST `/v1/auth/challenge` `{username,purpose:register|login}`，得到 UUID challenge_id、32 位 hex 随机盐、固定 `kdf={name:scrypt,N:16384,r:8,p:5,dkLen:32}` 和 5 分钟 expires_at。客户端后台运行 scrypt（32 MiB maxmem）；严格拒绝任意其他参数。POST `/v1/auth/register|login` 发送 `{username,challenge_id,auth_key:64hex,device_name,platform}`，不发送原密码。挑战绑定规范化账号和用途、单次消费。服务端用独立 AUTH_PEPPER 保存 HMAC-SHA256(accountId+salt+auth_key) 验证值。

这属于 TLS 上的客户端密码预哈希，auth_key 等价于密码，必须只在临时内存和 HTTPS 认证请求中出现，不能存盘或记录。服务端不存 auth_key 本身。AUTH_PEPPER 缺失时停止认证。并非 PAKE，不声称派生值泄露后仍不可重放。

成功返回 `{schema_version:1,token,expires_at,device_id,account}`。随机 256 位会话只在服务端存 SHA256，30 天到期。每账户最多 20 个有效设备，过期设备自动退休。客户端 token 放 Windows Credential Manager / macOS Keychain，普通 JSON 仅放账号元数据。每个请求重新校验会话/撤销；撤销后下一请求即 401。

GET `/v1/me` 返回 account/device_id/storage；GET `/v1/devices` 返回 devices（含 current、revoked_at）；DELETE `/v1/devices/{id}` 撤销本账号设备。POST `/v1/auth/logout` 退出当前设备。本版关闭邮箱绑定界面；预留协议：POST `/v1/email/code` `{email}` 成功为 202 `{challenge_id,expires_at}`；POST `/v1/email/bind` `{challenge_id,code}` 绑定。验证码有效 10 分钟、5 次尝试、HMAC 存储、单次使用；发信服务商接受后才显示已发送。未配置返回 503 EMAIL_UNAVAILABLE。没有邮箱找回密码或账号合并功能。

## 文件与协议

所有 JSON 都有 schema_version=1；Bearer 会话不跟随重定向。ID 为小写带连字符 UUID，name 1–255 字符，mime_type 不含 charset 参数。kind=file|text|url，文字/URL 保持原生文字，上传 UTF-8 字节，不能上传本机绝对路径。文件即使以原位置引用加入搁架，也会上传实际字节。图片不缩放、不压缩、不重新编码，预览缩略图不会替代上传内容。

1. POST `/v1/uploads` `{upload_id,item_id,base_version,kind,name,mime_type,size,sha256}`。若同账号已有仍在使用、摘要与大小一致的原文件，返回 `{instant:true,record}`，无新 R2 写入；否则返回 upload_id/chunk_size/part_count/expires_at。upload_id 在请求前持久化，可安全重试；首次版本 0。
2. 普通上传严格按顺序 PUT `/v1/uploads/{id}/parts/{index}`，原始 bytes 和 X-Content-SHA256。4 MiB 以内单段上传，较大文件按 1 MiB 分段（兼容旧断点）；R2 原生 SHA-256 校验每段。多段文件的完整 SHA256 增量状态原子持久化，状态 codec 用固定版本 hash-wasm 4.12.0 的静态 WASM 实现。
3. POST `/v1/uploads/{id}/commit` `{}`。最终核对累计大小、完整摘要，以 D1 CAS 事务更新 item/changes；无需单次重新读取整个文件。相同块/commit 重试幂等，不同内容占同块号报冲突。DELETE uploads/id 取消时也使用状态 CAS，不能把已提交文件回收。
4. GET `/v1/sync/changes?cursor=0&limit=100`，返回 changes/cursor/has_more。只有整页应用完才保存账户自己的 cursor。
5. GET `/v1/items/{id}/content` 返回 `{record,chunk_size,part_count}`；逐个 GET `/v1/items/{id}/parts/{index}?version=N` 下载，每块带大小/摘要。客户端对所有块再次做完整 SHA256，原子落盘后展示。版本不匹配返回 409，不混合多个版本。每次分块重新认证，读 lease 防回收竞争。
6. DELETE `/v1/items/{id}` `{base_version}`，保存版本化 tombstone；同账号多个条目复用同一份云端内容时，最后一个条目删除才清理 R2 对象。离线设备回来仍能收到删除。已有读取租约或暂时存储失败时，后台任务重试，删除成功前仍计入占用。删除搁架内容不会删除原文件。
7. GET `/v1/uploads/{id}` 返回所属账号上传的 state、next_part、大小、SHA256、分块信息与到期时间。客户端保留账户内原始快照，重启/网络中断后按服务端进度续传，不从头重复。未提交上传在最后一次成功写入后 72 小时到期，与提交后的大文件保留规则分开。

record={id,version,deleted,kind,name,mime_type,size,sha256,updated_at,expires_at}。每账户独立 cursor、缓存、离线日志、视图；仅首次登录账户导入当前游客搁架，后续账号不会继承上个账号文件。退出恢复独立游客搁架。冲突保留两个版本，不盲目覆盖；中断上传保留账户内断点与原始快照；文件修改/移除时才撤销旧 reservation。已提交但丢失响应可识别，避免重复文件。服务端限流的 retry_after 会持久化，等待额度恢复。

文件夹后台压缩，跨设备安全解压；保留空目录，拒绝链接、路径越界、重复路径和超额解压。上传读句柄允许 Windows 删除/移动原文件。磁盘、KDF、压缩/校验都在后台；正常情况下本机变更立即排队，账号专属 WebSocket 提示另一设备立即刷新上传占位、提交内容与设备列表。WebSocket 中断时保持最长 60 秒的定期拉取兜底。错误显示在设置内并自动重试。

## 免费额度边界

取消额外的 64 MiB 单文件限制，全部用户共享 8 GiB（不是每用户独享 8 GiB）；pending/旧版本也计入占用，直到 R2 删除成功才释放。每月 R2 A/B 请求预算按免费额度设置为 1000000/10000000，不再另设应用每日请求上限。Cloudflare Free 的 D1/Workers 硬限制仍可能触发；客户端遇到明确的额度耗尽响应，会暂停云请求至重置，本地搁架不受影响。额度仅约束本服务，不能保证此账号的其他业务也免费。每 15 分钟清理任务回收取消/过期内容；大于 500 MiB 的已提交文件在提交后 24 小时到期，形成跨设备删除记录并移除云端字节，保留本机原文件。恰好 500 MiB 不属于自动过期范围。R2 DELETE 免费，不消耗本服务的 A/B 预算。大文件同步时间仍受免费服务实际额度、网络及到期规则限制，特别大的文件不保证能在到期前同步到全部设备。

用户选择不绑定邮箱，EMAIL_PROVIDER=disabled，界面不显示绑定入口。R2 公共开发 URL 与自定义公开域名均关闭，只有认证 API 能取文件。

验证：cloud 中 npm ci / npm run typecheck / npm test；客户端 python test_cloud_client_v180.py 启动独立 workerd/D1/R2，测试实际 Qt 网络、后台密码派生、两设备、文件和文字、空文件、越权拒绝及撤销。所有自动 UI 测试 offscreen，不操作用户桌面。
# 1.8.1 传输与账号更新

`GET /v1/sync/changes` 附带 `transfers` 上传中元数据，另有 `GET /v1/transfers`。未提交的对象不能下载；其他设备以格式图标和进度占位，下载完整原文件并通过 SHA256 后替换。手动取消保留账号隔离的 7 天终止记录，阻止在途请求及发送端自动重建上传；未完成的 R2 数据在写入租约结束后清理。自然过期的断点仍可重建。

设置支持设备改名和修改密码（原密码、新密码、确认新密码）。客户端后台执行固定 scrypt 参数，密码不直接发送，服务端以一次性挑战和条件事务验证并撤销其他设备会话。当前设备保持登录。不绑定邮箱时不提供无需原密码的密码找回。

实际私有桶 `flickdock-private` 已启用 R2 Local Uploads；仍为标准存储、私有访问和原免费保护配置。此开关没有启用费，普通上传请求仍计入 Class A。参见 https://developers.cloudflare.com/r2/buckets/local-uploads/ 。配额统计仅覆盖此服务管理的分配与请求，不是整个 Cloudflare 账号的账单硬上限。

局域网互传走设备间 TLS 连接，不经过 R2。双方核对 TLS 会话绑定的确认码后接收原文件；支持当前会话内断点续传，取消清理未完成接收。云端严格整体 SHA 状态使同一文件分块仍按序上传；下载以最多四个分块并发，所有文件最终核对原始 SHA256。

