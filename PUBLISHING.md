# FlickDock 发布与在线更新

源码仓库保持私有。公开仓库 `rooogerluo/flick-dock-releases` 仅承载安装包附件、`updates/latest.json` 和公开说明。源码 ZIP、源码清单、构建验证报告和用户素材不能加入公开仓库。

## 发布顺序

1. 为新版本设置唯一版本号。`app_version.py` 是运行时版本来源，`flickdock.py` 从它导入；`FlickDock.iss` 和 `version_info.txt` 中的显式构建版本必须一致。
2. 在隔离配置和离屏 Qt 下运行相关回归，构建程序与安装包，并验证冻结程序。生成验证记录，其 `version` 对应本次版本，`checks` 非空且每项 `exit_code` 为 0，`frozen.ok`、`frozen.frozen` 为 true，`setup` 记录本次安装包的 `name`、`bytes`、`sha256`。
3. 将安装包上传到公开发行位置，取得实际、稳定、无需登录的 HTTPS 下载地址。下载地址来自已核实的发行页面或服务文档，不能猜测 Gitee API 或附件路径。
4. 运行下面的发布校验工具。它匿名完整下载远程安装包，核对 PE 文件头、大小及 SHA256，成功后才在最后写入 `updates/latest.json`。
5. 把已生成的更新索引作为公开仓库的最后一次发布提交。再匿名读取公开索引，确认它指向已可下载的正确安装包。源码提交或单独打 tag 均不等于完成在线更新发布。

安装包一旦发布不可替换。同版本更换字节、降低版本、缺少通过的构建验证文件、缺少源码白名单文件，工具都会拒绝。相同版本及完全相同内容可重复执行。

## 校验工具

在 Windows 发布机运行，无需启动安装包。工具只读取 PE 版本资源，不构建、不上传、不创建 tag、不运行安装程序，也不修改用户配置。

`$PublicInstallerUrl` 和 `$PublicManifestUrl` 必须填入已核实的真实地址；`$PublicRepo` 是仅用于公开发行索引的本地 checkout，`$PrivateArtifacts` 必须在该公开目录之外。

```powershell
python release_manifest.py `
  --root . `
  --installer ../outputs/FlickDock-Setup-1.7.0.exe `
  --version 1.7.0 `
  --notes-file ../outputs/1.7.0-notes.txt `
  --verification ../outputs/FlickDock-1.7.0-验证记录.json `
  --download-url $PublicInstallerUrl `
  --previous-url $PublicManifestUrl `
  --artifacts $PrivateArtifacts `
  --output "$PublicRepo/updates/latest.json"
```

首次建立更新索引时，用 `--initial-release` 代替 `--previous-url`。后续版本必须读取当前公开索引，或使用刚从公开位置取得的 `--previous-manifest` 文件；目标输出已存在时也会自动检查它。

HTTPS 重定向每一跳都检查协议和主机。默认只允许安装包及前一索引 URL 的主机；下载确实转到另一个已核实 CDN 时，追加 `--allow-host 实际主机名`。本仓库已匿名核实附件 CDN 为 `foruda.gitee.com`，raw 索引 CDN 为 `raw.giteeusercontent.com`，因此 Gitee 发布时分别追加这两个主机。不允许 HTTP 降级、登录页、URL 内嵌账号密码或访问令牌。写入索引的稳定下载地址不能带临时签名；已批准 CDN 的匿名重定向可带服务端临时签名，该地址不会写入公开索引。工具不使用登录 cookie 或 Authorization 请求头。

Gitee 附件匿名跳转使用名为 `token` 的服务端临时签名。此例外仅适用于内部 HTTPS 重定向到精确主机 `foruda.gitee.com`、路径以 `/attach_file/` 开头的地址；查询参数只能是 `token`、`ts`、可选 `attname`，其中 `token` 和 `ts` 必须非空且参数不得重复。其他主机、路径、额外凭证参数仍拒绝。稳定下载入口及 `latest.json` 内的安装包地址始终不能含 `token`，也不要复制临时 CDN 地址作为发布地址。

Gitee 的附件 CDN 会在匿名重定向中生成名为 `token` 的临时下载签名。工具仅对已批准的 `foruda.gitee.com/attach_file/` 内部 HTTPS 重定向接受该形式，且必须同时有非空 `token`、`ts`，只允许 `token`、`ts`、`attname` 参数。稳定入口、公开索引、其他主机和访问令牌参数仍会拒绝；临时 CDN 地址不会写入公开索引。

成功时，私有目录中生成：

- `FlickDock-版本-source.zip`：显式白名单源码、构建文件、维护测试及其依赖基准入口、发布检查脚本、说明、字体许可和图标。
- `FlickDock-版本-source-manifest.json`：每个入包文件的相对路径、大小、SHA256；ZIP 内也有同一份清单。
- `FlickDock-版本-SHA256SUMS.txt`：安装包与私有源码 ZIP 的校验值。
- `FlickDock-版本-release-record.json`：构建验证记录的哈希和本次匿名下载核验结果。

这些文件保留本地或上传私有源码仓库。公开 `latest.json` 不包含源码地址或源码元数据。源码 ZIP 使用固定时间戳和明确清单，可重复生成；不遍历整个工作区，不收录 `py/`、`dist/`、截图、录屏、下载媒体、用户设置、`.env`、Git 凭证或本地诊断 JSON。

新增本地运行模块、维护测试或测试依赖时，须在 `release_manifest.py` 的显式白名单中登记。选定 Python 文件导入了未登记的本地模块时会拒绝打包，避免遗漏实现或检查文件。字体还会核对 `font_manifest.json` 中的大小和哈希。

## 更新索引协议

稳定发布仅使用无前导零的 `X.Y.Z`，按整数元组比较，`1.10.0` 大于 `1.9.9`。时间必须为 UTC。SHA256 使用 64 位小写十六进制，`size` 为实际文件字节数。

```json
{
  "schema_version": 1,
  "version": "1.7.0",
  "published_at": "2026-10-23T00:00:00Z",
  "notes": "本次更新说明",
  "installer": {
    "filename": "FlickDock-Setup-1.7.0.exe",
    "url": "填写已核实的公开 HTTPS 安装包地址",
    "sha256": "填写实际安装包的64位小写SHA256",
    "size": 123456
  }
}
```

示例中的 URL 和哈希只是字段说明，不能直接用于发布。客户端会检查版本、URL、大小和哈希；安装包验证成功也不代表已经安装，客户端仍需用户主动选择安装。

## 建议合入 AGENTS.md 的持续授权规则

以下是待仓库负责人定案的规则建议，本文件本身不修改根 AGENTS.md：

> 本项目约定“上传源码”“发布新版”“同步发布”表示完成一次完整发布：检查变更与当前远程版本，必要时提升版本，运行相关回归和冻结程序检查，构建并校验安装包，按显式清单提交/推送到私有源码仓库，创建不可变版本 tag，上传公开安装包并匿名验证，最后发布公开更新索引并回读核实。用户已授权该发布流程时，持续执行到可安装更新实际可用，不因完成源码 push 或 tag 就结束，也不重复要求相同范围的授权。任何步骤失败均停止索引晋升，保留旧可用版本并报告实际阻碍。
>
> 源码仓库始终私有；公开发行仓只提交经过选择的 `updates/latest.json` 和 README，安装包通过发行附件上传。禁止 `git add .` 将源码 ZIP、源码、凭证、用户数据、媒体、构建目录或本地报告推送至公开仓库。凭证只从已有授权会话、凭证管理器或环境取得，不写入源码、清单、命令日志或公开链接。相同版本不得覆盖安装包，不移动已发布 tag，不伪造远程安装包已可用。
