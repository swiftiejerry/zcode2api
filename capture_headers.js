#!/usr/bin/env node
/** 挂在 ZCode(Electron) 的 CDP 上，抓所有发往 zcode.z.ai 的请求头。 */
const http = require('http');
const { URL } = require('url');

const PORT = process.argv[2] || 9222;
const FILTER = /zcode\.z\.ai|bigmodel\.cn|api\.z\.ai/i;

function getJSON(u) {
  return new Promise((res, rej) => {
    http.get(u, r => { let d = ''; r.on('data', c => d += c);
      r.on('end', () => { try { res(JSON.parse(d)); } catch (e) { rej(e); } }); }).on('error', rej);
  });
}

(async () => {
  const targets = await getJSON(`http://127.0.0.1:${PORT}/json/list`);
  const pages = targets;
  console.error(`pages: ${pages.map(p => p.title || p.url).join(' | ').slice(0, 200)}`);

  const WebSocket = require('./captcha_node/node_modules/ws');
  const sockets = [];
  let hitCount = 0;

  for (const t of pages) {
    const ws = new WebSocket(t.webSocketDebuggerUrl);
    await new Promise(r => ws.on('open', r).catch?.(() => {}));
    let id = 0; const pending = new Map();
    ws.on('message', raw => { let m; try { m = JSON.parse(raw); } catch { return; }
      if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } });
    const send = (method, params = {}) => new Promise((res) => {
      const mid = ++id; pending.set(mid, res);
      ws.send(JSON.stringify({ id: mid, method, params }));
    });
    await send('Network.enable');
    ws.on('message', raw => {
      let m; try { m = JSON.parse(raw); } catch { return; }
      if (m.method === 'Network.requestWillBeSent') {
        const u = m.params?.request?.url || '';
        if (FILTER.test(u) && !/\.js|\.css|\.png|\.svg|\.woff/.test(u)) {
          hitCount++;
          const req = m.params.request;
          const out = {
            ts: new Date().toISOString(),
            url: u,
            method: req.method,
            headers: req.headers,
            postData: req.postData ? req.postData.slice(0, 600) : undefined,
          };
          console.log(JSON.stringify(out, null, 1));
          console.log('====');
        }
      }
    });
    sockets.push(ws);
  }
  console.error(`listening on ${pages.length} pages... (Ctrl+C to stop)`);
  setInterval(() => {}, 1 << 30);
})().catch(e => { console.error('ERR', e.message); process.exit(1); });
