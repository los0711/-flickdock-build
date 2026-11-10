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
assert.equal(installers.windows.url, 'https://gitee.com/rooogerluo/flick-dock-windows-releases-2/releases/download/v1.8.28/FlickDock-Setup-1.8.28.exe');
assert.equal(installers['mac-arm'].url, 'https://gitee.com/rooogerluo/flick-dock-macos-releases-8/releases/download/v1.8.28/FlickDock-1.8.28-macOS-arm64.pkg');
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

// Network boundary checks use a controlled upstream; the deployed route is also
// verified by downloading and hashing the actual published DMG before promotion.
const worker=(await import('../worker.mjs')).default;
const originalFetch=globalThis.fetch;let forwarded=[];
try{
  globalThis.fetch=async(url,options)=>{forwarded.push({url,options});return new Response(new Uint8Array([1,2,3]),
    {status:206,headers:{'Content-Length':'3','Content-Range':'bytes 0-2/9','Accept-Ranges':'bytes','Content-Type':'application/octet-stream'}});};
  const url='https://flickdock.cc.cd/downloads/FlickDock-1.8.27-macOS-arm64.dmg';
  let reply=await worker.fetch(new Request(url,{headers:{Authorization:'Bearer should-never-forward',Cookie:'private=1',Range:'bytes=0-2'}}),{});
  assert.equal(reply.status,206);assert.deepEqual([...new Uint8Array(await reply.arrayBuffer())],[1,2,3]);
  assert.equal(reply.headers.get('Content-Range'),'bytes 0-2/9');
  assert.equal(forwarded[0].url,'https://github.com/los0711/-flickdock-build/releases/download/v1.8.27/FlickDock-1.8.27-macOS-arm64.dmg');
  assert(!forwarded[0].options.headers.has('Authorization'));assert(!forwarded[0].options.headers.has('Cookie'));
  const before=forwarded.length;
  for(const path of ['/downloads/https://private.example/file','/downloads/FlickDock-01.8.27-macOS-arm64.dmg','/downloads/FlickDock-1.8.27-source.zip']){
    assert.equal((await worker.fetch(new Request('https://flickdock.cc.cd'+path),{})).status,404);
  }
  assert.equal((await worker.fetch(new Request(url,{method:'POST'}),{})).status,405);
  assert.equal((await worker.fetch(new Request(url,{headers:{Range:'bytes=0-1,3-4'}}),{})).status,416);
  assert.equal(forwarded.length,before);
  globalThis.fetch=async()=>new Response('<html>login</html>',{headers:{'Content-Type':'text/html'}});
  assert.equal((await worker.fetch(new Request(url),{})).status,502);
  console.log('Immutable installer gateway: exact public repository, credential isolation, range, method and response validation passed.');
}finally{globalThis.fetch=originalFetch;}
