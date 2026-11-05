"""配置流程：首次添加 + 选项编辑。

流程设计：
- step_user        -> 连接参数（名称/地址/端口/token）
- step_channels    -> 频道多选（默认全选；仅当启用频道实体时有用）
- options.init     -> 播放列表/状态地址、刷新周期、实体开关
- options.select   -> 重新拉取频道列表并重选（保存后自动 reload 生效）
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_CHANNELS,
    CONF_CHANNEL_MAP_URL,
    CONF_CREATE_CAMERAS,
    CONF_CREATE_MEDIA_PLAYERS,
    CONF_PLAYLIST_URL,
    CONF_REFRESH_INTERVAL,
    CONF_STATUS_INTERVAL,
    CONF_STATUS_URL,
    CONF_USE_SNAPSHOT,
    DEFAULT_CREATE_CAMERAS,
    DEFAULT_CREATE_MEDIA_PLAYERS,
    DEFAULT_PORT,
    DEFAULT_REFRESH_INTERVAL,
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_USE_SNAPSHOT,
    DOMAIN,
    PLAYLIST_PATH,
    STATUS_SSE_PATH,
)
from .coordinator import fetch_channels
from .m3u import Channel, apply_token

_LOGGER = logging.getLogger(__name__)


def default_playlist_url(
    host: str, port: int, token: str | None = None
) -> str:
    """rtp2httpd 转换后播放列表地址。"""
    return apply_token(f"http://{host}:{port}/{PLAYLIST_PATH}", token)


def default_status_url(
    host: str, port: int, token: str | None = None
) -> str:
    """rtp2httpd 状态 SSE 地址（status-page-path 默认 /status）。"""
    return apply_token(f"http://{host}:{port}/{STATUS_SSE_PATH}", token)


def _channel_options(channels: list[Channel]) -> dict[str, str]:
    """{key: 显示名}，供多选框使用；同名频道附加序号以区分。"""
    seen: dict[str, int] = {}
    options: dict[str, str] = {}
    for ch in channels:
        name = ch.name
        if name in seen:
            seen[name] += 1
            label = f"{name}（源 {seen[name]}）"
        else:
            seen[name] = 0
            label = name
        options[ch.key] = label
    return options


class Rtp2httpdConfigFlow(ConfigFlow, domain=DOMAIN):
    """首次添加集成。"""

    VERSION = 1

    def __init__(self) -> None:
        self._found: dict[str, Channel] = {}
        self._user_input: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input.get(CONF_PORT, DEFAULT_PORT)
            token = user_input.get(CONF_TOKEN) or None
            playlist_url = default_playlist_url(host, port, token)
            try:
                channels = await fetch_channels(self.hass, playlist_url, token)
            except PermissionError:
                errors["base"] = "invalid_token"
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("连接 rtp2httpd 失败: %s", err)
                errors["base"] = "cannot_connect"
            else:
                self._found = {ch.key: ch for ch in channels}
                self._user_input = user_input
                return await self.async_step_channels()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default="rtp2httpd"): str,
                vol.Required(CONF_HOST): str,
                vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Optional(CONF_TOKEN, default=""): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_channels(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if user_input is not None:
            selected = user_input.get(CONF_CHANNELS, list(self._found))
            return self.async_create_entry(
                title=self._user_input[CONF_NAME],
                data=self._user_input,
                options={
                    CONF_CHANNELS: list(selected),
                    CONF_REFRESH_INTERVAL: DEFAULT_REFRESH_INTERVAL,
                    CONF_STATUS_INTERVAL: DEFAULT_STATUS_INTERVAL,
                    CONF_CREATE_MEDIA_PLAYERS: DEFAULT_CREATE_MEDIA_PLAYERS,
                    CONF_CREATE_CAMERAS: DEFAULT_CREATE_CAMERAS,
                    CONF_USE_SNAPSHOT: DEFAULT_USE_SNAPSHOT,
                },
            )
        options = _channel_options(list(self._found.values()))
        schema = vol.Schema(
            {
                vol.Required(CONF_CHANNELS, default=list(options)): cv.multi_select(
                    options
                )
            }
        )
        return self.async_show_form(
            step_id="channels",
            data_schema=schema,
            description_placeholders={
                "count": str(len(options)),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """注册选项编辑流程。"""
        return Rtp2httpdOptionsFlow(config_entry)


class Rtp2httpdOptionsFlow(OptionsFlow):
    """编辑集成选项。"""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._found: dict[str, Channel] = {}
        self._pending: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        host = self._entry.data[CONF_HOST]
        port = self._entry.data.get(CONF_PORT, DEFAULT_PORT)
        token = self._entry.data.get(CONF_TOKEN) or None
        default_p_url = default_playlist_url(host, port, token)
        default_s_url = default_status_url(host, port, token)

        if user_input is not None:
            want_reselect = user_input.pop("reselect_channels", False)
            self._pending = {**self._entry.options, **user_input}
            if want_reselect:
                playlist_url = user_input.get(CONF_PLAYLIST_URL) or default_p_url
                errors: dict[str, str] = {}
                try:
                    channels = await fetch_channels(self.hass, playlist_url, token)
                except PermissionError:
                    errors["base"] = "invalid_token"
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("连接 rtp2httpd 失败: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    self._found = {ch.key: ch for ch in channels}
                    return await self.async_step_select_channels(errors=errors)
                return await self._show_init(
                    default_p_url, default_s_url, errors, user_input=user_input
                )
            return self.async_create_entry(data=self._pending)

        return await self._show_init(default_p_url, default_s_url, {})

    async def _show_init(
        self,
        default_p_url: str,
        default_s_url: str,
        errors: dict[str, str],
        user_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        options = self._entry.options
        val = lambda key, default: (  # noqa: E731
            user_input.get(key) if user_input else options.get(key, default)
        )
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_PLAYLIST_URL,
                    default=val(CONF_PLAYLIST_URL, default_p_url),
                ): str,
                vol.Optional(
                    CONF_STATUS_URL,
                    default=val(CONF_STATUS_URL, default_s_url),
                ): str,
                vol.Optional(
                    CONF_REFRESH_INTERVAL,
                    default=val(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL),
                ): cv.positive_int,
                vol.Optional(
                    CONF_STATUS_INTERVAL,
                    default=val(CONF_STATUS_INTERVAL, DEFAULT_STATUS_INTERVAL),
                ): cv.positive_int,
                vol.Optional(
                    CONF_CREATE_MEDIA_PLAYERS,
                    default=val(
                        CONF_CREATE_MEDIA_PLAYERS, DEFAULT_CREATE_MEDIA_PLAYERS
                    ),
                ): bool,
                vol.Optional(
                    CONF_CREATE_CAMERAS,
                    default=val(CONF_CREATE_CAMERAS, DEFAULT_CREATE_CAMERAS),
                ): bool,
                vol.Optional(
                    CONF_USE_SNAPSHOT,
                    default=val(CONF_USE_SNAPSHOT, DEFAULT_USE_SNAPSHOT),
                ): bool,
                vol.Optional(
                    CONF_CHANNEL_MAP_URL,
                    default=val(CONF_CHANNEL_MAP_URL, ""),
                ): str,
                vol.Optional("reselect_channels", default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_select_channels(
        self,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if user_input is not None:
            self._pending[CONF_CHANNELS] = list(
                user_input.get(CONF_CHANNELS, list(self._found))
            )
            return self.async_create_entry(data=self._pending)

        options = _channel_options(list(self._found.values()))
        current = self._entry.options.get(CONF_CHANNELS, [])
        current = [k for k in current if k in options]
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CHANNELS, default=current or list(options)
                ): cv.multi_select(options)
            }
        )
        return self.async_show_form(
            step_id="select_channels",
            data_schema=schema,
            errors=errors or {},
            description_placeholders={"count": str(len(options))},
        )
