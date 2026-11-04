"""二进制传感器平台。

- 服务在线（connectivity）：轮询正常=开，失联=关。永远可用，
  用于自动化判断 rtp2httpd 是否存活。
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import StatusCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: StatusCoordinator = hass.data[DOMAIN][entry.entry_id]["status"]
    async_add_entities([Rtp2httpdOnlineBinarySensor(coordinator, entry)])
    _LOGGER.info("binary_sensor 平台装载：服务在线")


class Rtp2httpdOnlineBinarySensor(
    CoordinatorEntity[StatusCoordinator], BinarySensorEntity
):
    """服务在线状态。"""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "服务在线"

    def __init__(
        self, coordinator: StatusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_bin_online"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data.get("name", DOMAIN),
            manufacturer="rtp2httpd",
            model="IPTV 转发服务器",
        )

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        if self.coordinator.last_error:
            attrs["error"] = self.coordinator.last_error
        data = self.coordinator.data
        if data is not None and data.version:
            attrs["version"] = data.version
        return attrs
