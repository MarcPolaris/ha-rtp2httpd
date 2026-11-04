"""开关平台。

静态开关：无（rtp2httpd 没有"启动/停止服务"的 API，服务器级无法做真开关）。

动态开关（随客户端连入/断开自动增删）：
- 每个客户端一个「连接开关」：开=正在传输，关=强制断开
  （POST /status/api/disconnect，会封禁该 IP 5 秒）。
  比断开按钮更直观，也能在仪表盘上直接反映连接状态。
"""
from __future__ import annotations

import hashlib
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import StatusCoordinator
from .registry_utils import purge_client_entities
from .status import ClientStatus, disconnect_client

_LOGGER = logging.getLogger(__name__)
PLATFORM = "switch"


def _client_uid(entry_id: str, client_key: str) -> str:
    digest = hashlib.sha1(client_key.encode("utf-8")).hexdigest()[:10]
    return f"{entry_id}_csw_{digest}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: StatusCoordinator = hass.data[DOMAIN][entry.entry_id]["status"]

    # 清理残留的动态客户端开关注册条目
    purge_client_entities(hass, entry, f"{entry.entry_id}_csw_")

    coordinator.entities[PLATFORM] = []
    coordinator.tracked[PLATFORM] = set()
    _LOGGER.info("switch 平台装载：客户端连接开关（动态）")

    def _refresh() -> None:
        reg = async_get_entity_registry(hass)
        clients = {cli.key: cli for cli in coordinator.data.clients}
        # 断开的客户端开关连同注册条目一起删除，设备页不残留"不可用"
        for ent in list(coordinator.entities[PLATFORM]):
            if ent._client_key not in clients:  # noqa: SLF001
                coordinator.tracked[PLATFORM].discard(ent.unique_id)
                reg.async_remove(ent.entity_id)
                hass.async_create_task(ent.async_remove())
                coordinator.entities[PLATFORM].remove(ent)
        # 新增客户端
        to_add = []
        for key, cli in clients.items():
            uid = _client_uid(entry.entry_id, key)
            if uid in coordinator.tracked[PLATFORM]:
                continue
            if reg.async_get_entity_id(PLATFORM, DOMAIN, uid):
                coordinator.tracked[PLATFORM].add(uid)
                continue
            to_add.append(Rtp2httpdClientSwitch(coordinator, entry, cli))
        if to_add:
            coordinator.tracked[PLATFORM].update(e.unique_id for e in to_add)
            async_add_entities(to_add)
            coordinator.entities[PLATFORM].extend(to_add)

    coordinator.async_add_listener(_refresh)


class Rtp2httpdClientSwitch(CoordinatorEntity[StatusCoordinator], SwitchEntity):
    """客户端连接开关：开=连接中，关=已断开（关闭即强制断开）。"""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: StatusCoordinator,
        entry: ConfigEntry,
        client: ClientStatus,
    ) -> None:
        super().__init__(coordinator)
        self._client_key = client.key
        self._client_id = client.client_id
        self._attr_unique_id = _client_uid(entry.entry_id, client.key)
        self._attr_name = f"连接 {client.addr or client.client_id}"
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
    def is_on(self) -> bool:
        """只要该客户端仍出现在状态列表里就视为开。"""
        return self.coordinator.get_client(self._client_key) is not None

    @property
    def icon(self) -> str:
        return "mdi:lan-connect" if self.is_on else "mdi:lan-disconnect"

    @property
    def extra_state_attributes(self):
        client = self.coordinator.get_client(self._client_key)
        if client is None:
            return {}
        return {
            "client_id": client.client_id,
            "client_addr": client.addr,
            "service_url": client.service_url,
            "state": client.state_name,
        }

    async def async_turn_on(self, **kwargs) -> None:
        # 无法主动"建立"客户端连接（连接由播放器发起），仅刷新状态
        _LOGGER.info("客户端连接由播放器发起，无法主动开启：%s", self._client_id)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        ok = await disconnect_client(
            self.hass,
            self.coordinator.host,
            self.coordinator.port,
            self.coordinator.token,
            self._client_id,
        )
        if ok:
            _LOGGER.info("已通过开关断开客户端 %s", self._client_id)
            await self.coordinator.async_request_refresh()
