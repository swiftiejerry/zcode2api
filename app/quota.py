"""ZCode 额度 / 余额 / 用量查询，以及账号状态判定。

在查询基础上提供「额度用完自动标记 exhausted」的监控能力。
"""

from __future__ import annotations

import asyncio
import time

import httpx

from . import logs, settings
from .models import Account, Status
from .store import store


def _auth_headers(account: Account) -> dict:
    headers = {"Content-Type": "application/json"}
    if account.mode == "jwt" and account.jwt_token:
        headers["Authorization"] = f"Bearer {account.jwt_token}"
    elif account.api_key:
        headers["x-api-key"] = account.api_key
    return headers


def _units(value):
    """把上游的额度数字转成可比较的数值；非数值（字符串、None、对象）返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


async def fetch_quota(account: Account) -> dict:
    """拉取单个账号的 方案 / 余额 / 用量，写回账号状态并持久化。

    返回结构: {"billing":..., "balance":..., "usage":..., "error":...}
    """
    headers = _auth_headers(account)
    base = settings.ZCODE_BILLING_BASE
    result: dict = {}

    async with httpx.AsyncClient(timeout=20) as client:
        # billing/balance 必须带 app_version，否则上游返回 400 parameter error。
        params = {"app_version": settings.APP_CLIENT_VERSION}

        async def _get(path: str):
            try:
                return await client.get(f"{base}{path}", headers=headers, params=params)
            except httpx.HTTPError:
                return None

        billing_res, balance_res, usage_res = await asyncio.gather(
            _get("/billing/current"),
            _get("/billing/balance"),
            _get("/usage"),
        )

    now = time.time()
    account.last_checked_at = now

    # 鉴权失败 → 标记 invalid
    if billing_res is not None and billing_res.status_code in (401, 403):
        body = (billing_res.text or "").lower()
        if "captcha" not in body and "verify" not in body:
            account.status = Status.INVALID
            account.last_error = f"鉴权失败 HTTP {billing_res.status_code}"
            store.update_account(account)
            return {"error": account.last_error}

    if billing_res is not None and billing_res.status_code == 200:
        try:
            data = billing_res.json()
            result["billing"] = data
            plans = (data.get("data") or {}).get("plans") or []
            account.plan = plans[0] if plans else {}
        except (ValueError, KeyError):
            pass

    quota_map: dict = {}
    if balance_res is not None and balance_res.status_code == 200:
        try:
            data = balance_res.json()
            result["balance"] = data
            for bal in (data.get("data") or {}).get("balances") or []:
                if not isinstance(bal, dict):
                    continue
                # 同一个 show_name 会有多个桶（例如 GLM-5.3-Flash 来自 Global Build /
                # Weekend Build / Start Plan 三个方案），用 show_name 当 key 会互相覆盖，
                # 把大额的一次性池子在 UI 上抹掉。改用 bucket_id 保证一桶一项，
                # 并保留 show_name / priority 供展示与排序。
                bucket_id = bal.get("bucket_id") or bal.get("entitlement_id") or bal.get("show_name")
                name = bal.get("show_name") or bal.get("model") or "model"
                quota_map[str(bucket_id)] = {
                    "name": name,
                    "plan_id": bal.get("plan_id"),
                    "priority": bal.get("priority"),
                    "capabilities": bal.get("capabilities") or [],
                    "total": _units(bal.get("total_units")),
                    "used": _units(bal.get("used_units")),
                    "remaining": _units(bal.get("remaining_units")),
                    "expires_at": bal.get("expires_at"),
                }
        except (ValueError, KeyError):
            pass

    if usage_res is not None and usage_res.status_code == 200:
        try:
            account.usage = usage_res.json().get("data") or {}
            result["usage"] = account.usage
        except (ValueError, KeyError):
            pass

    if quota_map:
        account.quota = quota_map
        # 额度用完判定：把所有「未过期且剩余 > 0」的桶视为可用额度。
        # 以前是 all(remaining <= 0)，在多桶（同模型多方案）下会把已过期桶的
        # 剩余额度也算进来，导致明明还有一个可用池子却被误判为耗尽并摘出轮询。
        now_ts = time.time()
        usable = [
            q for q in quota_map.values()
            if (q.get("remaining") or 0) > 0
            and (q.get("expires_at") is None or q["expires_at"] > now_ts)
        ]
        checked = [q for q in quota_map.values() if q.get("remaining") is not None]
        if checked and not usable:
            account.status = Status.EXHAUSTED
            account.last_error = "额度已用完"
        elif account.status == Status.EXHAUSTED:
            # 只有 exhausted 才由额度刷新恢复。COOLING 有冷却窗口，不能被一次额度查询
            # 提前解锁（否则 429 刚标记完，下一轮刷新就放回池子继续被限流）；
            # INVALID 是凭证失效，只能靠改凭证 / 手动重新启用来恢复。
            account.status = Status.ACTIVE
            account.last_error = None
            account.cooling_until = None

    store.update_account(account)
    return result or {"error": "无法获取额度数据"}


async def refresh_accounts(accounts: list[Account]) -> dict:
    """并发刷新一批账号，返回汇总。"""
    if not accounts:
        return {"ok": 0, "fail": 0}
    sem = asyncio.Semaphore(8)

    async def _one(acc: Account) -> bool:
        async with sem:
            res = await fetch_quota(acc)
            return "error" not in res

    results = await asyncio.gather(*[_one(a) for a in accounts], return_exceptions=True)
    ok = sum(1 for r in results if r is True)
    return {"ok": ok, "fail": len(accounts) - ok}


class QuotaMonitor:
    """后台周期性刷新可管理账号的额度，实现实时用量监控。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def _loop(self) -> None:
        # 启动后先等几秒，避免与服务启动争抢
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass

        while not self._stop.is_set():
            interval = store.quota_refresh_interval()  # 实时读取设置，改后即生效
            if interval > 0:
                try:
                    accounts = [
                        a for a in store.list_accounts("zai")
                        if a.mode == "jwt" and a.status != Status.DISABLED
                    ]
                    if accounts:
                        await refresh_accounts(accounts)
                except Exception as err:  # noqa: BLE001 - 后台任务需吞掉异常继续运行
                    logs.err("quota", f"后台刷新出错: {err}")
            # interval<=0 视为关闭：仍周期性回看设置，便于随时启用
            wait = interval if interval > 0 else 30
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                continue

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            # 直接取消：只设 stop 事件的话，若此刻正卡在 refresh_accounts 里，
            # 关闭流程要等整轮上游请求（每个 20s 超时）跑完才返回。
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


monitor = QuotaMonitor()
