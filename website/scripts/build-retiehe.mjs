import { existsSync, mkdirSync, readFileSync, readdirSync, realpathSync, writeFileSync } from 'node:fs';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const siteRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const publicRoot = join(siteRoot, 'public');
const names = ['index.html', 'styles.css', 'site.js'];
const outputArg = process.argv[2];

if (!outputArg || process.argv.length !== 3) {
  throw new Error('Usage: npm run build:retiehe -- <empty-output-directory>');
}

const outputRoot = resolve(outputArg);
const isInside = (parent, child) => {
  const path = relative(parent, child);
  return path === '' || (path !== '..' && !path.startsWith(`..${sep}`) && !isAbsolute(path));
};

// Keep the original Worker assets and source tree untouched.
if (isInside(siteRoot, outputRoot) || isInside(outputRoot, siteRoot)) {
  throw new Error('Output directory must be outside the website source tree.');
}
if (existsSync(outputRoot)) {
  const realOutput = realpathSync(outputRoot);
  if (isInside(siteRoot, realOutput) || isInside(realOutput, siteRoot)) {
    throw new Error('Output directory resolves into the website source tree.');
  }
  if (readdirSync(outputRoot).length) {
    throw new Error('Output directory must be empty to avoid overwriting files.');
  }
}

const svgSources = new Map();
let replaced = 0;
function useIcon(reference, assetName) {
  const assetPath = resolve(publicRoot, 'assets', assetName);
  if (!isInside(join(publicRoot, 'assets'), assetPath) || !existsSync(assetPath)) {
    throw new Error(`Missing or unsafe SVG reference: ${reference}`);
  }
  if (!svgSources.has(assetName)) {
    const svg = readFileSync(assetPath, 'utf8').trim();
    if (!svg.startsWith('<svg') || !svg.endsWith('</svg>')) {
      throw new Error(`Invalid SVG file: ${assetPath}`);
    }
    svgSources.set(assetName, svg);
  }
  replaced += 1;
  return assetName;
}

const html = readFileSync(join(publicRoot, 'index.html'), 'utf8').replace(
  /\b(src|href)=(['"])\/assets\/([\w./-]+\.svg)\2/g,
  (reference, _attribute, _quote, assetName) => `data-icon="${useIcon(reference, assetName)}"`,
);
const css = readFileSync(join(publicRoot, 'styles.css'), 'utf8').replace(
  /url\(\s*(['"]?)\/assets\/([\w./-]+\.svg)\1\s*\)/g,
  (reference, _quote, assetName) => {
    const name = useIcon(reference, assetName);
    return `var(--site-icon-${name.replace(/[^a-z0-9]/gi, '-')})`;
  },
);
const js = readFileSync(join(publicRoot, 'site.js'), 'utf8').replace(
  /(['"])\/assets\/([\w./-]+\.svg)\1/g,
  (reference, _quote, assetName) => `siteIconUrl(${JSON.stringify(useIcon(reference, assetName))})`,
);

const iconRuntime = `// Static hosting CSP allows blob: images but blocks data: images.\n` +
  `const siteIconSources = ${JSON.stringify(Object.fromEntries(svgSources))};\n` +
  `const siteIconUrls = Object.fromEntries(Object.entries(siteIconSources).map(([name, svg]) =>\n` +
  `  [name, URL.createObjectURL(new Blob([svg], { type: 'image/svg+xml' }))]));\n` +
  `function siteIconUrl(name) {\n` +
  `  const url = siteIconUrls[name];\n` +
  `  if (!url) throw new Error(\`Missing site icon: \${name}\`);\n` +
  `  return url;\n` +
  `}\n` +
  `document.querySelectorAll('img[data-icon]').forEach((img) => {\n` +
  `  img.addEventListener('load', () => img.classList.add('icon-ready'), { once: true });\n` +
  `  img.src = siteIconUrl(img.dataset.icon);\n` +
  `  if (img.complete && img.naturalWidth > 0) img.classList.add('icon-ready');\n` +
  `});\n` +
  `document.querySelectorAll('link[rel~="icon"][data-icon]').forEach((link) => {\n` +
  `  link.href = siteIconUrl(link.dataset.icon);\n` +
  `});\n` +
  `for (const name of Object.keys(siteIconUrls)) {\n` +
  `  document.documentElement.style.setProperty(\n` +
  `    \`--site-icon-\${name.replace(/[^a-z0-9]/gi, '-')}\`,\n` +
  `    \`url("\${siteIconUrl(name)}")\`,\n` +
  `  );\n` +
  `}\n\n`;

const generated = [
  ['index.html', html],
  ['styles.css', `${css}\nimg[data-icon] { visibility: hidden; }\nimg[data-icon].icon-ready { visibility: visible; }\n`],
  ['site.js', iconRuntime + js],
];
for (const [name, content] of generated) {
  if (content.includes('/assets/') || content.includes('data:image/svg+xml')) {
    throw new Error(`Disallowed image reference in ${name}`);
  }
}

if (replaced === 0) {
  throw new Error('No SVG references found; check the website source before publishing.');
}

mkdirSync(outputRoot, { recursive: true });
for (const [name, content] of generated) {
  writeFileSync(join(outputRoot, name), content, 'utf8');
}
console.log(`Built ${names.length} static files with ${replaced} SVG references backed by blob URLs: ${outputRoot}`);
