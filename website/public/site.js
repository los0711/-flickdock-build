// Static hosting CSP allows blob: images but blocks data: images.
const siteIconSources = {"flickdock.svg":"<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 64 64\"><defs><linearGradient id=\"blue\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"1\"><stop stop-color=\"#47a7ff\"/><stop offset=\".52\" stop-color=\"#1686f6\"/><stop offset=\"1\" stop-color=\"#0964dd\"/></linearGradient></defs><rect x=\"2.5\" y=\"2.5\" width=\"59\" height=\"59\" rx=\"14\" fill=\"url(#blue)\" stroke=\"#0d70df\" stroke-width=\".5\"/><rect x=\"3.3\" y=\"3.3\" width=\"57.4\" height=\"57.4\" rx=\"13.5\" fill=\"none\" stroke=\"#ffffff\" stroke-opacity=\".27\" stroke-width=\".6\"/><rect x=\"20\" y=\"13\" width=\"24\" height=\"22\" rx=\"3.5\" fill=\"#ffffff\" opacity=\".65\"/><rect x=\"16\" y=\"19\" width=\"32\" height=\"23\" rx=\"4\" fill=\"#ffffff\" opacity=\".86\"/><path d=\"M13 30h9c3 0 3 7 8 7h4c5 0 5-7 8-7h9v12c0 5-3 7-7 7H20c-4 0-7-2-7-7z\" fill=\"#ffffff\"/></svg>","control-check.svg":"<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\"><g fill=\"none\" stroke=\"#6e7078\" stroke-width=\"1.65\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"m5 12 5 5L19 7\"/></g></svg>","control-nearby.svg":"<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\"><g fill=\"none\" stroke=\"#6e7078\" stroke-width=\"1.65\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"3\" y=\"5\" width=\"12\" height=\"10\" rx=\"2\"/><path d=\"M2 18h14M18 8h3v11h-4V8h1zm-9 2 3 2-3 2\"/></g></svg>","content-archive.svg":"<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 64 64\"><defs><linearGradient id=\"paper\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"1\"><stop stop-color=\"#cf9728\"/><stop offset=\"1\" stop-color=\"#bc8924\"/></linearGradient></defs><rect x=\"13\" y=\"5\" width=\"43\" height=\"51\" rx=\"7\" fill=\"url(#paper)\"/><path d=\"M41 5h8a7 7 0 0 1 7 7v8z\" fill=\"#ffffff\" fill-opacity=\".23\"/><rect x=\"13.5\" y=\"5.5\" width=\"42\" height=\"50\" rx=\"6.5\" fill=\"none\" stroke=\"#ffffff\" stroke-opacity=\".18\"/><g transform=\"translate(0,-11)\" opacity=\".3\" fill=\"none\" stroke=\"#ffffff\" stroke-width=\"1.6\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M29 26h6m-6 4h6m-6 4h6m-6 4h6\"/><rect x=\"28\" y=\"41\" width=\"8\" height=\"9\" rx=\"2\"/><path d=\"M31 45h2\"/></g><text x=\"34.5\" y=\"44\" text-anchor=\"middle\" font-family=\"HarmonyOS Sans SC,Arial,sans-serif\" font-size=\"14\" font-weight=\"600\" fill=\"#ffffff\">ZIP</text></svg>","content-image.svg":"<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 64 64\"><defs><linearGradient id=\"paper\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"1\"><stop stop-color=\"#2daa90\"/><stop offset=\"1\" stop-color=\"#299b83\"/></linearGradient></defs><rect x=\"13\" y=\"5\" width=\"43\" height=\"51\" rx=\"7\" fill=\"url(#paper)\"/><path d=\"M41 5h8a7 7 0 0 1 7 7v8z\" fill=\"#ffffff\" fill-opacity=\".23\"/><rect x=\"13.5\" y=\"5.5\" width=\"42\" height=\"50\" rx=\"6.5\" fill=\"none\" stroke=\"#ffffff\" stroke-opacity=\".18\"/><g transform=\"translate(0,-11)\" opacity=\".3\" fill=\"none\" stroke=\"#ffffff\" stroke-width=\"1.6\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"21\" y=\"28\" width=\"24\" height=\"20\" rx=\"4\"/><circle cx=\"27\" cy=\"34\" r=\"2\"/><path d=\"m22 45 7-7 4 4 6-8 5 7\"/></g><text x=\"34.5\" y=\"44\" text-anchor=\"middle\" font-family=\"HarmonyOS Sans SC,Arial,sans-serif\" font-size=\"14\" font-weight=\"600\" fill=\"#ffffff\">IMG</text></svg>","content-text.svg":"<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 64 64\"><defs><linearGradient id=\"paper\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"1\"><stop stop-color=\"#7584d0\"/><stop offset=\"1\" stop-color=\"#6a78bd\"/></linearGradient></defs><rect x=\"13\" y=\"5\" width=\"43\" height=\"51\" rx=\"7\" fill=\"url(#paper)\"/><path d=\"M41 5h8a7 7 0 0 1 7 7v8z\" fill=\"#ffffff\" fill-opacity=\".23\"/><rect x=\"13.5\" y=\"5.5\" width=\"42\" height=\"50\" rx=\"6.5\" fill=\"none\" stroke=\"#ffffff\" stroke-opacity=\".18\"/><g transform=\"translate(0,-11)\" opacity=\".3\" fill=\"none\" stroke=\"#ffffff\" stroke-width=\"1.6\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M23 29h18M23 35h18M23 41h18M23 47h12\"/></g><text x=\"34.5\" y=\"44\" text-anchor=\"middle\" font-family=\"HarmonyOS Sans SC,Arial,sans-serif\" font-size=\"12\" font-weight=\"600\" fill=\"#ffffff\">TEXT</text></svg>"};
const siteIconUrls = Object.fromEntries(Object.entries(siteIconSources).map(([name, svg]) =>
  [name, URL.createObjectURL(new Blob([svg], { type: 'image/svg+xml' }))]));
function siteIconUrl(name) {
  const url = siteIconUrls[name];
  if (!url) throw new Error(`Missing site icon: ${name}`);
  return url;
}
document.querySelectorAll('img[data-icon]').forEach((img) => {
  img.addEventListener('load', () => img.classList.add('icon-ready'), { once: true });
  img.src = siteIconUrl(img.dataset.icon);
  if (img.complete && img.naturalWidth > 0) img.classList.add('icon-ready');
});
document.querySelectorAll('link[rel~="icon"][data-icon]').forEach((link) => {
  link.href = siteIconUrl(link.dataset.icon);
});
for (const name of Object.keys(siteIconUrls)) {
  document.documentElement.style.setProperty(
    `--site-icon-${name.replace(/[^a-z0-9]/gi, '-')}`,
    `url("${siteIconUrl(name)}")`,
  );
}

// Website illustrations are demonstrations; they do not access local files.
const motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
let currentScene = 0;
let sceneTimer = null;
let demoVisible = false;
let paused = motionQuery.matches;
let sceneFocused = false;
let toastTimeout = null;

function setTheme(theme, remember = true) {
  document.body.classList.toggle('theme-light', theme === 'light');
  document.documentElement.style.colorScheme = theme;
  ['dark', 'light'].forEach(value => {
    document.getElementById(`theme-btn-${value}`).setAttribute('aria-pressed', String(value === theme));
  });
  if (remember) { try { localStorage.setItem('flickdock-site-theme', theme); } catch {} }
}
let savedTheme;
try { savedTheme = localStorage.getItem('flickdock-site-theme'); } catch {}
// The official website starts dark; an explicit visitor choice is retained.
setTheme(savedTheme === 'light' ? 'light' : 'dark', false);

function syncPlayback() {
  clearInterval(sceneTimer);
  const running = !paused && !motionQuery.matches && demoVisible && !document.hidden && !sceneFocused;
  document.body.classList.toggle('motion-paused', paused || motionQuery.matches || document.hidden);
  document.getElementById('demo-playback').textContent = paused || motionQuery.matches ? '播放演示' : '暂停演示';
  document.getElementById('demo-playback').setAttribute('aria-pressed', String(!paused && !motionQuery.matches));
  if (running) sceneTimer = setInterval(() => switchScene((currentScene + 1) % 3), 6500);
}

function switchScene(index, focus = false) {
  currentScene = index;
  [0, 1, 2].forEach(value => {
    const selected = value === index;
    const scene = document.getElementById(`scene-${value}`);
    scene.hidden = !selected;
    scene.classList.toggle('hidden', !selected);
    const tab = document.getElementById(`scene-tab-${value}`);
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
    if (selected && focus) tab.focus();
  });
  syncPlayback();
}

const tabs = document.getElementById('demo-tabs');
tabs.addEventListener('keydown', event => {
  let next = currentScene;
  if (event.key === 'ArrowRight') next = (currentScene + 1) % 3;
  else if (event.key === 'ArrowLeft') next = (currentScene + 2) % 3;
  else if (event.key === 'Home') next = 0;
  else if (event.key === 'End') next = 2;
  else return;
  event.preventDefault();
  switchScene(next, true);
});
tabs.addEventListener('focusin', () => { sceneFocused = true; syncPlayback(); });
tabs.addEventListener('focusout', event => {
  if (!tabs.contains(event.relatedTarget)) { sceneFocused = false; syncPlayback(); }
});
new IntersectionObserver(entries => {
  demoVisible = entries[0].isIntersecting;
  syncPlayback();
}, { threshold: 0.15 }).observe(document.getElementById('demo-stage'));
document.addEventListener('visibilitychange', syncPlayback);
motionQuery.addEventListener('change', () => { paused = motionQuery.matches; syncPlayback(); });
document.getElementById('demo-playback').addEventListener('click', () => {
  if (motionQuery.matches) { showToast('已遵循系统的减少动态效果设置'); return; }
  paused = !paused;
  syncPlayback();
});

function showToast(message) {
  const toast = document.getElementById('promo-toast');
  document.getElementById('promo-toast-text').textContent = message;
  toast.classList.remove('opacity-0', 'translate-y-2');
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => toast.classList.add('opacity-0', 'translate-y-2'), 3000);
}

const actions = {
  'theme-dark': () => setTheme('dark'),
  'theme-light': () => setTheme('light'),
  'download-windows': () => window.location.assign(document.querySelector('[data-installer="windows"]').href),
  'download-macos': () => {
    document.getElementById('download').scrollIntoView({ behavior: motionQuery.matches ? 'instant' : 'smooth' });
    document.querySelector('[data-installer="mac-arm"]').focus({ preventScroll: true });
    showToast('请选择适合 Mac 芯片的安装包');
  },
  'scene-0': () => switchScene(0),
  'scene-1': () => switchScene(1),
  'scene-2': () => switchScene(2),
};
document.querySelectorAll('[data-ui-action]').forEach(element => {
  element.addEventListener('click', actions[element.dataset.uiAction]);
});

// Coarse pointers and reduced motion keep a stable stage. Desktop parallax
// uses one animation-frame write, rather than a style write for every event.
const parallax = document.querySelector('[data-parallax="hero"]');
const deck = document.getElementById('hero-3d-deck');
let frame = null;
parallax.addEventListener('pointermove', event => {
  if (motionQuery.matches || paused || event.pointerType !== 'mouse' || frame !== null) return;
  const rect = parallax.getBoundingClientRect();
  const x = ((event.clientX - rect.left) / rect.width - 0.5) * 5;
  const y = 4 - ((event.clientY - rect.top) / rect.height - 0.5) * 4;
  frame = requestAnimationFrame(() => {
    deck.style.transform = `rotateX(${y.toFixed(2)}deg) rotateY(${x.toFixed(2)}deg)`;
    frame = null;
  });
});
parallax.addEventListener('pointerleave', () => {
  if (frame !== null) cancelAnimationFrame(frame);
  frame = null;
  deck.style.transform = '';
});
switchScene(0);
