"""下拉选择平台：rtp2httpd 日志级别（0=FATAL .. 4=DEBUG）。

选择后立即调用 PUT /status/api/log-level 生效（运行时可调，无需重载）。
"""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LOG_LEVELS, LOG_LEVEL_NAME
from .coordinator import StatusCoordinator
from .status import set_log_level

_LOGGER = logging.getLogger(__name__)
PLATFORM = "select"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: StatusCoordinator = hass.data[DOMAIN][entry.entry_id]["status"]
    async_add_entities([Rtp2httpdLogLevelSelect(coordinator, entry)])
    _LOGGER.info("select 平台装载：日志级别选择器")


class Rtp2httpdLogLevelSelect(
    CoordinatorEntity[StatusCoordinator], SelectEntity
):
    """日志级别选择。"""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_icon = "mdi:form-dropdown"
    _attr_name = "日志级别"
    _attr_options = list(LOG_LEVELS)

    def __init__(
        self, coordinator: StatusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sel_log_level"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data.get("name", DOMAIN),
            manufacturer="rtp2httpd",
            model="IPTV 转发服务器",
        )

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def current_option(self) -> str | None:
        level = self.coordinator.data.log_level
        if level is None:
            return None
        for label, value in LOG_LEVELS.items():
            if value == level:
                return label
        return LOG_LEVEL_NAME.get(level)

    async def async_select_option(self, option: str) -> None:
        level = LOG_LEVELS.get(option)
        if level is None:
            return
        ok = await set_log_level(
            self.hass,
            self.coordinator.host,
            self.coordinator.port,
            self.coordinator.token,
            level,
        )
        if ok:
            await self.coordinator.async_request_refresh()
