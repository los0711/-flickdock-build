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

// The official assets already include the shared blob-icon runtime.
const generated = names.map(name => [name, readFileSync(join(publicRoot, name), 'utf8')]);
for (const [name, content] of generated) {
  if (content.includes('/assets/') || content.includes('data:image/svg+xml')) {
    throw new Error(`Disallowed image reference in ${name}`);
  }
}

mkdirSync(outputRoot, { recursive: true });
for (const [name, content] of generated) {
  writeFileSync(join(outputRoot, name), content, 'utf8');
}
console.log(`Exported ${names.length} self-contained official site files: ${outputRoot}`);
