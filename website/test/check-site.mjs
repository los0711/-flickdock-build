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

const downloads = [...html.matchAll(/href="(https:\/\/github\.com\/los0711\/-flickdock-build\/releases\/download\/v1\.8\.23\/[^\"]+\.(?:exe|dmg))"/g)].map(match => match[1]);
assert.equal(downloads.length, 3, 'home page has three direct installer links');
assert(downloads.includes('https://github.com/los0711/-flickdock-build/releases/download/v1.8.23/FlickDock-Setup-1.8.23.exe'));
assert(downloads.some(url => url.endsWith('/FlickDock-1.8.23-macOS-arm64.dmg')));
assert(downloads.some(url => url.endsWith('/FlickDock-1.8.23-macOS-x86_64.dmg')));
assert(downloads.every(url => url.startsWith('https://github.com/los0711/-flickdock-build/releases/download/v1.8.23/')));
assert(html.includes('aria-label="下载 Windows 安装包 1.8.23"'));
assert(html.includes('安装程序 · 1.8.23'));
assert(html.includes('href="https://github.com/los0711/-flickdock-build/releases/tag/v1.8.23"'), 'download help links to the published release');

for (const [, path] of html.matchAll(/(?:src|href)="(\/(?:assets\/[^\"]+|styles\.css|site\.js))"/g)) {
  assert(existsSync(resolve(project, 'public', path.slice(1))), `missing public asset: ${path}`);
}
for (const [, target] of html.matchAll(/href="#([^"]+)"/g)) {
  assert(html.includes(`id="${target}"`), `broken in-page link: #${target}`);
}
assert(html.includes('aria-live="polite"'));
assert(html.includes('Apple 签名或公证'));
assert(css.includes('prefers-reduced-motion: reduce'));
assert(js.includes("text/flickdock-demo"), 'demo accepts only its own sample drag payload');
assert(!js.includes('fetch('), 'the marketing page must not call the cloud sync API');
console.log('FlickDock site: config, downloads, assets, navigation, demo and accessibility checks passed.');
