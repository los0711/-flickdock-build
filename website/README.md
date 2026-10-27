# FlickDock 官网

独立的 Cloudflare Workers Static Assets 站点。Worker 名称为 `flickdock-site`，不会覆盖处理账号和云同步的 `flickdock-cloud` Worker。站点不调用云同步 API，不在 Worker 上存放安装包；三个下载按钮链接到公开发行仓库。

## 本地检查

```powershell
cd website
npm ci
npm run check
npm run dev
```

本地预览后访问 Wrangler 输出的地址。交互演示只使用页面内置的示例文件，不读取访问者的本地文件。

## 部署

```powershell
cd website
npm ci
npm run deploy
```

默认地址由 Cloudflare 返回，通常为 `https://flickdock-site.<subdomain>.workers.dev/`。若要绑定正式域名，请在 Cloudflare 控制台为这个 Worker 单独设置自定义域名，不要将云同步 Worker 的路由改到官网。发布新客户端时，先核对公开安装包的实际 URL，再更新 `public/index.html` 中的三个链接和版本号。

当前链接：Windows 1.8.8 Setup、macOS 1.8.8 Apple 芯片 DMG、macOS 1.8.8 Intel DMG。Mac 安装包未签名或公证，页面如实提示。
