#!/usr/bin/env node
/**
 * 求解服务：让容器里的网关调用宿主机上的真实浏览器求解器。
 *
 * Chrome/Edge 只在宿主机上，容器内没有（也不该塞一个浏览器进镜像）。
 * 这个服务监听一个端口，收到请求就跑 solver_chrome.js 并返回 verifyParam。
 *
 * 用法: node captcha_server.js [port]
 * 网关侧设置: ZCODE_CAPTCHA_SERVICE_URL=http://host.docker.internal:8899/solve
 */
const http = require('http');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

const PORT = Number(process.argv[2]) || 8899;
const SOLVER = path.join(__dirname, 'solver_chrome.js');
const TIMEOUT_MS = 60000;

// 同一时刻只跑一个求解进程（阿里云对并发敏感），其余请求排队后复用结果
let inflight = null;

function solve(scene, region, prefix) {
  if (inflight) return inflight;
  inflight = new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    const p = spawn(process.execPath, [SOLVER, scene, region, prefix], {
      cwd: __dirname, stdio: ['ignore', 'pipe', 'pipe'],
    });
    let out = '', err = '';
    p.stdout.on('data', d => out += d);
    p.stderr.on('data', d => err += d);
    const t = setTimeout(() => { try { p.kill(); } catch {} finish({ error: 'timeout' }); }, TIMEOUT_MS);
    p.on('close', (code) => {
      clearTimeout(t);
      const m = out.split('\n').find(l => l.startsWith('VERIFY_PARAM='));
      if (m) finish({ param: m.slice('VERIFY_PARAM='.length).trim() });
      else finish({ error: `exit=${code} ${err.trim().slice(-200)}` });
    });
    p.on('error', e => finish({ error: e.message }));
  }).finally(() => { inflight = null; });
  return inflight;
}

const server = http.createServer((req, res) => {
  const send = (code, obj) => {
    res.writeHead(code, { 'content-type': 'application/json' });
    res.end(JSON.stringify(obj));
  };
  if (req.method !== 'GET' && req.method !== 'POST') return send(405, { error: 'method' });
  const u = new URL(req.url, 'http://x');
  if (u.pathname === '/health') return send(200, { ok: true, solver: fs.existsSync(SOLVER) });
  if (u.pathname !== '/solve') return send(404, { error: 'not found' });
  const scene = u.searchParams.get('scene') || '11xygtvd';
  const region = u.searchParams.get('region') || 'cn';
  const prefix = u.searchParams.get('prefix') || 'no8xfe';
  solve(scene, region, prefix).then(r => send(r.param ? 200 : 500, r));
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`captcha solver service on http://127.0.0.1:${PORT} (solver=${fs.existsSync(SOLVER)})`);
});
