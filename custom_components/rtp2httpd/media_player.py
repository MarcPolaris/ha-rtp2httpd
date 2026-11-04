"""媒体播放器实体：每个频道一个（逻辑播放器）。

用途：
- 服务 / 自动化 / Assist 语音控制（play_media、turn_on/off、stop）
- 配合投屏类设备（如 Kodi、Chromecast 等）播放 HTTP 单播流
- 面板内看直播请使用同名的 camera 实体（走 HA stream 组件）

说明：媒体播放器实体本身不渲染画面（HA 前端不对 media_player 做视频
渲染），画面由 camera 实体承载。
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_CATCHUP,
    ATTR_CHANNEL_URL,
    ATTR_GROUP,
    ATTR_LOGO,
    ATTR_TVG_ID,
    CONF_CHANNELS,
    DOMAIN,
)
from .coordinator import Rtp2httpdCoordinator
from .m3u import Channel

_LOGGER = logging.getLogger(__name__)
PLATFORM = "media_player"


def _uid(entry_id: str, key: str) -> str:
    return f"{entry_id}_mp_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """创建频道媒体播放器实体。"""
    coordinator: Rtp2httpdCoordinator = hass.data[DOMAIN][entry.entry_id]["channels"]
    if coordinator is None:
        return

    channels = _selected_channels(coordinator, entry)
    entities = [Rtp2httpdMediaPlayer(coordinator, entry, ch) for ch in channels]
    async_add_entities(entities)
    coordinator.entities[PLATFORM] = entities
    coordinator.tracked[PLATFORM] = {e._channel.key for e in entities}  # noqa: SLF001

    def _refresh() -> None:
        """协调器刷新后：移除下线频道，增量创建新频道。"""
        if not coordinator.data:
            return
        new_keys = {ch.key for ch in coordinator.data}
        reg = async_get_entity_registry(hass)

        # 1) 移除已不存在的频道
        for ent in list(coordinator.entities.get(PLATFORM, [])):
            if ent._channel.key not in new_keys:  # noqa: SLF001
                coordinator.tracked[PLATFORM].discard(ent._channel.key)  # noqa: SLF001
                hass.async_create_task(ent.async_remove())
                coordinator.entities[PLATFORM].remove(ent)

        # 2) 增量创建新频道（查 entity registry 防重复）
        to_add = []
        for ch in coordinator.data:
            if ch.key in coordinator.tracked[PLATFORM]:
                continue
            if reg.async_get_entity_id(PLATFORM, DOMAIN, _uid(entry.entry_id, ch.key)):
                coordinator.tracked[PLATFORM].add(ch.key)
                continue
            to_add.append(Rtp2httpdMediaPlayer(coordinator, entry, ch))
        if to_add:
            coordinator.tracked[PLATFORM].update(e._channel.key for e in to_add)  # noqa: SLF001
            async_add_entities(to_add)
            coordinator.entities[PLATFORM].extend(to_add)

    coordinator.async_add_listener(_refresh)


def _selected_channels(
    coordinator: Rtp2httpdCoordinator, entry: ConfigEntry
) -> list[Channel]:
    """按选项中的频道选择过滤（未选择则全部）。"""
    selected = entry.options.get(CONF_CHANNELS)
    if not selected:
        return list(coordinator.data or [])
    wanted = set(selected)
    return [ch for ch in (coordinator.data or []) if ch.key in wanted]


class Rtp2httpdMediaPlayer(CoordinatorEntity[Rtp2httpdCoordinator], MediaPlayerEntity):
    """rtp2httpd 频道播放器。"""

    _attr_should_poll = False
    _attr_icon = "mdi:television"

    def __init__(
        self,
        coordinator: Rtp2httpdCoordinator,
        entry: ConfigEntry,
        channel: Channel,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._channel = channel
        self._state = MediaPlayerState.IDLE
        self._attr_unique_id = _uid(entry.entry_id, channel.key)
        self._attr_name = channel.name
        self._attr_media_title = channel.name
        self._attr_supported_features = (
            MediaPlayerEntityFeature.PLAY_MEDIA
            | MediaPlayerEntityFeature.TURN_ON
            | MediaPlayerEntityFeature.TURN_OFF
            | MediaPlayerEntityFeature.STOP
        )

    @property
    def state(self) -> MediaPlayerState:
        return self._state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            ATTR_GROUP: self._channel.group,
            ATTR_LOGO: self._channel.logo,
            ATTR_TVG_ID: self._channel.tvg_id,
            ATTR_CHANNEL_URL: self._channel.url,
            ATTR_CATCHUP: self._channel.catchup,
        }

    async def _handle_coordinator_update(self) -> None:
        new = self.coordinator.get_channel(self._channel.key)
        if new is not None:
            self._channel = new
            self._attr_name = new.name
            self._attr_media_title = new.name
        await super()._handle_coordinator_update()

    async def async_turn_on(self) -> None:
        await self.async_play_media("video/mp2t", self._channel.url)

    async def async_turn_off(self) -> None:
        await self.async_media_stop()

    async def async_media_stop(self) -> None:
        self._state = MediaPlayerState.IDLE
        self.async_write_ha_state()

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs: Any
    ) -> None:
        """播放：media_id 可以是任意 http(s) 流地址（默认用频道自身 URL）。"""
        if isinstance(media_id, str) and media_id.startswith(("http://", "https://")):
            self._state = MediaPlayerState.PLAYING
            self._attr_media_content_type = media_type
            self._attr_media_title = self._channel.name
            self.async_write_ha_state()
