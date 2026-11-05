"""rtp2httpd 状态接口：SSE 状态拉取 + 控制 API。

接口来源（源码 src/status.c、src/connection.c 验证，默认 status-page-path=/status）：
- GET  {base}/status/sse           -> SSE 流，data: {json}
- POST {base}/status/api/reload-config      等效 SIGHUP（热重载配置）
- POST {base}/status/api/restart-workers    等效 SIGUSR1（重启工作进程）
- POST {base}/status/api/clear-logs         清空日志
- POST {base}/status/api/disconnect         body: client_id=xxx（强制断开，并封禁该 IP 5 秒）
- PUT  {base}/status/api/log-level          body: level=0..4（调整日志级别）

SSE JSON 顶层字段：serverStartTime / uptimeMs / version / maxClients /
currentLogLevel / clients[] / totalClients / totalBytesSent /
totalBandwidth / workers[] / logs[]
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client

from .const import (
    API_CLEAR_LOGS,
    API_DISCONNECT,
    API_LOG_LEVEL,
    API_RELOAD_CONFIG,
    API_RESTART_WORKERS,
    LOG_LEVEL_NAME,
    STATUS_SSE_PATH,
)

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = aiohttp.ClientTimeout(total=10)

# ---- 客户端状态枚举（src/status.h）----
_STATE_NAMES: dict[int, str] = {
    0: "连接中",
    1: "FCC 初始化",
    2: "FCC 请求中",
    3: "FCC 单播待命",
    4: "FCC 单播播放",
    5: "FCC 组播请求",
    6: "FCC 组播播放",
    7: "RTSP 初始化",
    8: "RTSP 连接中",
    9: "RTSP 已连接",
    10: "RTSP 发送 OPTIONS",
    11: "RTSP 等待 OPTIONS",
    12: "RTSP 发送 DESCRIBE",
    13: "RTSP 等待 DESCRIBE",
    14: "RTSP 描述完成",
    15: "RTSP 发送 SETUP",
    16: "RTSP 等待 SETUP",
    17: "RTSP SETUP 完成",
    18: "RTSP 发送 PLAY",
    19: "RTSP 等待 PLAY",
    20: "RTSP 播放中",
    21: "RTSP 重连中",
    22: "RTSP 发送 TEARDOWN",
    23: "RTSP 等待 TEARDOWN",
    24: "RTSP 拆流完成",
    25: "RTSP 暂停",
    26: "HTTP 连接中",
    27: "HTTP 发送请求",
    28: "HTTP 等待响应",
    29: "推流中",
    30: "错误",
    31: "已断开",
}


@dataclass
class ClientStatus:
    """一个正在播放的客户端连接。"""

    client_id: str
    addr: str
    service_url: str
    state: int = 0
    duration_ms: int = 0
    bytes_sent: int = 0
    bandwidth: int = 0          # 单位 B/s（rtp2httpd 未公开文档说明，按字节/秒处理）
    queue_bytes: int = 0
    queue_limit: int = 0
    queue_highwater: int = 0
    dropped_bytes: int = 0
    slow: bool = False

    @property
    def state_name(self) -> str:
        return _STATE_NAMES.get(self.state, f"未知({self.state})")

    @property
    def key(self) -> str:
        """会话唯一 key（含端口与序号，每次连接都不同）。"""
        return self.client_id

    @property
    def ip(self) -> str:
        """客户端 IP（去掉端口）。

        实体按 IP 聚合：回看/HLS 每片一个连接、端口每次都变，
        按连接建实体会导致实体不断新建删除。
        """
        addr = (self.addr or "").strip()
        if addr.count(":") == 1:  # IPv4:port
            return addr.split(":", 1)[0]
        return addr or self.client_id

    @property
    def is_http_stream(self) -> bool:
        """是否是 HTTP 反代流（回看/HLS），这类流没有组播地址。"""
        return (self.service_url or "").strip().lower().startswith(
            ("http://", "https://")
        )


@dataclass
class ServerStatus:
    """rtp2httpd 服务整体状态。"""

    online: bool = False
    error: str | None = None
    version: str | None = None
    server_start_time: int | None = None
    uptime_ms: int | None = None
    log_level: int | None = None
    max_clients: int | None = None
    total_clients: int = 0
    total_bytes: int = 0
    total_bandwidth: int = 0
    workers: list[dict] = field(default_factory=list)
    clients: list[ClientStatus] = field(default_factory=list)


def _parse_sse_payload(text: str) -> dict:
    """从 SSE 文本或纯 JSON 中提取状态负载。

    兼容：整段 JSON；单行 `data: {...}`；多行 `data:`（按行拼接还原）。
    """
    if text.lstrip().startswith("{"):
        return json.loads(text)
    data_lines: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                data_lines.append(payload)
    if not data_lines:
        raise ValueError("响应中未找到 SSE data 负载")
    # 先试单行，再试多行拼接
    for candidate in (data_lines[-1], "\n".join(data_lines)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ValueError("SSE data 负载无法解析为 JSON")


def _as_int(value: object, default: int | None = None) -> int | None:
    """安全转 int（rtp2httpd 个别字段可能以字符串返回）；缺失/非法返回 default。"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_status(text: str) -> ServerStatus:
    """解析 /status/sse 返回文本 -> ServerStatus。"""
    data = _parse_sse_payload(text)
    status = ServerStatus(
        online=True,
        version=data.get("version"),
        server_start_time=_as_int(data.get("serverStartTime")),
        uptime_ms=_as_int(data.get("uptimeMs")),
        log_level=_as_int(data.get("currentLogLevel")),
        max_clients=_as_int(data.get("maxClients")),
        total_clients=_as_int(data.get("totalClients"), 0) or 0,
        total_bytes=_as_int(data.get("totalBytesSent"), 0) or 0,
        total_bandwidth=_as_int(data.get("totalBandwidth"), 0) or 0,
        workers=data.get("workers", []),
    )
    for item in data.get("clients", []):
        if not isinstance(item, dict):
            continue
        status.clients.append(
            ClientStatus(
                client_id=str(item.get("clientId", "")),
                addr=str(item.get("clientAddr", "")),
                service_url=str(item.get("serviceUrl", "")),
                state=_as_int(item.get("state"), 0),
                duration_ms=_as_int(item.get("durationMs"), 0),
                bytes_sent=_as_int(item.get("bytesSent"), 0),
                bandwidth=_as_int(item.get("currentBandwidth"), 0),
                queue_bytes=_as_int(item.get("queueBytes"), 0),
                queue_limit=_as_int(item.get("queueLimitBytes"), 0),
                queue_highwater=_as_int(item.get("queueBytesHighwater"), 0),
                dropped_bytes=_as_int(item.get("droppedBytes"), 0),
                slow=bool(item.get("slow", False)),
            )
        )
    return status


def log_level_display(level: int | None) -> str | None:
    """日志级别数值 -> 显示文本。"""
    if level is None:
        return None
    name = LOG_LEVEL_NAME.get(level)
    return f"{name}({level})" if name else f"未知({level})"


def base_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def with_token(url: str, token: str | None) -> str:
    """URL 附加 r2h-token 查询参数。"""
    from .m3u import apply_token

    return apply_token(url, token)


async def fetch_status(
    hass: HomeAssistant, host: str, port: int, token: str | None = None
) -> ServerStatus:
    """拉取服务状态；失败时返回 online=False + error 的 ServerStatus。

    /status/sse 是 SSE 长连接（持续推送心跳、不关闭），不能用 resp.text()
    读完整响应体——那会每次挂到超时。用官方快照参数 snapshot=1 请求快照；
    个别版本忽略该参数仍持续推流，因此读满第一帧（首个空行）即断开。
    """
    url = with_token(f"{base_url(host, port)}/{STATUS_SSE_PATH}?snapshot=1", token)
    try:
        session = aiohttp_client.async_get_clientsession(hass)
        async with session.get(
            url, timeout=_TIMEOUT, headers={"Accept": "text/event-stream"}
        ) as resp:
            if resp.status in (401, 403):
                return ServerStatus(online=False, error="r2h-token 无效或未授权")
            if resp.status != 200:
                return ServerStatus(online=False, error=f"HTTP {resp.status}")
            lines: list[bytes] = []
            # 只收第一帧：遇到"已有 data 行之后的空行"即认为帧结束
            async for line in resp.content:
                if not line.strip():
                    if lines:
                        break
                    continue  # 跳过帧之间的空行/开头空行
                lines.append(line)
                if len(lines) >= 200:  # 保护：最多收 200 行
                    break
            text = b"\n".join(lines).decode("utf-8", "replace")
        return parse_status(text)
    except json.JSONDecodeError as err:
        return ServerStatus(online=False, error=f"响应解析失败: {err}")
    except Exception as err:  # noqa: BLE001 - 状态轮询失败不影响服务本身
        return ServerStatus(online=False, error=str(err))


async def post_api(
    hass: HomeAssistant,
    host: str,
    port: int,
    api_path: str,
    token: str | None = None,
    data: dict[str, str] | None = None,
    method: str = "POST",
) -> bool:
    """调用 rtp2httpd 控制 API；成功返回 True。"""
    url = with_token(f"{base_url(host, port)}/{api_path}", token)
    try:
        session = aiohttp_client.async_get_clientsession(hass)
        kwargs: dict = {"timeout": _TIMEOUT}
        if data:
            kwargs["data"] = data
        async with session.request(method, url, **kwargs) as resp:
            if resp.status not in (200, 204):
                _LOGGER.warning("API %s 返回 HTTP %s", api_path, resp.status)
                return False
            body = await resp.text()
            try:
                result = json.loads(body)
            except json.JSONDecodeError:
                return True
            if result.get("success") is False:
                _LOGGER.warning("API %s 执行失败: %s", api_path, result.get("error"))
                return False
            return True
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("调用 API %s 失败: %s", api_path, err)
        return False


async def reload_config(
    hass: HomeAssistant, host: str, port: int, token: str | None
) -> bool:
    return await post_api(hass, host, port, API_RELOAD_CONFIG, token)


async def restart_workers(
    hass: HomeAssistant, host: str, port: int, token: str | None
) -> bool:
    return await post_api(hass, host, port, API_RESTART_WORKERS, token)


async def clear_logs(
    hass: HomeAssistant, host: str, port: int, token: str | None
) -> bool:
    return await post_api(hass, host, port, API_CLEAR_LOGS, token)


async def disconnect_client(
    hass: HomeAssistant,
    host: str,
    port: int,
    token: str | None,
    client_id: str,
) -> bool:
    return await post_api(
        hass, host, port, API_DISCONNECT, token, data={"client_id": client_id}
    )


async def set_log_level(
    hass: HomeAssistant, host: str, port: int, token: str | None, level: int
) -> bool:
    return await post_api(
        hass, host, port, API_LOG_LEVEL, token, data={"level": str(level)}, method="PUT"
    )
