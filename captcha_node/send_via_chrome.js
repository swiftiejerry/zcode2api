#!/usr/bin/env node
/** 用真实 Chrome 发请求（TLS/HTTP2 指纹与正常浏览器一致，绕 WAF 的异常客户端检测）。
 *  用法: node send_via_chrome.js <req.json> <out.txt>
 *  req.json: {url, headers:{...}, body}
 */
const fs = require('fs');
const { spawn } = require('child_process');
const http = require('http');
const os = require('os');
const path = require('path');

const [reqFile, outFile] = process.argv.slice(2);
const req = JSON.parse(fs.readFileSync(reqFile, 'utf8'));

const BROWSERS = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
];
const bin = BROWSERS.find(p => fs.existsSync(p));
if (!bin) { console.error('no browser'); process.exit(6); }

const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const port = 9400 + Math.floor(Math.random() * 500);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'zcode-send-'));
  const chrome = spawn(bin, [
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check',
    '--disable-blink-features=AutomationControlled',
    'about:blank',
  ], { stdio: 'ignore' });
  const cleanup = () => { try { chrome.kill(); } catch {} };
  process.on('exit', cleanup);

  let list = null;
  for (let i = 0; i < 60; i++) {
    try { list = await getJSON(`http://127.0.0.1:${port}/json/list`); if (list && list.length) break; } catch {}
    await sleep(250);
  }
  const target = (list || []).find(t => t.type === 'page') || (list || [])[0];
  if (!target) { console.error('no page'); process.exit(7); }

  const ws = new (require('ws'))(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.on('open', res); ws.on('error', rej); });
  let id = 0; const pending = new Map();
  ws.on('message', raw => { let m; try { m = JSON.parse(raw); } catch { return; }
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } });
  const send = (method, params = {}) => new Promise((res, rej) => {
    const mid = ++id; pending.set(mid, r => r.error ? rej(new Error(r.error.message)) : res(r.result));
    ws.send(JSON.stringify({ id: mid, method, params }));
    setTimeout(() => { if (pending.has(mid)) { pending.delete(mid); rej(new Error('timeout ' + method)); } }, 120000);
  });
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.navigate', { url: 'https://zcode.z.ai/' });
  await sleep(3500);

  const script = `
    (async () => {
      const r = await fetch(${JSON.stringify(req.url)}, {
        method: 'POST',
        headers: ${JSON.stringify(req.headers)},
        body: ${JSON.stringify(req.body)},
        credentials: 'include',
      });
      const text = await r.text();
      return { status: r.status, text: text.slice(0, 4000) };
    })()
  `;
  const r = await send('Runtime.evaluate', { expression: script, awaitPromise: true, returnByValue: true });
  const v = r?.result?.value;
  fs.writeFileSync(outFile, JSON.stringify(v));
  console.log('STATUS=' + (v ? v.status : '?'));
  if (v && v.text) console.log(v.text.slice(0, 300));
  cleanup(); process.exit(0);

  function getJSON(url) {
    return new Promise((res, rej) => {
      http.get(url, r => { let d = ''; r.on('data', c => d += c);
        r.on('end', () => { try { res(JSON.parse(d)); } catch (e) { rej(e); } }); }).on('error', rej);
    });
  }
})();
