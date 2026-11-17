'use strict';
// node test.js — runs the ACP client against the fake agent (editors/test/fake_agent.py).
const assert = require('assert');
const path = require('path');
const { AcpClient } = require('./acp-client');

(async () => {
  const fake = path.join(__dirname, '..', 'test', 'fake_agent.py');
  for (const verdict of ['allow', 'reject', null]) {
    const client = new AcpClient(process.env.PYTHON || 'python3', [fake], __dirname);
    client.start();
    const updates = [];
    client.on('update', (u) => updates.push(u));
    client.onPermission = async (p) => {
      assert.strictEqual(p.toolCall.title, 'edit a.txt');
      return verdict;
    };
    const sid = await client.open(__dirname);
    assert.strictEqual(sid, 's1');
    const done = await client.prompt('fix the bug');
    assert.strictEqual(done.stopReason, 'end_turn');
    const text = updates.filter((u) => u.sessionUpdate === 'agent_message_chunk').map((u) => u.content.text).join('');
    assert.ok(text.includes('You said: fix the bug'), text);
    assert.ok(text.includes(`choice=${verdict || 'cancelled'}`), text);
    const finished = updates.find((u) => u.sessionUpdate === 'tool_call_update');
    assert.strictEqual(finished.status, verdict === 'allow' ? 'completed' : 'failed');
    assert.strictEqual(finished.content[0].type, 'diff');
    await assert.rejects(client.request('nope', {}), /method not found/);
    client.stop();
  }
  // a missing program is reported, not thrown
  const missing = new AcpClient('definitely-not-a-program-hexiel', [], __dirname);
  const closed = new Promise((r) => missing.on('closed', r));
  missing.start();
  assert.match(await closed, /could not start/);
  await assert.rejects(missing.request('initialize', {}), /not running/);
  console.log('ok: acp-client');
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
