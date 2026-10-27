# 官网中国大陆访问方案

FlickDock 官网是纯静态站点：`public/index.html`、`public/styles.css`、`public/site.js` 和 `public/assets/`。安装包位于公开 Gitee 发行仓库；官网不代理安装包，也不调用账号或云同步 API。可以把整个 `public/` 目录部署到另一家静态网站托管服务，无需移植 Worker 代码。

## 现在可用的备用入口

[Gitee 公开发行页](https://gitee.com/rooogerluo/flick-dock-releases)的 README 提供 Windows、macOS Apple 芯片和 macOS Intel 三个安装包链接。每次发布新版本时，先核验安装包，再更新这三个链接。它是下载入口，不承诺有官网的动画演示，也不能保证任意网络环境的访问质量。

Gitee Pages 和 Pages Pro 已下线，不能用来承载这个静态官网。GitHub Pages 等境外托管同样不能保证中国大陆可达。

## 正式方案：自有域名 + 大陆静态托管

推荐在有已完成 ICP 备案的自有域名后，将 `public/` 直接上传到腾讯 EdgeOne Makers，选择中国大陆或全球含中国大陆的加速区域，并将该域名绑定到生产环境。该服务的项目默认域名在中国大陆不是稳定的公开入口；大陆访问须使用符合要求的自定义域名。上线前从不同的中国大陆网络实际检查首页、CSS、JS、SVG 和三个 Gitee 安装包链接，再将正式域名公布给用户。

如果暂时没有 ICP 备案，可以先用自有域名绑定香港或境外区域的静态托管节点，或给现有 Cloudflare Worker 绑定自有域名。这样可避免仅依赖 `workers.dev` 域名，但仍走跨境网络，不保证中国大陆的访问速度与成功率。不要把官网域名接到 `flickdock-cloud` 云同步 Worker。

## 部署核对

1. 在 `website` 目录运行 `npm ci` 和 `npm run check`。
2. 选择支持静态文件直接上传的服务，将 `public/` 的**内容**放在网站根目录，确保 `/index.html`、`/styles.css`、`/site.js` 和 `/assets/` 都在同一级入口。页面使用根路径引用资源。
3. 绑定正式域名并启用 HTTPS。若使用中国大陆节点，先完成该域名的 ICP 备案，并按服务商要求展示备案号。
4. 在不同的大陆网络与非大陆网络核对资源状态、动画和三个安装包的匿名下载。单个本机或境外探测成功，不代表中国大陆可达。
5. 新版本仍按现有发布顺序：安装包及 SHA-256 校验通过后，再修改三个链接；公网入口切换前保留旧站可用。

参考：

- [Cloudflare Workers 路由与域名](https://developers.cloudflare.com/workers/configuration/routing/)
- [Cloudflare China Network 的订阅与备案要求](https://developers.cloudflare.com/china-network/)
- [腾讯 EdgeOne Makers 域名与加速区域说明](https://pages.edgeone.ai/document/domain-overview)
- [Gitee Pages 下线状态](https://gitee.com/help.com/)
