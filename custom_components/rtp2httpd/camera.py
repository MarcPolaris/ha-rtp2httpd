"""摄像头实体：每个频道一个，用于 HA 面板内直接看直播。

原理：实体提供 stream_source（rtp2httpd 的 HTTP MPEG-TS 单播地址），
HA 的 stream 组件（依赖项，自动装载）会转封装为 HLS，前端以
`/api/stream/<entity_id>` 播放 —— 与 go2rtc 集成的思路一致。

可选：use_snapshot=True 时，实体缩略图走 rtp2httpd 的 `?snapshot=1`
接口（服务端需配置 ffmpeg-path）。注意 OpenWrt 上 ffmpeg 做截图有
CPU 开销，频道多时建议保持默认关闭。
"""
from __future__ import annotations

import hashlib
import logging

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CHANNELS,
    CONF_USE_SNAPSHOT,
    DEFAULT_USE_SNAPSHOT,
    DOMAIN,
)
from .coordinator import Rtp2httpdCoordinator
from .m3u import Channel, snapshot_url

_LOGGER = logging.getLogger(__name__)
PLATFORM = "camera"


def _uid(entry_id: str, key: str) -> str:
    return f"{entry_id}_cam_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """创建频道摄像头实体。"""
    coordinator: Rtp2httpdCoordinator = hass.data[DOMAIN][entry.entry_id]["channels"]
    if coordinator is None:
        return

    selected = entry.options.get(CONF_CHANNELS)
    channels = coordinator.data or []
    if selected:
        wanted = set(selected)
        channels = [ch for ch in channels if ch.key in wanted]

    entities = [Rtp2httpdCamera(coordinator, entry, ch) for ch in channels]
    async_add_entities(entities)
    coordinator.entities[PLATFORM] = entities
    coordinator.tracked[PLATFORM] = {e._channel.key for e in entities}  # noqa: SLF001

    def _refresh() -> None:
        if not coordinator.data:
            return
        new_keys = {ch.key for ch in coordinator.data}
        reg = async_get_entity_registry(hass)

        for ent in list(coordinator.entities.get(PLATFORM, [])):
            if ent._channel.key not in new_keys:  # noqa: SLF001
                coordinator.tracked[PLATFORM].discard(ent._channel.key)  # noqa: SLF001
                hass.async_create_task(ent.async_remove())
                coordinator.entities[PLATFORM].remove(ent)

        to_add = []
        for ch in coordinator.data:
            if ch.key in coordinator.tracked[PLATFORM]:
                continue
            if reg.async_get_entity_id(PLATFORM, DOMAIN, _uid(entry.entry_id, ch.key)):
                coordinator.tracked[PLATFORM].add(ch.key)
                continue
            to_add.append(Rtp2httpdCamera(coordinator, entry, ch))
        if to_add:
            coordinator.tracked[PLATFORM].update(e._channel.key for e in to_add)  # noqa: SLF001
            async_add_entities(to_add)
            coordinator.entities[PLATFORM].extend(to_add)

    coordinator.async_add_listener(_refresh)


class Rtp2httpdCamera(CoordinatorEntity[Rtp2httpdCoordinator], Camera):
    """rtp2httpd 频道直播摄像头。"""

    _attr_should_poll = False
    _attr_icon = "mdi:television-classic"

    def __init__(
        self,
        coordinator: Rtp2httpdCoordinator,
        entry: ConfigEntry,
        channel: Channel,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._channel = channel
        self._attr_unique_id = _uid(entry.entry_id, channel.key)
        self._attr_name = channel.name

    @property
    def stream_source(self) -> str | None:
        """返回 MPEG-TS over HTTP 单播地址，交给 HA stream 组件。"""
        return self._channel.url

    @property
    def camera_image_type(self) -> str:
        return "image/jpeg"

    async def _handle_coordinator_update(self) -> None:
        new = self.coordinator.get_channel(self._channel.key)
        if new is not None:
            self._channel = new
            self._attr_name = new.name
        await super()._handle_coordinator_update()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """缩略图：use_snapshot 开启时走 rtp2httpd 截图接口，失败静默降级。"""
        if not self._entry.options.get(CONF_USE_SNAPSHOT, DEFAULT_USE_SNAPSHOT):
            return None
        url = snapshot_url(self._channel.url)
        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.read()
        except Exception:  # noqa: BLE001 - 截图失败不影响直播
            return None
