"""rtp2httpd 集成入口。

核心能力（对应 /status 页面与 /status/api/* 接口）：
- 服务状态：版本 / 运行时长 / 日志级别 / worker 明细（sensor）
- 实时连接统计：在线客户端数 / 总带宽 / 累计流量（sensor）
- 接入设备：每个客户端动态生成「状态传感器 + 断开按钮」（sensor/button）
- 控制：重载配置 / 重启工作进程 / 清空日志（button）、日志级别（select）
- 可选频道实体：media_player（逻辑播放器）/ camera（面板内看直播，走 stream）

同步策略：协调器刷新后，下线设备自动移除、新设备自动创建；
媒体播放器/摄像头实体需在选项里开启（默认关闭）。
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant

from .config_flow import default_playlist_url, default_status_url
from .const import (
    CONF_CHANNEL_MAP_URL,
    CONF_CREATE_CAMERAS,
    CONF_CREATE_MEDIA_PLAYERS,
    CONF_PLAYLIST_URL,
    CONF_REFRESH_INTERVAL,
    CONF_STATUS_INTERVAL,
    CONF_STATUS_URL,
    DEFAULT_CREATE_CAMERAS,
    DEFAULT_CREATE_MEDIA_PLAYERS,
    DEFAULT_PORT,
    DEFAULT_REFRESH_INTERVAL,
    DEFAULT_STATUS_INTERVAL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import Rtp2httpdCoordinator, StatusCoordinator

_LOGGER = logging.getLogger(__name__)


def _enabled_platforms(entry: ConfigEntry) -> list[str]:
    """根据选项决定装载的平台。"""
    options = entry.options
    platforms: list[str] = ["sensor", "binary_sensor", "button", "switch", "select"]
    if options.get(CONF_CREATE_MEDIA_PLAYERS, DEFAULT_CREATE_MEDIA_PLAYERS):
        platforms.append("media_player")
    if options.get(CONF_CREATE_CAMERAS, DEFAULT_CREATE_CAMERAS):
        platforms.append("camera")
    return [p for p in PLATFORMS if p in platforms]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """装载集成。"""
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)
    token = entry.data.get(CONF_TOKEN) or None
    options = entry.options

    status_url = options.get(
        CONF_STATUS_URL, default_status_url(host, port, token)
    )
    status_interval = options.get(CONF_STATUS_INTERVAL, DEFAULT_STATUS_INTERVAL)

    # 状态协调器（始终创建；首轮失败不阻塞，实体按周期自动重试）
    status_coordinator = StatusCoordinator(
        hass, host, port, status_interval, token
    )
    try:
        await status_coordinator.async_config_entry_first_refresh()
    except Exception as err:  # noqa: BLE001 - 首轮失败继续装载，后台自动重试
        _LOGGER.warning("rtp2httpd 首轮状态刷新失败（后台继续重试）: %s", err)

    # 频道协调器（仅当启用了频道类实体才创建）
    channel_coordinator: Rtp2httpdCoordinator | None = None
    if _channel_platforms_enabled(entry):
        playlist_url = options.get(
            CONF_PLAYLIST_URL, default_playlist_url(host, port, token)
        )
        refresh_interval = options.get(
            CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL
        )
        channel_coordinator = Rtp2httpdCoordinator(
            hass, playlist_url, refresh_interval, token
        )
        await channel_coordinator.async_refresh()

    # 频道名称协调器（把播放中的 /rtp/<组播> 反查成频道名；仅配置了来源才创建）
    channel_names: Rtp2httpdCoordinator | None = None
    map_url = (options.get(CONF_CHANNEL_MAP_URL) or "").strip()
    if map_url:
        channel_names = Rtp2httpdCoordinator(
            hass,
            map_url,
            options.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL),
            token,
        )
        await channel_names.async_refresh()
        if channel_names.last_update_success:
            _LOGGER.info(
                "频道名称来源已加载：%s（%d 个频道）", map_url, len(channel_names.data)
            )
        else:
            _LOGGER.warning(
                "频道名称来源拉取失败（播放状态将只显示组播地址）：%s", map_url
            )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "status": status_coordinator,
        "channels": channel_coordinator,
        "channel_names": channel_names,
    }
    # 选项变更后自动重载（否则新选项要等重启才生效）
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    platforms = _enabled_platforms(entry)
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    _LOGGER.info(
        "rtp2httpd 集成装载完成（%s:%s，token=%s，状态每 %ss 轮询，平台: %s）",
        host,
        port,
        "已配置" if token else "未配置",
        status_interval,
        ",".join(platforms),
    )
    return True


def _channel_platforms_enabled(entry: ConfigEntry) -> bool:
    options = entry.options
    return bool(
        options.get(CONF_CREATE_MEDIA_PLAYERS, DEFAULT_CREATE_MEDIA_PLAYERS)
        or options.get(CONF_CREATE_CAMERAS, DEFAULT_CREATE_CAMERAS)
    )


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """选项保存后重载集成，让新配置立即生效。"""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """卸载集成。"""
    platforms = _enabled_platforms(entry)
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, platforms
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
