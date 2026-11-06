// Run with: node tests/test_widget_ui.js
// A tiny DOM/TelegramWidget simulator: catches regressions without Telegram.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const html = fs.readFileSync('bot/widgets/browse.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert.ok(script, 'Widget script is missing');
new vm.Script(script); // syntax check

class Element {
  constructor() {
    this.children = [];
    this.listeners = {};
    this.className = '';
    this.disabled = false;
    this.classList = { add() {}, remove() {} };
    this.value = '';
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text; }
  appendChild(el) { this.children.push(el); return el; }
  addEventListener(type, cb) { this.listeners[type] = cb; }
  click() { return this.listeners.click?.(); }
}

function harness(chatData = JSON.stringify({ k: 'abc' })) {
  const nodes = {};
  const document = {
    getElementById(id) { return nodes[id] ??= new Element(); },
    createElement() { return new Element(); },
  };
  const calls = [];
  const initial = {
    ok: 1, p: 0, d: 0, total: 2, pages: 1, title: 'Зарубежные направления', date: '2026-12-03',
    rows: [
      { i: 0, c: 'Стамбул', n: 'Турция', p: 9000, t: 1 },
      { i: 1, c: 'Ереван', n: 'Армения', p: 12000, t: 0 },
    ],
  };
  const TelegramWidget = {
    chatData,
    publicData: JSON.stringify(initial),
    ready() { calls.push('ready'); },
    openLink(url) { calls.push('link:' + url); },
    async request(command) {
      calls.push(command);
      if (command.startsWith('v:'))
        return { result: JSON.stringify({ ok: 1, name: 'Турция', summary: 'Правила', url: 'https://example.org' }) };
      if (command.startsWith('c:'))
        return { result: JSON.stringify({ ok: 1, message: 'Проверка началась' }) };
      return { result: JSON.stringify({
        ...initial, d: 1, total: 1,
        rows: [{ i: 1, c: 'Ереван', n: 'Армения', p: 12000, t: 0 }],
      }) };
    },
  };
  vm.runInNewContext(script, { window: { TelegramWidget }, document });
  return { nodes, calls };
}

(async () => {
  const { nodes, calls } = harness();
  assert.equal(nodes.list.children.length, 2, 'Initial rows');
  nodes.list.children[0].click();
  assert.equal(nodes.detail.children.length, 3, 'Detail content');
  assert.equal(nodes.detail.children[2].children.length, 3, 'Verify / visa / back controls');
  await nodes.detail.children[2].children[0].click();
  assert.ok(calls.includes('c:abc:0:0'), 'Verify fare request');
  await nodes.detail.children[2].children[1].click();
  assert.equal(nodes.detail.children[2].children.length, 2, 'Visa source + back');
  nodes.detail.children[2].children[0].click();
  assert.ok(calls.includes('link:https://example.org'), 'Visa source link');
  nodes.detail.children[2].children[1].click();
  nodes.detail.children[2].children[2].click();
  await nodes.direct.click();
  assert.ok(calls.includes('b:abc:0:1'), 'Direct-only request');
  assert.equal(nodes.list.children.length, 1, 'Filtered rows');
  assert.equal(nodes.list.children[0].children[1].textContent.startsWith('≈'), true, 'Indicative price');
  const forwarded = harness(null);
  assert.equal(forwarded.nodes.list.children.length, 2, 'Forward still has static snapshot');
  await forwarded.nodes.direct.click();
  assert.equal(forwarded.calls.some(s => s.startsWith('b:')), false, 'Forward cannot fetch private results');
  console.log('HTML bubble UI smoke test passed');
})().catch(e => { console.error(e); process.exitCode = 1; });
