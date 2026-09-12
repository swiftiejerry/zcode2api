"""运行期配置：环境变量 + 默认值。

所有可调参数集中在此。账号与凭证不在此处，而是持久化到 data/ 目录（见 store.py）。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# 项目根目录
ROOT_DIR = Path(__file__).resolve().parents[1]


def _resolve_path(env_name: str, default: str) -> Path:
    raw = (os.getenv(env_name, default) or default).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path


def _int(env_name: str, default: int) -> int:
    try:
        return int(os.getenv(env_name, str(default)))
    except (TypeError, ValueError):
        return default


# ── 目录 ─────────────────────────────────────────────────────────────────────
DATA_DIR = _resolve_path("ZCODE_DATA_DIR", "data")
# 账号与设置持久化到本地 SQLite（与 grok2api 的 local 后端一致）
DB_PATH = DATA_DIR / "accounts.db"
STATIC_DIR = Path(__file__).resolve().parent / "statics"

# ── 服务 ─────────────────────────────────────────────────────────────────────
PORT = _int("ZCODE_PORT", 3000)
HOST = os.getenv("ZCODE_HOST", "0.0.0.0")

# ── 鉴权 ─────────────────────────────────────────────────────────────────────
# 后台管理密码默认值，首次启动写入 data/accounts.db，之后以数据库（meta 表）为准。
DEFAULT_ADMIN_KEY = os.getenv("ZCODE_ADMIN_KEY", "zcode")

# ── 验证码缓存 ───────────────────────────────────────────────────────────────
CAPTCHA_CACHE_TTL = _int("CAPTCHA_CACHE_TTL", 45_000)          # ms
CAPTCHA_CONFIG_CACHE_TTL = _int("CAPTCHA_CONFIG_CACHE_TTL", 600_000)  # ms

# 验证码求解（无浏览器：Node + jsdom 模拟浏览器环境，运行阿里云无痕 SDK）
NODE_PATH = os.getenv("ZCODE_NODE_PATH", "node")
CAPTCHA_SOLVER_DIR = ROOT_DIR / "captcha_node"
CAPTCHA_SOLVER_JS = CAPTCHA_SOLVER_DIR / "solver.js"
# 真实浏览器求解器（Chrome/Edge + CDP）。jsdom 版会被阿里云设备指纹识别直接 fail，
# 本机有 Chrome/Edge 时优先用它。
CAPTCHA_SOLVER_CHROME_JS = CAPTCHA_SOLVER_DIR / "solver_chrome.js"
CAPTCHA_SOLVE_RETRIES = _int("ZCODE_CAPTCHA_RETRIES", 4)
CAPTCHA_SOLVE_TIMEOUT = _int("ZCODE_CAPTCHA_TIMEOUT", 40)  # 每次求解超时（秒）

# ── 用量监控 ─────────────────────────────────────────────────────────────────
# 后台自动刷新账号额度的间隔（秒）。0 表示关闭后台轮询，仅按需刷新。
QUOTA_REFRESH_INTERVAL = _int("ZCODE_QUOTA_REFRESH_INTERVAL", 60)
# 限流（cooling）冷却时长（秒）
COOLING_SECONDS = _int("ZCODE_COOLING_SECONDS", 300)
# 非流式上游请求的读超时（秒）。流式仍为不限制，以免长回复被截断。
UPSTREAM_READ_TIMEOUT = _int("ZCODE_UPSTREAM_READ_TIMEOUT", 300)

# ── 上游端点 ─────────────────────────────────────────────────────────────────
UPSTREAM = {
    "zai": os.getenv(
        "ZAI_UPSTREAM_URL",
        "https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages",
    ),
    "zai_fallback": os.getenv(
        "ZAI_FALLBACK_URL",
        "https://api.z.ai/api/anthropic/v1/messages",
    ),
    "bigmodel": os.getenv(
        "BIGMODEL_UPSTREAM_URL",
        "https://open.bigmodel.cn/api/anthropic/v1/messages",
    ),
}

# ZCode 计费 / 额度查询端点
ZCODE_BILLING_BASE = "https://zcode.z.ai/api/v1/zcode-plan"

USER_AGENT = os.getenv("UPSTREAM_USER_AGENT", "ZCode/3.11.2")
UPSTREAM_USER_AGENT = USER_AGENT  # 兼容旧引用
APP_VERSION = "2.0.0"

# 伪装成 ZCode 客户端：client/configs 与 billing/balance 都校验 app_version，
# 而 platform 必须是 "{os}-{arch}"（win32-x64），写成 win32 会被上游拒 400。
APP_CLIENT_VERSION = os.getenv("ZCODE_CLIENT_APP_VERSION", "3.11.2")
APP_PLATFORM = os.getenv("ZCODE_CLIENT_PLATFORM", "win32-x64")

# 求解服务：真实浏览器在宿主机上，容器里没有。指向它即可让容器复用宿主机求解能力。
CAPTCHA_SERVICE_URL = (os.getenv("ZCODE_CAPTCHA_SERVICE_URL", "") or "").strip()
CAPTCHA_SERVICE_TIMEOUT = _int("ZCODE_CAPTCHA_SERVICE_TIMEOUT", 70)
CAPTCHA_FALLBACK_CONFIG = {
    "enabled": True,
    "prefix": "no8xfe",
    "region": os.getenv("ZCODE_CAPTCHA_REGION", "cn"),
    "sceneId": "11xygtvd",
}
