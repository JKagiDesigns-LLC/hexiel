'use strict';
// hexiel for VS Code: a chat panel on top of `hexiel --acp` (Agent Client Protocol over stdio).
const vscode = require('vscode');
const { AcpClient } = require('./acp-client');

let panel;
let client;
let busy = false;

function workspaceDir() {
  const folder = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0];
  return folder ? folder.uri.fsPath : process.cwd();
}

function post(msg) {
  if (panel) panel.webview.postMessage(msg);
}

async function ensureClient() {
  if (client && !client.dead && client.sessionId) return client;
  const cfg = vscode.workspace.getConfiguration('hexiel');
  client = new AcpClient(cfg.get('command', 'hexiel'), cfg.get('args', ['--acp']), workspaceDir());
  client.on('update', (u) => post({ type: 'update', update: u }));
  client.on('closed', (why) => {
    busy = false;
    post({ type: 'error', text: why + ' — is hexiel installed? (pipx install hexiel)' });
    post({ type: 'busy', value: false });
  });
  client.onPermission = (params) =>
    new Promise((resolve) => {
      pendingPermission = resolve;
      post({ type: 'permission', title: params.toolCall && params.toolCall.title, options: params.options });
    });
  client.start();
  await client.open(workspaceDir());
  return client;
}

let pendingPermission = null;

async function ask(text) {
  if (busy) {
    vscode.window.showInformationMessage('hexiel is still working — stop it first or wait.');
    return;
  }
  openPanel();
  post({ type: 'user', text });
  busy = true;
  post({ type: 'busy', value: true });
  try {
    const c = await ensureClient();
    const done = await c.prompt(text);
    if (done && done.stopReason === 'cancelled') post({ type: 'note', text: 'Stopped.' });
  } catch (err) {
    post({ type: 'error', text: String(err.message || err) });
  } finally {
    busy = false;
    post({ type: 'busy', value: false });
  }
}

function fence(editor, text) {
  const rel = vscode.workspace.asRelativePath(editor.document.uri);
  const lang = editor.document.languageId;
  return `${rel}:\n\`\`\`${lang}\n${text}\n\`\`\``;
}

async function openDiff(msg) {
  const left = await vscode.workspace.openTextDocument({ content: msg.oldText || '' });
  const right = await vscode.workspace.openTextDocument({ content: msg.newText || '' });
  await vscode.commands.executeCommand('vscode.diff', left.uri, right.uri, `hexiel: ${msg.path}`);
}

function openPanel() {
  if (panel) {
    panel.reveal(vscode.ViewColumn.Beside, true);
    return;
  }
  panel = vscode.window.createWebviewPanel('hexiel', 'hexiel', { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
    { enableScripts: true, retainContextWhenHidden: true });
  panel.webview.html = html();
  panel.onDidDispose(() => (panel = undefined));
  panel.webview.onDidReceiveMessage((m) => {
    if (m.type === 'send') void ask(m.text);
    else if (m.type === 'stop') client && client.cancel();
    else if (m.type === 'permission' && pendingPermission) {
      pendingPermission(m.optionId);
      pendingPermission = null;
    } else if (m.type === 'diff') void openDiff(m);
  });
}

function html() {
  return `<!doctype html><html><head><meta charset="utf-8"><style>
  body{font-family:var(--vscode-font-family);color:var(--vscode-foreground);margin:0;display:flex;flex-direction:column;height:100vh}
  #log{flex:1;overflow:auto;padding:10px}
  .user{background:var(--vscode-editor-inactiveSelectionBackground);padding:6px 10px;border-radius:6px;margin:8px 0;white-space:pre-wrap}
  .agent{white-space:pre-wrap;margin:8px 0}
  .tool{font-family:var(--vscode-editor-font-family);font-size:.9em;opacity:.85;border-left:3px solid var(--vscode-charts-green);padding:2px 8px;margin:6px 0}
  .tool.failed{border-color:var(--vscode-charts-red)}
  .note{opacity:.7;font-style:italic}.err{color:var(--vscode-errorForeground)}
  #perm{display:none;padding:8px 10px;background:var(--vscode-inputValidation-warningBackground)}
  #bar{display:flex;gap:6px;padding:8px;border-top:1px solid var(--vscode-panel-border)}
  textarea{flex:1;resize:none;background:var(--vscode-input-background);color:var(--vscode-input-foreground);border:1px solid var(--vscode-input-border);padding:6px}
  button{background:var(--vscode-button-background);color:var(--vscode-button-foreground);border:0;padding:4px 10px;cursor:pointer}
  </style></head><body><div id="log"></div><div id="perm"></div>
  <div id="bar"><textarea id="box" rows="2" placeholder="Ask hexiel…  (Enter sends, Shift+Enter for a new line)"></textarea>
  <button id="send">Send</button><button id="stop" hidden>Stop</button></div>
  <script>
  const vs = acquireVsCodeApi(), log = document.getElementById('log'), box = document.getElementById('box');
  let agent = null;
  const add = (cls, text) => { const d = document.createElement('div'); d.className = cls; d.textContent = text; log.appendChild(d); log.scrollTop = log.scrollHeight; return d; };
  const send = () => { const t = box.value.trim(); if (t) { vs.postMessage({ type: 'send', text: t }); box.value = ''; } };
  document.getElementById('send').onclick = send;
  document.getElementById('stop').onclick = () => vs.postMessage({ type: 'stop' });
  box.onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } };
  const tools = {};
  window.addEventListener('message', ({ data: m }) => {
    if (m.type === 'user') { agent = null; add('user', m.text); }
    else if (m.type === 'busy') { document.getElementById('stop').hidden = !m.value; document.getElementById('send').disabled = m.value; }
    else if (m.type === 'error') add('err', m.text);
    else if (m.type === 'note') add('note', m.text);
    else if (m.type === 'permission') {
      const p = document.getElementById('perm'); p.style.display = 'block'; p.textContent = 'Allow: ' + (m.title || 'a tool') + '  ';
      for (const o of m.options) { const b = document.createElement('button'); b.textContent = o.name; b.onclick = () => { p.style.display = 'none'; vs.postMessage({ type: 'permission', optionId: o.optionId }); }; p.appendChild(b); p.append(' '); }
    } else if (m.type === 'update') {
      const u = m.update;
      if (u.sessionUpdate === 'agent_message_chunk') { agent = agent || add('agent', ''); agent.textContent += u.content.text; log.scrollTop = log.scrollHeight; }
      else if (u.sessionUpdate === 'tool_call') { agent = null; tools[u.toolCallId] = add('tool', '⏺ ' + u.title); }
      else if (u.sessionUpdate === 'tool_call_update') {
        const el = tools[u.toolCallId] || add('tool', '⏺ tool'); if (u.status === 'failed') el.classList.add('failed');
        for (const c of u.content || []) if (c.type === 'diff') { const b = document.createElement('button'); b.textContent = 'View diff'; b.style.marginLeft = '8px'; b.onclick = () => vs.postMessage({ type: 'diff', path: c.path, oldText: c.oldText, newText: c.newText }); el.appendChild(b); }
      }
    }
  });
  </script></body></html>`;
}

function activate(context) {
  const reg = (name, fn) => context.subscriptions.push(vscode.commands.registerCommand(name, fn));
  reg('hexiel.open', openPanel);
  reg('hexiel.stop', () => client && client.cancel());
  reg('hexiel.newChat', () => {
    if (client) client.stop();
    client = undefined;
    busy = false;
    if (panel) panel.dispose();
    openPanel();
  });
  reg('hexiel.sendSelection', async () => {
    const editor = vscode.window.activeTextEditor;
    if (!editor || editor.selection.isEmpty) return vscode.window.showInformationMessage('Select some code first.');
    const question = await vscode.window.showInputBox({ prompt: 'What should hexiel do with this selection?', value: 'Explain this.' });
    if (question) await ask(`${question}\n\n${fence(editor, editor.document.getText(editor.selection))}`);
  });
  reg('hexiel.sendFile', async () => {
    const editor = vscode.window.activeTextEditor;
    if (!editor) return;
    const rel = vscode.workspace.asRelativePath(editor.document.uri);
    const question = await vscode.window.showInputBox({ prompt: `What should hexiel do with ${rel}?`, value: 'Review this file.' });
    if (question) await ask(`${question}\n\n@${rel}`);
  });
}

function deactivate() {
  if (client) client.stop();
}

module.exports = { activate, deactivate };
