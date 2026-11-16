# 集中发布

发布者使用有仓库写入权限的 GitHub 账号，在任意电脑打开
`los0711/-flickdock-build` 的 Actions → Central release → Run workflow，选择 `main`。
发布环境 `flickdock-release` 只允许 `main` 分支使用；不要把环境密钥放进代码、
工件、日志或聊天记录。只有可信的发布者才能拥有该仓库的写入权限：可以修改主分支
工作流的人同样可能使用发布环境密钥。

## 环境密钥

在仓库 Settings → Environments → `flickdock-release` 配置：

* `FLICKDOCK_RELEASE_SIGNING_KEY`：旧客户端已经信任的 Ed25519 原始私钥，Base64 编码。
* `GITEE_RELEASE_TOKEN`：有私有源码仓库和公开安装包归档仓库所需权限的专用令牌。
* `CLOUDFLARE_API_TOKEN`：受限于 FlickDock 账号和 `flickdock.cc.cd` 的 Workers/R2 发布令牌。

这些值由 GitHub 加密保存。不要在多台发布电脑分发签名私钥。轮换签名密钥时，
必须先确认已安装客户端的信任列表，否则旧版可能无法更新。

## 三种运行模式

* `signing-test`：检查密钥是否匹配现有客户端的发布者公钥，并核对私有 Gitee 源码
  的读取权限，不生成发布文件。
* `verify`：再检查 Gitee 登录、私有仓库、Cloudflare 令牌状态及 R2 桶的读取权限；
  成功不表示尚未实际验证的写入和部署权限已经具备。
* `publish`：填入简短的用户可见更新内容。该模式要求代码中的版本严格高于线上版本；
  已发布版本和标签不可覆盖。Windows 与 Apple 芯片 Mac 构建和各项验证完成后才继续。

发布前将经过审查的源码提交到 GitHub `main`，同时检查 `app_version.py`、
`FlickDock.iss` 和 `version_info.txt` 的版本一致，并保留安装脚本的 UTF-8 BOM。
`SOURCE_MANIFEST.json` 记录的是**上一次发布的基准**，在发布流程更新它之前不要手工改写。
如果私有 Gitee 源码与这个基准不一致，发布会停止，需要先检查并合并另一台电脑的改动。

发布步骤先验证并归档安装包，部署保留旧更新索引的下载网关，再验证公开下载、生成签名，
记录匹配的私有 Gitee 源码和 GitHub 发布元数据，最后更新官网与线上索引并读回校验。
最近三版保留 R2；更早版本只在对应公开归档下载可用并通过哈希校验后才移出 R2。
不同服务之间无法进行单一事务：失败时检查 Actions 中具体失败步骤，核对已存在的归档、
标签和索引，不要强推标签或覆盖同版本安装包。确认没有其他发布在运行后再处理。

此流程不发布 Intel Mac 包。macOS 签名和公证状态取决于实际构建凭据；不能把临时签名
误称为 Apple Developer ID 签名或 Apple 公证。
