const demoFiles = {
  image: { name: '旅行照片.png', icon: '/assets/content-image.svg', type: '图片' },
  video: { name: '周末片段.mp4', icon: '/assets/content-video.svg', type: '视频' },
  pdf: { name: '设计提案.pdf', icon: '/assets/content-pdf.svg', type: '文件' },
  text: { name: '一段灵感', icon: '/assets/content-text.svg', type: '文字' },
};

const stage = document.querySelector('.demo-stage');
const dropzone = document.querySelector('#demo-dropzone');
const stack = document.querySelector('#demo-stack');
const count = document.querySelector('#demo-count');
const latest = document.querySelector('#demo-latest');
const overview = document.querySelector('#demo-overview');
const overviewItems = document.querySelector('#demo-overview-items');
const toggle = document.querySelector('#demo-toggle');
const reset = document.querySelector('#demo-reset');
const announcement = document.querySelector('#demo-announcement');
const seed = ['image', 'video', 'text'];
let items = [...seed];
let dragSource = null;

function renderDemo() {
  const top = demoFiles[items[0]];
  count.textContent = `${items.length} 项`;
  latest.textContent = top ? top.name : '拖入示例文件';
  stack.replaceChildren();
  items.slice(0, 5).reverse().forEach((kind, index, visible) => {
    const img = document.createElement('img');
    img.className = 'stack-card';
    img.src = demoFiles[kind].icon;
    img.alt = '';
    const fromFront = visible.length - index - 1;
    img.style.transform = `translate(${6 - fromFront * 8}px, ${-6 + fromFront * 7}px) rotate(${3 - fromFront * 6}deg)`;
    img.style.zIndex = String(index + 1);
    stack.append(img);
  });
  overviewItems.replaceChildren();
  items.forEach((kind) => {
    const row = document.createElement('span');
    row.className = 'demo-overview-item';
    const icon = document.createElement('img');
    icon.src = demoFiles[kind].icon;
    icon.alt = '';
    const name = document.createElement('span');
    name.textContent = demoFiles[kind].name;
    row.append(icon, name);
    overviewItems.append(row);
  });
}

function animateToShelf(source, kind) {
  if (!source || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return Promise.resolve();
  const icon = source.querySelector('img');
  if (!icon) return Promise.resolve();
  const a = icon.getBoundingClientRect();
  const b = stack.getBoundingClientRect();
  const clone = icon.cloneNode();
  clone.className = 'demo-fly';
  clone.alt = '';
  clone.style.left = `${a.left}px`;
  clone.style.top = `${a.top}px`;
  clone.style.width = `${a.width}px`;
  clone.style.height = `${a.height}px`;
  document.body.append(clone);
  const dx = b.left + b.width / 2 - a.left - a.width / 2;
  const dy = b.top + b.height / 2 - a.top - a.height / 2;
  const animation = clone.animate([
    { transform: 'translate(0, 0) scale(1)', opacity: 1 },
    { transform: `translate(${dx * .56}px, ${dy * .4 - 28}px) scale(1.25)`, opacity: 1, offset: .56 },
    { transform: `translate(${dx}px, ${dy}px) scale(.92)`, opacity: .95 },
  ], { duration: 500, easing: 'cubic-bezier(.22,.78,.22,1)' });
  return animation.finished.catch(() => {}).finally(() => clone.remove());
}

async function addDemoFile(kind, source) {
  if (!Object.hasOwn(demoFiles, kind)) return;
  await animateToShelf(source, kind);
  items.unshift(kind);
  renderDemo();
  dropzone.classList.remove('is-receiving');
  void dropzone.offsetWidth;
  dropzone.classList.add('is-receiving');
  announcement.textContent = `已将${demoFiles[kind].name}放入演示搁架，现在有${items.length}项。`;
}

document.querySelectorAll('.demo-file').forEach((source) => {
  source.addEventListener('click', () => addDemoFile(source.dataset.kind, source));
  source.addEventListener('dragstart', (event) => {
    dragSource = source;
    event.dataTransfer.effectAllowed = 'copy';
    event.dataTransfer.setData('text/flickdock-demo', source.dataset.kind);
  });
  source.addEventListener('dragend', () => {
    dragSource = null;
    dropzone.classList.remove('is-drag-over');
  });
});

dropzone.addEventListener('dragover', (event) => {
  if (!event.dataTransfer.types.includes('text/flickdock-demo')) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = 'copy';
  dropzone.classList.add('is-drag-over');
});
dropzone.addEventListener('dragleave', (event) => {
  if (!dropzone.contains(event.relatedTarget)) dropzone.classList.remove('is-drag-over');
});
dropzone.addEventListener('drop', (event) => {
  const kind = event.dataTransfer.getData('text/flickdock-demo');
  if (!Object.hasOwn(demoFiles, kind)) return;
  event.preventDefault();
  dropzone.classList.remove('is-drag-over');
  addDemoFile(kind, dragSource);
});

toggle.addEventListener('click', () => {
  const expanding = overview.hidden;
  overview.hidden = !expanding;
  toggle.setAttribute('aria-expanded', String(expanding));
  toggle.innerHTML = expanding ? '收起总览 <span aria-hidden="true">↗</span>' : '展开总览 <span aria-hidden="true">↗</span>';
  announcement.textContent = expanding ? `总览已展开，共${items.length}项。` : '总览已收起。';
});
reset.addEventListener('click', () => {
  items = [...seed];
  renderDemo();
  announcement.textContent = '演示已重置为三项示例文件。';
});
renderDemo();

if ('IntersectionObserver' in window) {
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      entry.target.classList.add('is-visible');
      observer.unobserve(entry.target);
    });
  }, { threshold: .18 });
  document.querySelectorAll('.section-copy, .feature-head, .feature-row, .steps-lead, .steps-copy, .download-heading, .download-card').forEach((element) => {
    element.classList.add('reveal');
    observer.observe(element);
  });
}
