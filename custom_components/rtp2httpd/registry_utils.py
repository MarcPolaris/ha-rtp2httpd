"""实体注册表清理工具。

动态客户端实体（传感器/按钮/开关）的 unique_id 由 client_id 哈希确定，
客户端断开后实体被移除，但注册表条目会残留（重启后显示"不可用"）。
平台装载时按 unique_id 前缀清掉残留条目：客户端再连入时会以相同
unique_id / entity_id 重新注册，历史统计不丢。
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_registry import (
    RegistryEntry,
    async_get as async_get_entity_registry,
)

_LOGGER = logging.getLogger(__name__)


def purge_client_entities(hass: HomeAssistant, entry: ConfigEntry, uid_prefix: str) -> int:
    """删除本 entry 下 unique_id 以 uid_prefix 开头的注册表条目。

    仅匹配动态客户端实体（_cli_/_cbtn_/_csw_ 前缀），静态实体不受影响。
    返回删除条数。
    """
    reg = async_get_entity_registry(hass)
    removed = 0
    entries: list[RegistryEntry] = [
        e for e in reg.entities.values()
        if e.config_entry_id == entry.entry_id
        and e.unique_id.startswith(uid_prefix)
    ]
    for ent in entries:
        reg.async_remove(ent.entity_id)
        removed += 1
    if removed:
        _LOGGER.info("清理 %d 个残留的 %s 实体注册条目", removed, uid_prefix)
    return removed
