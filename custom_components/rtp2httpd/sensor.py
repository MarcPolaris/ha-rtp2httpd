"""传感器平台。

静态传感器：
- 服务状态（运行中/离线，永远可用；属性含版本/运行时长/worker 明细等）
- 运行时长 / 最大客户端数 / 工作进程数
- 在线客户端数 / 当前总带宽 / 累计发送字节

动态传感器（随客户端连入/断开自动增删）：
- 每个客户端两个：连接状态（属性含频道/组播地址/时长/流量/队列/丢包）、正在播放的频道

不再提供全局「当前播放频道」：多客户端时无法用一个值准确表达，
各客户端的频道看「客户端 <IP> 频道」即可。

注意：实体类必须继承 SensorEntity（此前豆包版本基类漏掉了它，
导致实体加入 sensor 平台后 state 恒为 None → HA 显示「未知」）。
状态值统一在 _handle_coordinator_update 里计算并写入 _attr_native_value
（HA 标准写法）。

可用性语义：协调器轮询失败时保留最后一次成功数据（last_update_success=False），
常规传感器进入「不可用」；「服务状态」传感器覆写 available 永远可见，
离线时显示「离线」而不是消失/未知。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_BANDWIDTH,
    ATTR_BYTES_SENT,
    ATTR_CHANNEL,
    ATTR_CHANNEL_GROUP,
    ATTR_CHANNEL_LOGO,
    ATTR_CHANNEL_TVG_ID,
    ATTR_CLIENT_ADDR,
    ATTR_CLIENT_ID,
    ATTR_DROPPED_BYTES,
    ATTR_DURATION,
    ATTR_DURATION_SECONDS,
    ATTR_LOG_LEVEL,
    ATTR_MAX_CLIENTS,
    ATTR_MULTICAST,
    ATTR_PLAYING_SECONDS,
    ATTR_QUEUE_BYTES,
    ATTR_QUEUE_HIGHWATER,
    ATTR_QUEUE_LIMIT,
    ATTR_SERVICE_URL,
    ATTR_SLOW,
    ATTR_STARTED,
    ATTR_STATE_CODE,
    ATTR_TOTAL_BANDWIDTH,
    ATTR_TOTAL_BYTES,
    ATTR_TOTAL_CLIENTS,
    ATTR_UPTIME,
    ATTR_VERSION,
    ATTR_WORKERS,
    DOMAIN,
)
from .coordinator import StatusCoordinator
from .m3u import Channel, lookup_channel, stream_key
from .registry_utils import purge_client_entities
from .status import ClientStatus, ServerStatus, log_level_display

_LOGGER = logging.getLogger(__name__)
PLATFORM = "sensor"

_STATE_ICONS = {
    "推流中": "mdi:television-play",
    "FCC 单播播放": "mdi:lightning-bolt",
    "FCC 组播播放": "mdi:lightning-bolt",
    "RTSP 播放中": "mdi:play-circle-outline",
    "连接中": "mdi:lan-connect",
    "错误": "mdi:alert-circle-outline",
}


def _uid(entry_id: str, suffix: str) -> str:
    return f"{entry_id}_sen_{suffix}"


def _client_uid(entry_id: str, client_key: str) -> str:
    digest = hashlib.sha1(client_key.encode("utf-8")).hexdigest()[:10]
    return f"{entry_id}_cli_{digest}"


def _fmt_uptime(uptime_ms: int | None) -> str | None:
    if uptime_ms is None:
        return None
    try:
        seconds = int(uptime_ms) // 1000
    except (TypeError, ValueError):
        return None
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{days}天 {hours:02d}:{minutes:02d}:{secs:02d}"


def _fmt_uptime_short(uptime_ms: int | None) -> str | None:
    """运行时长的短格式：12h 24m 56s（小时累计，不拆天）。"""
    if uptime_ms is None:
        return None
    try:
        seconds = int(uptime_ms) // 1000
    except (TypeError, ValueError):
        return None
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}h {minutes}m {secs}s"


def _fmt_started(server_start_time: int | None) -> str | None:
    """安全格式化启动时间（兼容毫秒/秒时间戳，容错字符串）。"""
    if not server_start_time:
        return None
    try:
        ts = int(server_start_time)
    except (TypeError, ValueError):
        return None
    # rtp2httpd 的 serverStartTime 是毫秒时间戳；若数值明显是秒级则原样用
    seconds = ts / 1000 if ts > 10_000_000_000 else ts
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _fmt_bytes(value: int | None) -> str:
    if value is None:
        return "-"
    value = int(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value} B"
        value /= 1024
    return f"{value:.1f} TB"


def _fmt_duration(duration_ms: int | None) -> str | None:
    """毫秒 -> 12h 24m 56s（与运行时长同格式）。"""
    if not duration_ms:
        return "0s"
    try:
        seconds = int(duration_ms) // 1000
    except (TypeError, ValueError):
        return None
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _channel_index(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Channel]:
    """「流地址 / 路径 -> 频道」索引；未配置「频道名称来源」时为空。"""
    names = (hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}).get(
        "channel_names"
    )
    return names.index if names is not None else {}


def _channel_label(index: dict[str, Channel], service_url: str) -> tuple[str, Channel | None]:
    """播放地址 -> (可读名称, 频道对象)。查不到频道时退化为组播地址。"""
    channel = lookup_channel(index, service_url)
    if channel is not None:
        return channel.name, channel
    multicast = stream_key(service_url)
    if multicast:
        return multicast, None
    cleaned = (service_url or "").split("?", 1)[0] or "未知"
    return cleaned, None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: StatusCoordinator = hass.data[DOMAIN][entry.entry_id]["status"]

    # 清理残留的动态客户端实体注册条目（断开过的客户端重启后不再显示"不可用"）
    purge_client_entities(hass, entry, f"{entry.entry_id}_cli_")

    # 静态传感器（「服务在线」二进制传感器在 binary_sensor.py 平台）
    static_entities = [
        Rtp2httpdServiceSensor(coordinator, entry),
        Rtp2httpdUptimeSensor(coordinator, entry),
        Rtp2httpdMaxClientsSensor(coordinator, entry),
        Rtp2httpdWorkerCountSensor(coordinator, entry),
        Rtp2httpdClientsSensor(coordinator, entry),
        Rtp2httpdBandwidthSensor(coordinator, entry),
        Rtp2httpdBytesSensor(coordinator, entry),
    ]
    async_add_entities(static_entities)
    coordinator.entities[PLATFORM] = list(static_entities)
    coordinator.tracked[PLATFORM] = {e.unique_id for e in static_entities}
    _LOGGER.info("sensor 平台装载：%d 个静态传感器", len(static_entities))

    def _refresh() -> None:
        """客户端连入/断开时增量增删动态传感器（断开即连注册条目一起清理）。"""
        reg = async_get_entity_registry(hass)
        clients = {cli.key: cli for cli in coordinator.data.clients}
        # 移除已断开的客户端：必须同时删除注册表条目，否则设备页残留"不可用"
        for ent in list(coordinator.entities[PLATFORM]):
            if getattr(ent, "_client_key", None) is None:
                continue
            if ent._client_key not in clients:  # noqa: SLF001
                coordinator.tracked[PLATFORM].discard(ent.unique_id)
                reg.async_remove(ent.entity_id)
                hass.async_create_task(ent.async_remove())
                coordinator.entities[PLATFORM].remove(ent)
        # 新增客户端：状态传感器 + 频道传感器
        to_add = []
        for key, cli in clients.items():
            base_uid = _client_uid(entry.entry_id, key)
            for uid, factory in (
                (base_uid, Rtp2httpdClientSensor),
                (f"{base_uid}_ch", Rtp2httpdClientChannelSensor),
            ):
                if uid in coordinator.tracked[PLATFORM]:
                    continue
                if reg.async_get_entity_id(PLATFORM, DOMAIN, uid):
                    coordinator.tracked[PLATFORM].add(uid)
                    continue
                to_add.append(factory(coordinator, entry, cli))
        if to_add:
            coordinator.tracked[PLATFORM].update(e.unique_id for e in to_add)
            async_add_entities(to_add)
            coordinator.entities[PLATFORM].extend(to_add)

    coordinator.async_add_listener(_refresh)

    # ---- 自检：启动 40s 后输出状态机中各传感器的真实状态（一次性） ----
    async def _selfcheck() -> None:
        await asyncio.sleep(40)
        for ent in coordinator.entities.get(PLATFORM, []):
            st = hass.states.get(ent.entity_id)
            _LOGGER.info(
                "自检 %s | native_value=%s | 状态机=%s",
                ent.entity_id,
                ent._attr_native_value,  # noqa: SLF001
                st.state if st else "MISSING",
            )

    hass.async_create_task(_selfcheck())


class _Rtp2httpdStatusEntity(CoordinatorEntity[StatusCoordinator], SensorEntity):
    """状态类传感器公共部分。

    注意必须继承 SensorEntity：此前基类只有 CoordinatorEntity，实体加入
    sensor 平台后 Entity.state 返回 None，状态永远显示「未知」。
    状态值统一在 _handle_coordinator_update 里计算并写入 _attr_native_value
    （HA 标准写法，兼容 HA 2026.10+ 的 state 计算路径）。
    """

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
            sw_version=coordinator.data.version,
        )
        # 初次赋值（实体加入状态机时即有正确状态）
        self._attr_native_value = self._compute_native_value()

    def _compute_native_value(self) -> Any:
        """由子类实现：根据当前协调器数据计算状态值。"""
        raise NotImplementedError

    @callback
    def _handle_coordinator_update(self) -> None:
        """协调器数据更新时重算状态值。"""
        self._attr_native_value = self._compute_native_value()
        super()._handle_coordinator_update()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # 加入状态机后立刻刷新一次，避免用陈旧的初始化值
        self._attr_native_value = self._compute_native_value()

    @property
    def available(self) -> bool:
        # 轮询失败时协调器保留最后成功数据；只要最近一次轮询成功即视为可用。
        # （数据非实时时由「服务状态」传感器呈现离线，这里不需要全部变不可用）
        return self.coordinator.last_update_success


class Rtp2httpdServiceSensor(_Rtp2httpdStatusEntity):
    """服务运行状态：运行中/离线。永远可用，避免服务失联时显示「未知」。"""

    _attr_icon = "mdi:server-network"

    def __init__(
        self, coordinator: StatusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = _uid(entry.entry_id, "service")
        self._attr_name = "服务状态"

    def _compute_native_value(self) -> str:
        return "运行中" if self.coordinator.last_update_success else "离线"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data: ServerStatus = self.coordinator.data
        attrs: dict[str, Any] = {
            ATTR_VERSION: data.version,
            ATTR_LOG_LEVEL: log_level_display(data.log_level),
            ATTR_MAX_CLIENTS: data.max_clients,
            ATTR_UPTIME: _fmt_uptime_short(data.uptime_ms),
            ATTR_TOTAL_CLIENTS: data.total_clients,
            ATTR_TOTAL_BYTES: data.total_bytes,
            ATTR_TOTAL_BANDWIDTH: data.total_bandwidth,
        }
        if data.server_start_time:
            attrs[ATTR_STARTED] = _fmt_started(data.server_start_time)
        if data.workers:
            attrs[ATTR_WORKERS] = [
                {
                    "id": w.get("id") if isinstance(w, dict) else None,
                    "pid": w.get("pid") if isinstance(w, dict) else None,
                    "activeClients": (
                        w.get("activeClients") if isinstance(w, dict) else None
                    ),
                    "totalBandwidth": (
                        w.get("totalBandwidth") if isinstance(w, dict) else None
                    ),
                }
                for w in data.workers
            ]
        if self.coordinator.last_error:
            attrs["error"] = self.coordinator.last_error
        return attrs


class Rtp2httpdUptimeSensor(_Rtp2httpdStatusEntity):
    """服务运行时长（显示格式：12h 24m 56s）。"""

    _attr_icon = "mdi:timer-outline"

    def __init__(
        self, coordinator: StatusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = _uid(entry.entry_id, "uptime")
        self._attr_name = "运行时长"

    def _compute_native_value(self) -> str | None:
        return _fmt_uptime_short(self.coordinator.data.uptime_ms)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        uptime_ms = self.coordinator.data.uptime_ms
        seconds = None
        if uptime_ms is not None:
            try:
                seconds = int(uptime_ms) // 1000
            except (TypeError, ValueError):
                seconds = None
        return {
            "seconds": seconds,
            "display": _fmt_uptime(uptime_ms),
        }


class Rtp2httpdMaxClientsSensor(_Rtp2httpdStatusEntity):
    """最大允许客户端连接数（配置值）。"""

    _attr_icon = "mdi:account-multiple-outline"
    _attr_native_unit_of_measurement = "个"
    _attr_state_class = "measurement"

    def __init__(
        self, coordinator: StatusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = _uid(entry.entry_id, "max_clients")
        self._attr_name = "最大客户端数"

    def _compute_native_value(self) -> int | None:
        return self.coordinator.data.max_clients


class Rtp2httpdWorkerCountSensor(_Rtp2httpdStatusEntity):
    """工作进程数量。"""

    _attr_icon = "mdi:chip"
    _attr_native_unit_of_measurement = "个"
    _attr_state_class = "measurement"

    def __init__(
        self, coordinator: StatusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = _uid(entry.entry_id, "workers")
        self._attr_name = "工作进程数"

    def _compute_native_value(self) -> int:
        return len(self.coordinator.data.workers)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        for w in self.coordinator.data.workers:
            if not isinstance(w, dict):
                continue
            wid = w.get("id")
            attrs[f"worker_{wid}"] = {
                "pid": w.get("pid"),
                "activeClients": w.get("activeClients"),
                "totalBandwidth": w.get("totalBandwidth"),
                "totalBytes": w.get("totalBytes"),
                "pool_utilization": (w.get("pool") or {}).get("utilization"),
                "send_eagain": (w.get("send") or {}).get("eagain"),
            }
        return attrs


class Rtp2httpdClientsSensor(_Rtp2httpdStatusEntity):
    """在线客户端（正在播放的流会话）数量。"""

    _attr_icon = "mdi:lan"
    _attr_state_class = "measurement"

    def __init__(
        self, coordinator: StatusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = _uid(entry.entry_id, "clients")
        self._attr_name = "在线客户端数"

    def _compute_native_value(self) -> int:
        return self.coordinator.data.total_clients


class Rtp2httpdBandwidthSensor(_Rtp2httpdStatusEntity):
    """当前总推流带宽（Mbps，网络标准口径：Mbit/s）。"""

    _attr_icon = "mdi:speedometer"
    _attr_native_unit_of_measurement = "Mbps"
    _attr_state_class = "measurement"
    _attr_suggested_display_precision = 2

    def __init__(
        self, coordinator: StatusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = _uid(entry.entry_id, "bandwidth")
        self._attr_name = "当前总带宽"

    def _compute_native_value(self) -> float:
        # 字节/秒 → 兆比特/秒：×8 ÷ 1,000,000
        return round((self.coordinator.data.total_bandwidth or 0) * 8 / 1_000_000, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data: ServerStatus = self.coordinator.data
        return {
            "bandwidth_mbit_s": round(data.total_bandwidth * 8 / 1_000_000, 2),
            "bandwidth_display": _fmt_bytes(data.total_bandwidth) + "/s",
        }


class Rtp2httpdBytesSensor(_Rtp2httpdStatusEntity):
    """累计流量（GB）。"""

    _attr_icon = "mdi:database-arrow-down"
    _attr_native_unit_of_measurement = "GB"
    _attr_state_class = "total_increasing"

    def __init__(
        self, coordinator: StatusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = _uid(entry.entry_id, "bytes")
        self._attr_name = "累计流量"

    def _compute_native_value(self) -> float | None:
        total = self.coordinator.data.total_bytes
        if total is None:
            return None
        try:
            return round(int(total) / 1024**3, 2)
        except (TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"display": _fmt_bytes(self.coordinator.data.total_bytes)}


class Rtp2httpdClientSensor(_Rtp2httpdStatusEntity):
    """单个客户端连接状态（动态创建/删除）。"""

    def __init__(
        self,
        coordinator: StatusCoordinator,
        entry: ConfigEntry,
        client: ClientStatus,
    ) -> None:
        self._client_key = client.key
        super().__init__(coordinator, entry)
        self._attr_unique_id = _client_uid(entry.entry_id, client.key)
        self._attr_name = f"客户端 {client.addr or client.client_id}"

    def _compute_native_value(self) -> str:
        client = self._get_client()
        return client.state_name if client else "已断开"

    @property
    def icon(self) -> str:
        client = self._get_client()
        if client is None:
            return "mdi:lan-disconnect"
        return _STATE_ICONS.get(client.state_name, "mdi:lan-connect")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        client = self._get_client()
        if client is None:
            return {}
        label, channel = _channel_label(
            _channel_index(self.coordinator.hass, self._entry), client.service_url
        )
        attrs: dict[str, Any] = {
            ATTR_CHANNEL: label,
            ATTR_MULTICAST: stream_key(client.service_url),
            ATTR_CLIENT_ID: client.client_id,
            ATTR_CLIENT_ADDR: client.addr,
            ATTR_SERVICE_URL: client.service_url,
            ATTR_STATE_CODE: client.state,
            ATTR_DURATION: _fmt_duration(client.duration_ms),
            ATTR_DURATION_SECONDS: (client.duration_ms or 0) // 1000,
            ATTR_BYTES_SENT: client.bytes_sent,
            ATTR_BANDWIDTH: client.bandwidth,
            "bandwidth_display": _fmt_bytes(client.bandwidth) + "/s",
            ATTR_QUEUE_BYTES: client.queue_bytes,
            ATTR_QUEUE_LIMIT: client.queue_limit,
            ATTR_QUEUE_HIGHWATER: client.queue_highwater,
            ATTR_DROPPED_BYTES: client.dropped_bytes,
            ATTR_SLOW: client.slow,
        }
        if channel is not None:
            attrs[ATTR_CHANNEL_GROUP] = channel.group
            attrs[ATTR_CHANNEL_LOGO] = channel.logo
            attrs[ATTR_CHANNEL_TVG_ID] = channel.tvg_id
        return attrs

    def _get_client(self) -> ClientStatus | None:
        return self.coordinator.get_client(self._client_key)


class Rtp2httpdClientChannelSensor(_Rtp2httpdStatusEntity):
    """单个客户端正在播放的频道（随客户端连入/断开自动增删）。"""

    _attr_icon = "mdi:television-guide"

    def __init__(
        self,
        coordinator: StatusCoordinator,
        entry: ConfigEntry,
        client: ClientStatus,
    ) -> None:
        self._client_key = client.key
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{_client_uid(entry.entry_id, client.key)}_ch"
        self._attr_name = f"客户端 {client.addr or client.client_id} 频道"

    def _get_client(self) -> ClientStatus | None:
        return self.coordinator.get_client(self._client_key)

    def _compute_native_value(self) -> str:
        client = self._get_client()
        if client is None:
            return "已断开"
        label, _ = _channel_label(
            _channel_index(self.coordinator.hass, self._entry), client.service_url
        )
        return label

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        client = self._get_client()
        if client is None:
            return {}
        channel = lookup_channel(
            _channel_index(self.coordinator.hass, self._entry), client.service_url
        )
        attrs: dict[str, Any] = {
            ATTR_CLIENT_ADDR: client.addr,
            ATTR_MULTICAST: stream_key(client.service_url),
            ATTR_SERVICE_URL: client.service_url,
            ATTR_DURATION: _fmt_duration(client.duration_ms),
            ATTR_PLAYING_SECONDS: (client.duration_ms or 0) // 1000,
        }
        if channel is not None:
            attrs.update(
                {
                    ATTR_CHANNEL: channel.name,
                    ATTR_CHANNEL_GROUP: channel.group,
                    ATTR_CHANNEL_LOGO: channel.logo,
                    ATTR_CHANNEL_TVG_ID: channel.tvg_id,
                }
            )
        return attrs
