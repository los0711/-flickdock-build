import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const project = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const html = readFileSync(resolve(project, 'public/index.html'), 'utf8');
const css = readFileSync(resolve(project, 'public/styles.css'), 'utf8');
const js = readFileSync(resolve(project, 'public/site.js'), 'utf8');
const config = JSON.parse(readFileSync(resolve(project, 'wrangler.jsonc'), 'utf8'));

assert.equal(config.name, 'flickdock-site');
assert.notEqual(config.name, 'flickdock-cloud');
assert.equal(config.assets.directory, './public');
assert.equal(config.workers_dev, true);

assert.equal(config.account_id, '59153da0809ff3d18297d943e1ef5b56');
assert.deepEqual(config.routes, [{ pattern: 'flickdock.cc.cd', custom_domain: true }]);
const section = html.slice(html.indexOf('<section id="download"')).split('</section>')[0];
const downloads = [...section.matchAll(/<a[^>]*href="([^"]+)"[^>]*data-installer="([^"]+)"[^>]*>([^<]+)<\/a>/g)];
assert.equal(downloads.length, 3, 'bottom download area keeps exactly three platform links');
const installers = Object.fromEntries(downloads.map(match => [match[2], { url: match[1], label: match[3] }]));
assert.equal(installers.windows.url, 'https://gitee.com/rooogerluo/flick-dock-windows-releases/releases/download/v1.8.24/FlickDock-Setup-1.8.24.exe');
assert.equal(installers['mac-arm'].url, 'https://gitee.com/rooogerluo/flick-dock-macos-releases-7/releases/download/v1.8.24/FlickDock-1.8.24-macOS-arm64.pkg');
assert.equal(installers['mac-intel'].url, 'https://gitee.com/rooogerluo/flick-dock-macos-releases-7/releases/download/v1.8.22/FlickDock-1.8.22-macOS-x86_64.pkg');
assert(installers['mac-intel'].label.includes('已停止更新'));
assert(!section.includes('data-ui-action="download-windows"') && !section.includes('data-ui-action="download-macos"'), 'remove only the duplicated bottom pair');
assert.equal((html.match(/data-ui-action="download-windows"/g) || []).length, 1, 'keep the original hero download control');
assert.equal((html.match(/data-ui-action="download-macos"/g) || []).length, 1);

for (const [, path] of html.matchAll(/(?:src|href)="(\/(?:assets\/[^\"]+|styles\.css|site\.js))"/g)) {
  assert(existsSync(resolve(project, 'public', path.slice(1))), `missing public asset: ${path}`);
}
for (const [, target] of html.matchAll(/href="#([^"]+)"/g)) {
  assert(html.includes(`id="${target}"`), `broken in-page link: #${target}`);
}
assert(html.includes('aria-live="polite"'));
assert(/prefers-reduced-motion:\s*reduce/.test(css));
assert(html.includes('id="demo-tabs"') && html.includes('id="theme-btn-dark"') && html.includes('id="theme-btn-light"'));
assert(js.includes('siteIconSources') && js.includes('URL.createObjectURL'), 'keep the live site self-contained icons');
assert(!js.includes('fetch('), 'the marketing page must not call the cloud sync API');
console.log('FlickDock site: config, downloads, assets, navigation, demo and accessibility checks passed.');
