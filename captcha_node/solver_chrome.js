#!/usr/bin/env node
/**
 * 真实浏览器求解阿里云无痕验证。
 *
 * jsdom 版 solver.js 会被阿里云的设备指纹检测识别（fail 回调直接触发），
 * 这里改用本机 Chrome/Edge 的真实内核跑官方 SDK，指纹与正常浏览器一致。
 * 通过 CDP（DevTools Protocol）驱动，不安装 puppeteer。
 *
 * 用法: node solver_chrome.js <sceneId> <region> <prefix>
 * 输出: VERIFY_PARAM=<param>
 */
const { spawn } = require('child_process');
const http = require('http');
const os = require('os');
const fs = require('fs');
const path = require('path');

const SCENE = process.argv[2] || '11xygtvd';
const REGION = process.argv[3] || 'cn';
const PREFIX = process.argv[4] || 'no8xfe';

const BROWSERS = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
];

function findBrowser() {
  for (const p of BROWSERS) if (fs.existsSync(p)) return p;
  return null;
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function main() {
  const bin = findBrowser();
  if (!bin) { console.error('未找到 Chrome/Edge'); process.exit(6); }

  const port = 9200 + Math.floor(Math.random() * 700);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'zcode-cap-'));

  const chrome = spawn(bin, [
    /* visible */
    '--disable-gpu',
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check',
    '--disable-blink-features=AutomationControlled',
    '--window-size=1280,800',
    'about:blank',
  ], { stdio: 'ignore' });

  const cleanup = () => { try { chrome.kill(); } catch {} };
  process.on('exit', cleanup);

  // 等待调试端口就绪
  let list = null;
  for (let i = 0; i < 60; i++) {
    try { list = await getJSON(`http://127.0.0.1:${port}/json/list`); if (list.length) break; } catch {}
    await sleep(250);
  }
  if (!list || !list.length) { console.error('浏览器启动失败'); cleanup(); process.exit(7); }

  const target = list.find(t => t.type === 'page') || list[0];
  const ws = new (require('ws'))(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.on('open', res); ws.on('error', rej); });

  let id = 0;
  const pending = new Map();
  ws.on('message', (raw) => {
    let m; try { m = JSON.parse(raw); } catch { return; }
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  const send = (method, params = {}) => new Promise((res, rej) => {
    const mid = ++id;
    pending.set(mid, (r) => r.error ? rej(new Error(r.error.message)) : res(r.result));
    ws.send(JSON.stringify({ id: mid, method, params }));
    setTimeout(() => { if (pending.has(mid)) { pending.delete(mid); rej(new Error('timeout ' + method)); } }, 45000);
  });

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: 'https://zcode.z.ai/' });
  await sleep(3000);

  // 注入阿里云 SDK 并启动无痕验证
  const script = `
    (async () => {
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
      const btn = document.createElement('button');
      btn.id = '__btn'; document.body.appendChild(btn);

      window.__result = null;
      await new Promise((done) => {
        window.initAliyunCaptcha({
          SceneId: ${JSON.stringify(SCENE)}, mode: 'popup',
          region: ${JSON.stringify(REGION)}, prefix: ${JSON.stringify(PREFIX)},
          element: '#__cap', button: '#__btn', captchaLogoImg: '', showErrorTip: false,
          getInstance: (inst) => { try { (inst.startTracelessVerification || inst.show).call(inst); } catch (e) {} },
          success: (param) => { window.__result = { ok: true, param }; done(); },
          fail: () => { window.__result = { ok: false, why: 'fail' }; done(); },
          onError: (e) => { window.__result = { ok: false, why: 'onError:' + JSON.stringify(e) }; done(); },
        });
        setTimeout(() => { if (!window.__result) { window.__result = { ok: false, why: 'timeout' }; done(); } }, 25000);
      });
      return window.__result;
    })()
  `;

  const r = await send('Runtime.evaluate', {
    expression: script, awaitPromise: true, returnByValue: true,
  });

  const val = r?.result?.value;
  if (val && val.ok && val.param) {
    console.log('VERIFY_PARAM=' + val.param);
    cleanup(); process.exit(0);
  }
  console.error('求解失败: ' + JSON.stringify(val));
  cleanup();
  process.exit(4);
}

function getJSON(url) {
  return new Promise((res, rej) => {
    http.get(url, (r) => {
      let d = ''; r.on('data', c => d += c); r.on('end', () => {
        try { res(JSON.parse(d)); } catch (e) { rej(e); }
      });
    }).on('error', rej);
  });
}

main().catch(e => { console.error('ERR ' + e.message); process.exit(5); });
