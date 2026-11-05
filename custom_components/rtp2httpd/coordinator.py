"""数据协调器：频道列表（可选）与服务状态（必需）。"""
from __future__ import annotations

import logging
from datetime import timedelta

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import DOMAIN
from .m3u import Channel, build_channel_index, parse_playlist
from .status import ServerStatus, fetch_status

_LOGGER = logging.getLogger(__name__)
_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=15)


async def fetch_channels(
    hass: HomeAssistant, playlist_url: str, token: str | None = None
) -> list[Channel]:
    """拉取并解析转换后的频道列表，失败抛异常（由调用方呈现错误）。"""
    session = aiohttp_client.async_get_clientsession(hass)
    async with session.get(playlist_url, timeout=_FETCH_TIMEOUT) as resp:
        if resp.status in (401, 403):
            raise PermissionError(f"HTTP {resp.status}: r2h-token 无效或未授权")
        if resp.status != 200:
            raise ConnectionError(f"HTTP {resp.status}")
        text = await resp.text()
    channels = parse_playlist(text, token)
    if not channels:
        raise ValueError("频道列表为空")
    return channels


class Rtp2httpdCoordinator(DataUpdateCoordinator[list[Channel]]):
    """频道列表协调器：只在启用媒体播放器/摄像头实体时创建。"""

    def __init__(
        self,
        hass: HomeAssistant,
        playlist_url: str,
        refresh_interval: int | None,
        token: str | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_channels",
            update_interval=(
                timedelta(seconds=refresh_interval) if refresh_interval else None
            ),
        )
        self.playlist_url = playlist_url
        self.token = token
        self.data: list[Channel] = []
        self._index: dict[str, Channel] = {}
        # 平台名 -> 已跟踪 key / 已创建实体（刷新时增量增删）
        self.tracked: dict[str, set[str]] = {}
        self.entities: dict[str, list] = {}

    async def _async_update_data(self) -> list[Channel]:
        try:
            channels = await fetch_channels(
                self.hass, self.playlist_url, self.token
            )
        except PermissionError as err:
            raise UpdateFailed(f"鉴权失败: {err}") from err
        except ConnectionError as err:
            raise UpdateFailed(f"无法访问 rtp2httpd: {err}") from err
        except ValueError as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"获取频道列表失败: {err}") from err
        # 顺带刷新「流地址 -> 频道」索引（用于把播放中的组播地址解析成频道名）
        self._index = build_channel_index(channels)
        return channels

    @property
    def index(self) -> dict[str, Channel]:
        """「流地址 / 路径 -> 频道」索引；频道列表刷新后自动更新。"""
        if not self._index and self.data:
            self._index = build_channel_index(self.data)
        return self._index

    def get_channel(self, key: str) -> Channel | None:
        for ch in self.data or []:
            if ch.key == key:
                return ch
        return None


class StatusCoordinator(DataUpdateCoordinator[ServerStatus]):
    """服务状态协调器：轮询 /status/sse，驱动传感器/按钮/客户端实体。

    失败语义（HA 标准做法）：拉取失败时抛 UpdateFailed，DataUpdateCoordinator
    会自动保留上一次成功的数据（self.data 不变、last_update_success=False）。
    实体据此表现：
    - 常规实体 available=False（不可用），恢复后自动回到最新值；
    - 服务状态传感器/二进制传感器覆写 available，永远可见（运行中/离线）。
    """

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        status_interval: int,
        token: str | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_status",
            update_interval=timedelta(seconds=status_interval),
        )
        self.host = host
        self.port = port
        self.token = token
        self.data: ServerStatus = ServerStatus()
        self.last_error: str | None = None
        self.tracked: dict[str, set[str]] = {}
        self.entities: dict[str, list] = {}

    async def _async_update_data(self) -> ServerStatus:
        status = await fetch_status(self.hass, self.host, self.port, self.token)
        if not status.online:
            # 抛 UpdateFailed：保留上次成功数据，实体进入"不可用"而不是"未知"，
            # 同时错误信息会出现在 HA 日志里，便于排查
            self.last_error = status.error or "未知错误"
            _LOGGER.warning(
                "rtp2httpd 状态轮询失败: %s（将每 %ss 重试，期间保留最后已知数据）",
                self.last_error,
                self.update_interval.total_seconds() if self.update_interval else "?",
            )
            raise UpdateFailed(f"rtp2httpd 状态轮询失败: {self.last_error}")
        self.last_error = None
        _LOGGER.debug(
            "状态轮询成功: version=%s clients=%s bandwidth=%s",
            status.version,
            len(status.clients),
            status.total_bandwidth,
        )
        return status

    def get_client(self, key: str) -> object | None:
        for cli in self.data.clients:
            if cli.key == key:
                return cli
        return None

    def get_clients_by_ip(self, ip: str) -> list:
        """某个 IP 当前的全部会话（一个 IP 可能同时有多条流）。"""
        return [cli for cli in self.data.clients if cli.ip == ip]
