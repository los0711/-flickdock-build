# FlickDock 官网

正式域名为 **https://flickdock.cc.cd**，由 Cloudflare Worker `flickdock-site` 的自定义域名提供服务。`wrangler.jsonc` 固定账号和域名，保留 `WAN_SERVICE` 到 `flickdock-cloud` 的服务绑定。网页演示只使用内置示例；客户端的 `/v1/*` 请求由现有网关转发。

## 检查和发布

```powershell
cd E:\Codex\Projects\2026-11-04\ba-x20\work\flick-dock-master\website
npm ci
npm run check
npm run dev
```

发布前必须确认公开安装包可匿名下载且 SHA-256 与构建产物一致，将发行工具生成并签名的七份更新索引放入 `public/updates/`，再运行 `npm run deploy`。不要手写签名、覆盖已发布版本的安装包，或将官网部署到 `flickdock-cloud`。部署后同时检查正式域名和现有 `workers.dev` 更新地址，验证客户端仍可获取签名索引。

底部保留三个下载入口：Windows x64 1.8.23 Setup、Mac Apple 芯片 1.8.23 PKG、Mac Intel 1.8.22 PKG（已停止更新）。顶部原有下载控件复用这些链接。Mac 包未经过 Apple Developer ID 签名或公证。Intel 更新索引保持 1.8.22，不再构建新版本。

## 三文件静态导出

页面的 SVG 图标已内置于 JS，通过 `blob:` URL 加载。可导出原有静态页面用于独立托管：

```powershell
npm run build:retiehe -- E:\Codex\Temp\flickdock-site-export
```

输出目录必须位于源码目录之外且为空。生成的 `index.html`、`styles.css`、`site.js` 不包含客户端网关或更新索引；此导出不替代正式 Cloudflare 部署。
