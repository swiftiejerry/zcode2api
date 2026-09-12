"""核心网关：兼容 Anthropic Messages 协议的 /v1/messages。

实现多账号轮询 + 额度用完自动换号 + 阿里无痕验证自动续期。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import logs, settings
from ..agent import build_request
from ..auth_admin import verify_gateway_key
from ..captcha import captcha_manager
from ..models import Account, Status
from ..quota import fetch_quota
from ..store import store

router = APIRouter()

MAX_CAPTCHA_RETRIES = 3
MAX_ACCOUNT_ATTEMPTS = 5

# Z.AI 上游模型名（大小写敏感）。以 ZCode 客户端实际在用的名字为准：
# 日志 / config.json 里只有 glm-5.3 与 glm-5.3-flash 两个真实模型。
MODEL_NAME_MAP = {
    "glm-5.3": "GLM-5.3",
    "glm-5.3-flash": "GLM-5.3-Flash",
    "glm-5.2": "GLM-5.2",
    "glm-5-turbo": "GLM-5-Turbo",
    "glm-turbo": "GLM-5-Turbo",
    "glm-5.1": "GLM-5.1",
    "glm-4.7": "GLM-4.7",
}
# Claude 客户端（Claude Code / Claude Desktop）直接发来的家族名兜底映射，
# 避免 cc-switch 没配 routes 时把 claude-* 原样甩给 GLM 上游而 400。
CLAUDE_FAMILY_ALIASES = {
    "claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-4-5",
    "claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-4-5-20251001",
    "claude-3-5-sonnet", "claude-3-7-sonnet",
}
DEFAULT_UPSTREAM_MODEL = "GLM-5.3"

# /v1/models 对外公布的可用模型（与上游实际存在的模型一致）
AVAILABLE_MODELS = ["GLM-5.3", "GLM-5.3-Flash"]

# 命中以下信号则认为账号额度用完。
# 只用足够具体的短语：单看 "quota"/"balance" 这类词会把模型名、无关报错
# （例如 "failed to read user balance history"）也算成额度耗尽，误封健康账号。
_EXHAUST_KEYWORDS = (
    "insufficient balance",
    "insufficient_quota",
    "insufficient quota",
    "quota exceeded",
    "quota_exhausted",
    "no quota",
    "out of quota",
    "balance is insufficient",
    "余额不足",
    "额度不足",
    "额度已用完",
    "额度用完",
)


def _detect_provider(body: dict, headers) -> str:
    model = body.get("model") or ""
    if model.startswith("bigmodel/") or headers.get("x-provider") == "bigmodel":
        return "bigmodel"
    return "zai"


def _normalize_body(body: dict) -> dict:
    model = body.get("model")
    if isinstance(model, str) and "/" in model:
        model = "/".join(model.split("/")[1:])
    if isinstance(model, str):
        mapped = MODEL_NAME_MAP.get(model.lower())
        if mapped is None and model.lower() in CLAUDE_FAMILY_ALIASES:
            # Claude 客户端没被路由改写时，兜底成默认上游模型，避免把 claude-* 甩给 GLM 上游。
            mapped = DEFAULT_UPSTREAM_MODEL
        body["model"] = mapped or model

    messages = body.get("messages")
    if isinstance(messages, list):
        bridged = []
        for msg in messages:
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                bridged.append({**msg, "content": [{"type": "text", "text": msg["content"]}]})
            else:
                bridged.append(msg)
        body["messages"] = bridged
    return body


def _is_captcha_error(text: str) -> bool:
    low = text.lower()
    return "captcha" in low or "verify token" in low or "verify failed" in low


def _is_exhausted(status_code: int, text: str) -> bool:
    if status_code in (402,):
        return True
    low = text.lower()
    return any(k in low for k in _EXHAUST_KEYWORDS)


def _mark(account: Account, status_value: str, error: str | None = None) -> None:
    account.status = status_value
    account.last_error = error
    if status_value == Status.COOLING:
        account.cooling_until = time.time() + settings.COOLING_SECONDS
    store.update_account(account)


def _last_user_text(body: dict) -> str:
    for msg in reversed(body.get("messages") or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    return part.get("text", "")
    return ""


@router.get("/v1/models", dependencies=[Depends(verify_gateway_key)])
async def list_models():
    """列出可用模型（Anthropic /v1/models 风格）。"""
    return {
        "object": "list",
        "data": [
            {"id": i, "type": "model", "display_name": i, "created_at": "2025-01-01T00:00:00Z"}
            for i in AVAILABLE_MODELS
        ],
    }


@router.post("/v1/messages", dependencies=[Depends(verify_gateway_key)])
async def messages(request: Request):
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": {"message": "请求体不是合法 JSON", "type": "invalid_request"}}, status_code=400)

    # /v1/messages 的 body 必须是 JSON 对象。数组 / 字符串 / 数字 / null 都算非法请求，
    # 不能带着它继续往下走——后面 body.get() 会抛 AttributeError 变成 500。
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": {"message": "请求体必须是 JSON 对象", "type": "invalid_request"}},
            status_code=400,
        )
    if "messages" in body and not isinstance(body["messages"], list):
        return JSONResponse(
            {"error": {"message": "messages 必须是数组", "type": "invalid_request"}},
            status_code=400,
        )

    incoming_headers = dict(request.headers)
    provider = _detect_provider(body, request.headers)
    body = _normalize_body(body)
    # 验证码页面由本服务托管，端口取实际请求端口（兼容任意启动端口）
    port = request.url.port or settings.PORT
    payload = json.dumps(body).encode("utf-8")

    req_id = secrets.token_hex(3)
    logs.req(req_id, str(body.get("model") or "-"), bool(body.get("stream")), _last_user_text(body))

    tried: set[str] = set()
    is_stream = bool(body.get("stream"))

    for _ in range(MAX_ACCOUNT_ATTEMPTS):
        account = store.select(provider, skip_ids=tried)
        if account is None:
            break
        tried.add(account.id)
        needs_captcha = provider == "zai" and account.mode == "jwt"

        result = await _try_account(req_id, account, body, payload, incoming_headers, port,
                                    needs_captcha, is_stream)
        if result is _NEXT_ACCOUNT:
            continue
        return result

    logs.req_err(req_id, "无可用账号 / 额度均已耗尽")
    return JSONResponse(
        {"error": {"message": "所有账号均不可用或额度已用完，请在后台检查账号状态", "type": "no_available_account"}},
        status_code=503,
    )


_NEXT_ACCOUNT = object()

# 事件循环只持有 task 的弱引用，不保留引用的话任务可能在跑完前被 GC 掉（额度刷新静默丢失）。
# 另外并发请求会对同一个账号各起一个刷新任务，这里按 account.id 去重，避免连接数随并发量放大。
_refresh_tasks: dict[str, asyncio.Task] = {}


def _schedule_refresh(account: Account) -> None:
    """后台刷新账号额度：按账号去重，并持有任务引用直到完成。"""
    existing = _refresh_tasks.get(account.id)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(_safe_refresh(account))
    _refresh_tasks[account.id] = task

    def _done(_t: asyncio.Task, _id: str = account.id) -> None:
        if _refresh_tasks.get(_id) is _t:
            _refresh_tasks.pop(_id, None)

    task.add_done_callback(_done)


async def _try_account(req_id, account, body, payload, incoming_headers, port, needs_captcha, is_stream=False):
    """尝试用单个账号转发，含验证码续期。返回 Response 或 _NEXT_ACCOUNT。"""
    for attempt in range(MAX_CAPTCHA_RETRIES):
        verify_param = None
        if needs_captcha:
            try:
                verify_param = await captcha_manager.get_verify_param(port)
            except Exception as err:  # noqa: BLE001
                # 求解器/Node 挂了是账号级问题（只在 jwt 账号上需要验证码），
                # 不该把整个请求判 500——API Key 账号或其它账号仍然可用，交给上层换号。
                _mark(account, Status.COOLING, f"人机校验不可用: {err}")
                logs.warn(req_id, f"账号 {account.name} 人机校验不可用，切换下一个")
                return _NEXT_ACCOUNT

        try:
            url, headers = build_request(account, body, verify_param, incoming_headers)
        except RuntimeError as err:
            _mark(account, Status.INVALID, str(err))
            logs.warn(req_id, f"账号 {account.name} 凭证无效，切换下一个")
            return _NEXT_ACCOUNT

        # 流式请求要长时间保持连接，read=None 是必需的；但非流式请求如果也 read=None，
        # 上游只建连不返包时会永远挂住（客户端一直等、连接和协程都不释放）。
        # 非流式给一个有限的读超时，超时后走下面的 httpx.HTTPError 分支冷却换号。
        read_timeout = None if is_stream else settings.UPSTREAM_READ_TIMEOUT
        client = httpx.AsyncClient(timeout=httpx.Timeout(connect=30.0, read=read_timeout, write=120.0, pool=30.0))
        cm = client.stream("POST", url, headers=headers, content=payload)
        try:
            resp = await cm.__aenter__()
        except asyncio.CancelledError:
            # 下游断开时 uvicorn 会取消本协程；CancelledError 是 BaseException，
            # 不会被下面的 httpx.HTTPError 接住，必须自己确保连接池被关掉。
            await client.aclose()
            raise
        except httpx.HTTPError as err:
            await client.aclose()
            _mark(account, Status.COOLING, f"连接失败: {err}")
            logs.warn(req_id, f"账号 {account.name} 连接失败，切换下一个")
            return _NEXT_ACCOUNT
        except BaseException:
            # 其它非 HTTPError 异常同样会跳过清理，这里兜底关闭，避免上游 socket 泄漏。
            await client.aclose()
            raise

        status_code = resp.status_code

        if status_code >= 400:
            try:
                text = (await resp.aread()).decode("utf-8", "ignore")
            except asyncio.CancelledError:
                await cm.__aexit__(None, None, None)
                await client.aclose()
                raise
            except httpx.HTTPError:
                # 上游在错误体中途断连（Content-Length 不符等）会在这里抛，
                # 它不是 __aenter__ 那次捕获的范围，以前会直接泄漏 client。
                await cm.__aexit__(None, None, None)
                await client.aclose()
                _mark(account, Status.COOLING, "读取上游错误响应失败")
                logs.warn(req_id, f"账号 {account.name} 读取上游响应失败，切换下一个")
                return _NEXT_ACCOUNT
            await cm.__aexit__(None, None, None)
            await client.aclose()

            if status_code == 403 and _is_captcha_error(text) and needs_captcha:
                captcha_manager.invalidate()
                logs.warn(req_id, f"账号 {account.name} 验证码失效，刷新重试")
                continue  # 同账号重试验证码

            if _is_exhausted(status_code, text):
                _mark(account, Status.EXHAUSTED, "额度已用完")
                logs.warn(req_id, f"账号 {account.name} 额度用完，切换下一个")
                _schedule_refresh(account)
                return _NEXT_ACCOUNT

            if status_code in (401, 403):
                _mark(account, Status.INVALID, f"鉴权失败 HTTP {status_code}")
                logs.warn(req_id, f"账号 {account.name} 鉴权失败 {status_code}，切换下一个")
                return _NEXT_ACCOUNT

            if status_code == 429:
                _mark(account, Status.COOLING, "上游限流 429")
                logs.warn(req_id, f"账号 {account.name} 被限流 429，切换下一个")
                return _NEXT_ACCOUNT

            # 其它错误：直接回传客户端
            account.fail_count += 1
            store.update_account(account)
            logs.req_err(req_id, f"上游错误 HTTP {status_code}（账号 {account.name}）")
            return JSONResponse(
                _safe_json(text) or {"error": {"message": text[:500], "type": "upstream_error"}},
                status_code=status_code,
            )

        # 成功：记录用量并流式透传
        account.use_count += 1
        account.last_used_at = time.time()
        if account.status in (Status.COOLING, Status.EXHAUSTED):
            account.status = Status.ACTIVE
        store.update_account(account)
        _schedule_refresh(account)

        content_type = resp.headers.get("content-type", "application/json")

        async def _body_iter():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
                logs.req_ok(req_id)
            except Exception as err:  # noqa: BLE001
                logs.req_err(req_id, f"流传输中断: {err}")
            finally:
                await cm.__aexit__(None, None, None)
                await client.aclose()

        out_headers = {"Cache-Control": "no-cache"}
        return StreamingResponse(_body_iter(), status_code=status_code,
                                 media_type=content_type, headers=out_headers)

    # 验证码连续失败
    logs.warn(req_id, f"账号 {account.name} 验证码连续失败，切换下一个")
    return _NEXT_ACCOUNT


def _safe_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


async def _safe_refresh(account: Account) -> None:
    try:
        if account.provider == "zai" and account.mode == "jwt":
            await fetch_quota(account)
    except Exception:  # noqa: BLE001
        pass
