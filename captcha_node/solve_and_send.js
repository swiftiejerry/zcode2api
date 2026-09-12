#!/usr/bin/env node
/**
 * 一步式：同一真实 Chrome 会话内，先解阿里云无痕验证，再用同一会话发 /v1/messages。
 * （verifyParam 与浏览器会话/指纹绑定，跨会话使用会 3007 captcha verify failed）
 *
 * 用法: node solve_and_send.js <reqFile>
 * reqFile: {url, headers(除captcha外), body}
 * 输出: STATUS=<code> + 响应体
 */
const fs = require('fs');
const { spawn } = require('child_process');
const http = require('http');
const os = require('os');
const path = require('path');

const reqFile = process.argv[2];
const req = JSON.parse(fs.readFileSync(reqFile, 'utf8'));

const BROWSERS = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
];
const bin = BROWSERS.find(p => fs.existsSync(p));
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const port = 9500 + Math.floor(Math.random() * 500);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'zcode-one-'));
  const chrome = spawn(bin, [
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check',
    '--disable-blink-features=AutomationControlled',
    '--window-size=1280,800',
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
    setTimeout(() => { if (pending.has(mid)) { pending.delete(mid); rej(new Error('timeout ' + method)); } }, 180000);
  });
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.navigate', { url: 'https://zcode.z.ai/' });
  await sleep(3500);

  const script = `
    (async () => {
      // 1. 加载阿里云 SDK
      await new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = 'https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js';
        s.onload = resolve; s.onerror = () => reject(new Error('SDK load failed'));
        document.head.appendChild(s);
        setTimeout(() => reject(new Error('SDK timeout')), 15000);
      });
      const host = document.createElement('div');
      host.id = '__cap'; host.style.cssText='position:fixed;left:10px;top:10px;width:300px;height:200px;';
      document.body.appendChild(host);
      const btn = document.createElement('button'); btn.id='__btn'; document.body.appendChild(btn);

      // 2. 解无痕验证
      const cap = await new Promise((done) => {
        window.initAliyunCaptcha({
          SceneId: '11xygtvd', mode: 'popup', region: 'cn', prefix: 'no8xfe',
          element: '#__cap', button: '#__btn', captchaLogoImg: '', showErrorTip: false,
          getInstance: (inst) => { try { (inst.startTracelessVerification || inst.show).call(inst); } catch (e) {} },
          success: (param) => done({ ok: true, param }),
          fail: () => done({ ok: false, why: 'fail' }),
          onError: (e) => done({ ok: false, why: 'onError:' + JSON.stringify(e) }),
        });
        setTimeout(() => { done({ ok: false, why: 'timeout' }); }, 30000);
      });
      if (!cap.ok) return { captcha: cap };

      // 3. 同一会话立刻发请求
      const headers = ${JSON.stringify(req.headers)};
      headers['X-Aliyun-Captcha-Verify-Param'] = cap.param;
      const r = await fetch(${JSON.stringify(req.url)}, {
        method: 'POST', headers, body: ${JSON.stringify(req.body)},
        credentials: 'include',
      });
      const text = await r.text();
      return { captcha: { ok: true }, status: r.status, text: text.slice(0, 4000) };
    })()
  `;

  const r = await send('Runtime.evaluate', { expression: script, awaitPromise: true, returnByValue: true });
  const v = r?.result?.value;
  const out = JSON.stringify(v);
  fs.writeFileSync(process.env.OUT_FILE || '/tmp/one_resp.json', out);
  console.log('STATUS=' + (v ? (v.status || 'captcha-fail') : '?'));
  if (v && v.text) console.log(v.text.slice(0, 400));
  cleanup(); process.exit(0);

  function getJSON(url) {
    return new Promise((res, rej) => {
      http.get(url, r => { let d = ''; r.on('data', c => d += c);
        r.on('end', () => { try { res(JSON.parse(d)); } catch (e) { rej(e); } }); }).on('error', rej);
    });
  }
})();
