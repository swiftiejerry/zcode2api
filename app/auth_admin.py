"""鉴权依赖：后台管理密钥 + 可选的网关 API Key。"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from .store import store


def _as_bytes(value: str | None) -> bytes:
    """编码为 bytes 后再比较。

    hmac.compare_digest 对含非 ASCII 字符的 str 会抛 TypeError，而密钥来自
    用户输入（中文密码很常见）。统一编码为 UTF-8 bytes 后比较，既是时序安全的，
    也不会因为密钥里出现非 ASCII 字符而 500。
    """
    return (value or "").encode("utf-8")


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def verify_admin_key(authorization: str | None = Header(default=None)) -> None:
    """校验后台管理密钥（仅接受 `Authorization: Bearer <key>`）。

    不再支持 `?app_key=` 查询参数：密钥出现在 URL 里会被写进访问日志、浏览器历史
    与 Referer 头。前端只用 Authorization 头，没有依赖该参数。
    """
    key = store.admin_key()
    if not key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未配置后台密钥")

    token = _extract_bearer(authorization)
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少鉴权凭证")
    if not hmac.compare_digest(_as_bytes(token), _as_bytes(key)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "鉴权凭证无效")


async def verify_gateway_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> None:
    """校验 /v1/messages 网关访问密钥（未配置则放行）。"""
    key = store.gateway_key()
    if not key:
        return
    token = _extract_bearer(authorization) or x_api_key
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少 API Key")
    if not hmac.compare_digest(_as_bytes(token), _as_bytes(key)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "API Key 无效")
