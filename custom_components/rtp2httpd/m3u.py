"""M3U 频道列表解析与 URL 工具。

rtp2httpd 的 `http://host:port/playlist.m3u` 会返回"转换后"的播放列表：
- 频道 URL 已被重写为 rtp2httpd 自己的地址（隐藏上游组播 IP / 鉴权信息）
- 频道名取自 #EXTINF 行最后一个逗号之后
- catchup-source 的 {utc...} 占位符会被保留（由播放端填充）

本模块只做解析 + 可选 token 注入，不做组播地址重写。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
_URL_PREFIXES = ("rtp://", "udp://", "rtsp://", "http://", "https://")


@dataclass
class Channel:
    """一个频道（一个 #EXTINF + 一个 URL 行）。"""

    name: str
    url: str
    group: str | None = None
    logo: str | None = None
    tvg_id: str | None = None
    catchup: str | None = None

    @property
    def key(self) -> str:
        """实体唯一标识用的稳定 key（名称 + URL）。"""
        return f"{self.name}|||{self.url}"


def _strip_label(url: str) -> str:
    """去掉 rtp2httpd 转换列表里 URL 末尾的 `$label`（清晰度标签）。"""
    return url.split("$", 1)[0].strip()


def append_query(url: str, query: str) -> str:
    """向 URL 追加查询参数（自动判断 ? / &）。"""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{query}"


def snapshot_url(url: str) -> str:
    """生成 rtp2httpd 视频快照 URL（需服务端配置 ffmpeg）。"""
    return append_query(url, "snapshot=1")


def apply_token(url: str, token: str | None) -> str:
    """r2h-token 通过 URL 参数注入（文档支持的三种方式之一）。"""
    if not token or "r2h-token" in url:
        return url
    return append_query(url, f"r2h-token={quote(token, safe='')}")


def parse_playlist(text: str, token: str | None = None) -> list[Channel]:
    """解析 M3U/M3U8 文本，返回频道列表（自动去重）。

    支持：#EXTINF 属性（tvg-id/tvg-name/group-title/tvg-logo/catchup-source）、
    #EXTGRP 分组、单频道多 URL 行、URL 末尾 $label。
    """
    channels: list[Channel] = []
    current: dict[str, str | None] | None = None
    pending_group: str | None = None  # #EXTGRP 作用于下一条 #EXTINF

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        if line.startswith("#EXTINF"):
            attrs = dict(_ATTR_RE.findall(line))
            name = (
                line.rsplit(",", 1)[1].strip()
                if "," in line
                else attrs.get("tvg-name", "")
            )
            group = attrs.get("group-title") or pending_group
            current = {
                "name": name,
                "group": group,
                "logo": attrs.get("tvg-logo"),
                "tvg_id": attrs.get("tvg-id"),
                "catchup": attrs.get("catchup-source"),
            }
            pending_group = None
            continue

        if line.startswith("#EXTGRP"):
            if ":" in line:
                pending_group = line.split(":", 1)[1].strip()
            continue

        if line.startswith("#"):
            continue

        # 普通行 = 频道 URL
        if current is None:
            continue
        url = _strip_label(line)
        url = apply_token(url, token)
        if not url.startswith(_URL_PREFIXES):
            continue
        name = current["name"] or url
        if not name:
            continue
        channels.append(
            Channel(
                name=name,
                url=url,
                group=current["group"],
                logo=current["logo"],
                tvg_id=current["tvg_id"],
                catchup=current["catchup"],
            )
        )

    # 按 key 去重，保持原始顺序
    seen: set[str] = set()
    result: list[Channel] = []
    for ch in channels:
        if ch.key not in seen:
            seen.add(ch.key)
            result.append(ch)
    return result
