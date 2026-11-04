"""按钮平台（一次性控制，等效 Unix 信号/状态页管理操作）。

静态按钮：
- 重载配置（等效 SIGHUP，POST /status/api/reload-config）
- 重启工作进程（等效 SIGUSR1，POST /status/api/restart-workers）
- 清空日志（POST /status/api/clear-logs）

动态按钮（随客户端连入/断开自动增删）：
- 每个客户端一个「断开」按钮（POST /status/api/disconnect，会封禁该 IP 5 秒）
"""
from __future__ import annotations

import hashlib
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import StatusCoordinator
from .registry_utils import purge_client_entities
from .status import (
    ClientStatus,
    clear_logs,
    disconnect_client,
    reload_config,
    restart_workers,
)

_LOGGER = logging.getLogger(__name__)
PLATFORM = "button"


def _uid(entry_id: str, suffix: str) -> str:
    return f"{entry_id}_btn_{suffix}"


def _client_uid(entry_id: str, client_key: str) -> str:
    digest = hashlib.sha1(client_key.encode("utf-8")).hexdigest()[:10]
    return f"{entry_id}_cbtn_{digest}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: StatusCoordinator = hass.data[DOMAIN][entry.entry_id]["status"]

    # 清理残留的动态客户端按钮注册条目
    purge_client_entities(hass, entry, f"{entry.entry_id}_cbtn_")

    static_entities = [
        Rtp2httpdReloadButton(coordinator, entry),
        Rtp2httpdRestartButton(coordinator, entry),
        Rtp2httpdClearLogsButton(coordinator, entry),
    ]
    async_add_entities(static_entities)
    coordinator.entities[PLATFORM] = list(static_entities)
    coordinator.tracked[PLATFORM] = {e.unique_id for e in static_entities}
    _LOGGER.info("button 平台装载：%d 个静态按钮", len(static_entities))

    def _refresh() -> None:
        reg = async_get_entity_registry(hass)
        clients = {cli.key for cli in coordinator.data.clients}
        # 断开的客户端按钮连同注册条目一起删除，设备页不残留"不可用"
        for ent in list(coordinator.entities[PLATFORM]):
            if isinstance(ent, Rtp2httpdClientDisconnectButton) and ent._client_key not in clients:  # noqa: SLF001
                coordinator.tracked[PLATFORM].discard(ent.unique_id)
                reg.async_remove(ent.entity_id)
                hass.async_create_task(ent.async_remove())
                coordinator.entities[PLATFORM].remove(ent)

        to_add = []
        for key in clients:
            uid = _client_uid(entry.entry_id, key)
            if uid in coordinator.tracked[PLATFORM]:
                continue
            if reg.async_get_entity_id(PLATFORM, DOMAIN, uid):
                coordinator.tracked[PLATFORM].add(uid)
                continue
            client = coordinator.get_client(key)
            if client is not None:
                to_add.append(Rtp2httpdClientDisconnectButton(coordinator, entry, client))
        if to_add:
            coordinator.tracked[PLATFORM].update(e.unique_id for e in to_add)
            async_add_entities(to_add)
            coordinator.entities[PLATFORM].extend(to_add)

    coordinator.async_add_listener(_refresh)


class _Rtp2httpdButton(CoordinatorEntity[StatusCoordinator], ButtonEntity):
    """控制按钮公共部分。"""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: StatusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data.get("name", DOMAIN),
            manufacturer="rtp2httpd",
            model="IPTV 转发服务器",
        )

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success


class Rtp2httpdReloadButton(_Rtp2httpdButton):
    """热重载配置（SIGHUP）。"""

    _attr_icon = "mdi:reload"
    _attr_name = "重载配置"

    def __init__(
        self, coordinator: StatusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = _uid(entry.entry_id, "reload")

    async def async_press(self) -> None:
        ok = await reload_config(
            self.hass,
            self.coordinator.host,
            self.coordinator.port,
            self.coordinator.token,
        )
        if ok:
            _LOGGER.info("rtp2httpd 配置已重载")
            await self.coordinator.async_request_refresh()


class Rtp2httpdRestartButton(_Rtp2httpdButton):
    """重启工作进程（SIGUSR1）。"""

    _attr_icon = "mdi:restart"
    _attr_name = "重启工作进程"

    def __init__(
        self, coordinator: StatusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = _uid(entry.entry_id, "restart")

    async def async_press(self) -> None:
        ok = await restart_workers(
            self.hass,
            self.coordinator.host,
            self.coordinator.port,
            self.coordinator.token,
        )
        if ok:
            _LOGGER.info("rtp2httpd 工作进程已重启")
            await self.coordinator.async_request_refresh()


class Rtp2httpdClearLogsButton(_Rtp2httpdButton):
    """清空日志。"""

    _attr_icon = "mdi:broom"
    _attr_name = "清空日志"

    def __init__(
        self, coordinator: StatusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = _uid(entry.entry_id, "clear_logs")

    async def async_press(self) -> None:
        ok = await clear_logs(
            self.hass,
            self.coordinator.host,
            self.coordinator.port,
            self.coordinator.token,
        )
        if ok:
            _LOGGER.info("rtp2httpd 日志已清空")


class Rtp2httpdClientDisconnectButton(_Rtp2httpdButton):
    """强制断开某个客户端（动态创建/删除；同时封禁其 IP 5 秒）。"""

    _attr_icon = "mdi:lan-disconnect"

    def __init__(
        self,
        coordinator: StatusCoordinator,
        entry: ConfigEntry,
        client: ClientStatus,
    ) -> None:
        super().__init__(coordinator, entry)
        self._client_key = client.key
        self._client_id = client.client_id
        self._addr = client.addr
        self._attr_unique_id = _client_uid(entry.entry_id, client.key)
        self._attr_name = f"断开 {client.addr or client.client_id}"

    async def async_press(self) -> None:
        ok = await disconnect_client(
            self.hass,
            self.coordinator.host,
            self.coordinator.port,
            self.coordinator.token,
            self._client_id,
        )
        if ok:
            _LOGGER.info("已请求断开客户端 %s", self._client_id)
            await self.coordinator.async_request_refresh()
