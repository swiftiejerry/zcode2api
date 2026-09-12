"""验证码求解（无浏览器）。

通过 Node + jsdom 在模拟浏览器环境中运行阿里云无痕 SDK，
求得 verifyParam（X-Aliyun-Captcha-Verify-Param）。不再启动真实浏览器。

- 缓存：求得的 verifyParam 在 TTL 内复用
- 并发：同一时刻只跑一个求解进程，其余请求等待后命中缓存
- 重试：单次求解偶发失败时自动重试
"""

from __future__ import annotations

import asyncio
import time

import httpx

from . import logs, settings


class CaptchaManager:
    def __init__(self) -> None:
        self._cached: str | None = None
        self._cached_at: float = 0.0
        self._lock = asyncio.Lock()
        self._config_cache: dict | None = None
        self._config_cache_at: float = 0.0

    # ── 配置 ─────────────────────────────────────────────────────────────────
    async def fetch_config(self) -> dict:
        now = time.time() * 1000
        if self._config_cache and now - self._config_cache_at < settings.CAPTCHA_CONFIG_CACHE_TTL:
            return self._config_cache
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.get(
                    "https://zcode.z.ai/api/v1/client/configs"
                    "?app_version=3.0.0&platform=win32"
                )
            res.raise_for_status()
            captcha = ((res.json().get("data") or {}).get("configs") or {}).get("captcha")
            if captcha:
                self._config_cache = captcha
                self._config_cache_at = now
                return captcha
        except (httpx.HTTPError, ValueError) as err:
            logs.warn("captcha", f"获取配置失败，使用默认: {err}")
        return {"enabled": True, "prefix": "no8xfe", "region": "sgp", "sceneId": "11xygtvd"}

    # ── 求解 ─────────────────────────────────────────────────────────────────
    async def get_verify_param(self, port: int | None = None) -> str:
        now = time.time() * 1000
        if self._cached and now - self._cached_at < settings.CAPTCHA_CACHE_TTL:
            return self._cached

        async with self._lock:
            # 二次检查：等锁期间可能已被其他请求填充
            if self._cached and time.time() * 1000 - self._cached_at < settings.CAPTCHA_CACHE_TTL:
                return self._cached

            config = await self.fetch_config()
            param = await self._solve(config)
            self._cached = param
            self._cached_at = time.time() * 1000
            return param

    async def _solve(self, config: dict) -> str:
        scene = config.get("sceneId") or "11xygtvd"
        region = config.get("region") or "sgp"
        prefix = config.get("prefix") or "no8xfe"

        last_err: str | None = None
        for attempt in range(1, settings.CAPTCHA_SOLVE_RETRIES + 1):
            try:
                param = await self._run_solver(scene, region, prefix)
            except Exception as err:  # noqa: BLE001
                last_err = str(err)
                param = None
            if param:
                if attempt > 1:
                    logs.ok("captcha", f"求解成功（第 {attempt} 次尝试）")
                return param
            logs.warn("captcha", f"第 {attempt}/{settings.CAPTCHA_SOLVE_RETRIES} 次求解未果，重试…")

        raise RuntimeError(f"验证码求解失败: {last_err or '多次重试无结果'}")

    async def _run_solver(self, scene: str, region: str, prefix: str) -> str | None:
        if not settings.CAPTCHA_SOLVER_JS.exists():
            raise RuntimeError(
                f"未找到求解器 {settings.CAPTCHA_SOLVER_JS}，请先在 captcha_node 下执行 npm install"
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                settings.NODE_PATH, str(settings.CAPTCHA_SOLVER_JS), scene, region, prefix,
                cwd=str(settings.CAPTCHA_SOLVER_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as err:
            # 进程创建本身就会抛 FileNotFoundError，必须在 try 内；否则这个分支永远进不来，
            # 只会拿到底层的 "[Errno 2] No such file or directory" 并被无意义地重试 4 次。
            raise RuntimeError(f"无法启动 Node（{settings.NODE_PATH}）: {err}") from err
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=settings.CAPTCHA_SOLVE_TIMEOUT)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return None

        for line in stdout.decode("utf-8", "ignore").splitlines():
            if line.startswith("VERIFY_PARAM="):
                return line[len("VERIFY_PARAM="):].strip()

        # 没有 VERIFY_PARAM 时把 stderr / 退出码带出来，否则 jsdom 缺失之类的
        # 真实原因（solver.js 里 require('jsdom') 失败）会被完全吞掉，
        # 使用者只看到一句「求解失败」，无从排查。
        detail = (stderr or b"").decode("utf-8", "ignore").strip()
        if detail:
            raise RuntimeError(f"求解器无输出（exit={proc.returncode}）: {detail[-400:]}")
        raise RuntimeError(f"求解器无输出（exit={proc.returncode}）")

    def invalidate(self) -> None:
        self._cached = None
        self._cached_at = 0.0

    async def close(self) -> None:
        pass


captcha_manager = CaptchaManager()
