'use strict';
// A small Agent Client Protocol client: spawns `hexiel --acp` and speaks newline-delimited JSON-RPC over its stdio.
// No dependency on VS Code, so it can be tested with plain node.
const { spawn } = require('child_process');
const { EventEmitter } = require('events');

class AcpClient extends EventEmitter {
  constructor(command, args, cwd) {
    super();
    this.command = command;
    this.args = args;
    this.cwd = cwd;
    this.nextId = 1;
    this.pending = new Map();
    this.buffer = '';
    this.sessionId = null;
    this.onPermission = null; // async (params) => optionId | null
  }

  start() {
    this.proc = spawn(this.command, this.args, { cwd: this.cwd, stdio: ['pipe', 'pipe', 'pipe'] });
    this.proc.stdout.setEncoding('utf8');
    this.proc.stdout.on('data', (chunk) => this._read(chunk));
    this.proc.stderr.setEncoding('utf8');
    this.proc.stderr.on('data', (text) => this.emit('stderr', text));
    this.proc.on('error', (err) => this._died(`could not start "${this.command}": ${err.message}`));
    this.proc.on('exit', (code) => this._died(`hexiel stopped (exit ${code})`));
  }

  _died(why) {
    if (this.dead) return;
    this.dead = true;
    for (const { reject } of this.pending.values()) reject(new Error(why));
    this.pending.clear();
    this.emit('closed', why);
  }

  _read(chunk) {
    this.buffer += chunk;
    let at;
    while ((at = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, at).trim();
      this.buffer = this.buffer.slice(at + 1);
      if (!line) continue;
      let msg;
      try {
        msg = JSON.parse(line);
      } catch {
        continue; // not protocol
      }
      this._handle(msg);
    }
  }

  _handle(msg) {
    if (msg.method === undefined) {
      const slot = this.pending.get(msg.id);
      if (!slot) return;
      this.pending.delete(msg.id);
      if (msg.error) slot.reject(new Error(msg.error.message || 'request failed'));
      else slot.resolve(msg.result);
    } else if (msg.method === 'session/update') {
      this.emit('update', msg.params.update);
    } else if (msg.method === 'session/request_permission') {
      void this._permission(msg);
    } else if (msg.id !== undefined) {
      this._send({ jsonrpc: '2.0', id: msg.id, error: { code: -32601, message: `not supported: ${msg.method}` } });
    }
  }

  async _permission(msg) {
    let optionId = null;
    try {
      optionId = this.onPermission ? await this.onPermission(msg.params) : null;
    } catch {
      optionId = null;
    }
    const outcome = optionId ? { outcome: 'selected', optionId } : { outcome: 'cancelled' };
    this._send({ jsonrpc: '2.0', id: msg.id, result: { outcome } });
  }

  _send(obj) {
    if (this.dead || !this.proc.stdin.writable) return;
    this.proc.stdin.write(JSON.stringify(obj) + '\n');
  }

  request(method, params) {
    if (this.dead) return Promise.reject(new Error('hexiel is not running'));
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this._send({ jsonrpc: '2.0', id, method, params });
    });
  }

  notify(method, params) {
    this._send({ jsonrpc: '2.0', method, params });
  }

  async open(cwd) {
    await this.request('initialize', {
      protocolVersion: 1,
      clientCapabilities: {},
      clientInfo: { name: 'hexiel-vscode', version: '0.1.0' },
    });
    const made = await this.request('session/new', { cwd, mcpServers: [] });
    this.sessionId = made.sessionId;
    return this.sessionId;
  }

  prompt(text) {
    return this.request('session/prompt', { sessionId: this.sessionId, prompt: [{ type: 'text', text }] });
  }

  cancel() {
    this.notify('session/cancel', { sessionId: this.sessionId });
  }

  stop() {
    try {
      this.proc.kill();
    } catch {
      /* already gone */
    }
  }
}

module.exports = { AcpClient };
