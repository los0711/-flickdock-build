// Isolated Electron webview using the same Electron release as Seda 1.0.4.
const { app, BrowserWindow, clipboard } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const [folder, editor] = process.argv.slice(2);
app.setPath('userData', path.join(folder, 'profile'));
app.commandLine.appendSwitch('force-renderer-accessibility');
const draft = '这个聊天输入框里的原文需要替换。';
const message = editor === 'whatsapp'
  ? `<div id="field" contenteditable="true" role="textbox" aria-label="输入消息" aria-multiline="true" data-lexical-editor="true"><p><span data-lexical-text="true">${draft}</span></p></div>`
  : `<div id="field" contenteditable="true" role="textbox" aria-label="消息" aria-multiline="true"><span>${draft}</span></div>`;

app.whenReady().then(async () => {
  app.setAccessibilitySupportEnabled(true);
  clipboard.writeText('FlickDock test original clipboard');
  const file = path.join(folder, 'editor.html');
  fs.writeFileSync(file, `<!doctype html><meta charset="utf-8"><style>
    #field {padding: 12px; border: 1px solid #888; min-width: 330px; min-height: 64px}
    </style><p>Unrelated message in the embedded page</p>${message}
    <input id="other" value="Other input must remain unchanged">
    <script>
      const field = document.getElementById('field');
      field.dataset.inputs = '0';
      field.addEventListener('input', () => field.dataset.inputs = Number(field.dataset.inputs) + 1);
    </script>`);
  const window = new BrowserWindow({
    width: 660, height: 420, x: 120, y: 130, autoHideMenuBar: true,
    webPreferences: { webviewTag: true, contextIsolation: true, sandbox: true }
  });
  window.webContents.on('did-attach-webview', (_event, view) => {
    view.on('before-input-event', (event, input) => {
      if (input.type === 'keyDown' && input.control) {
        if (editor === 'whatsapp' && input.code === 'KeyA') event.preventDefault();
        if (editor === 'telegram' && input.code === 'KeyV') event.preventDefault();
      }
    });
    view.on('did-finish-load', async () => {
      window.focus();
      view.focus();
      await view.executeJavaScript(`(() => {
        const field = document.getElementById('field'); field.focus();
        const range = document.createRange(); range.selectNodeContents(field);
        range.collapse(false); const selection = getSelection();
        selection.removeAllRanges(); selection.addRange(range);
      })()`);
      const timer = setInterval(async () => {
        if (fs.existsSync(path.join(folder, 'exit'))) {
          clearInterval(timer); app.quit(); return;
        }
        try {
          const state = await view.executeJavaScript(`(() => {
            const field = document.getElementById('field');
            const box=field.getBoundingClientRect();
            return { text: field.innerText, selected: getSelection().toString(),
              inputs: Number(field.dataset.inputs), focused: document.activeElement === field,
              other: document.getElementById('other').value, x:Math.round(box.left+25),y:Math.round(box.top+18) };
          })()`);
          const bounds=window.getContentBounds();
          state.point=[bounds.x+state.x,bounds.y+state.y];
          state.clipboard = clipboard.readText();
          fs.writeFileSync(path.join(folder, 'state.json'), JSON.stringify(state));
        } catch { /* A concurrent inspection or shutdown can interrupt a sample. */ }
      }, 60);
    });
  });
  const source = require('node:url').pathToFileURL(file).href;
  const shell = path.join(folder, 'shell.html');
  fs.writeFileSync(shell,
    `<!doctype html><meta charset="utf-8"><body style="margin:0"><webview style="width:100vw;height:100vh" src="${source}"></webview></body>`);
  await window.loadFile(shell);
});
