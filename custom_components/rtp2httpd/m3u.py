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
from urllib.parse import quote, unquote, urlsplit

_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
_URL_PREFIXES = ("rtp://", "udp://", "rtsp://", "http://", "https://")

# 从 URL 中提取流地址：/rtp/<addr:port> 或 rtp://<addr:port> / udp://@<addr:port>
_PROXY_STREAM_RE = re.compile(r"/(?:rtp|udp|rtsp)/([^/]+)$", re.I)
_DIRECT_STREAM_RE = re.compile(r"^(?:rtp|udp|rtsp)://@?([^/]+?)/?$", re.I)


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


def _strip_query(url: str) -> str:
    return url.split("#", 1)[0].split("?", 1)[0].strip()


def stream_key(url: str) -> str | None:
    """提取播放地址里的流标识（组播/单播地址 host:port）。

    rtp2httpd 状态里上报的 serviceUrl 形如 `/rtp/238.1.78.210:7552?fcc=...`，
    而原始 M3U 里写的是 `/rtp/238.1.78.210:7552?fcc=...`、`rtp://238.1.78.210:7552`
    或 `udp://@238.1.78.210:7552`——统一归一化成 `238.1.78.210:7552` 才能对上。
    """
    if not url:
        return None
    path = _strip_query(url)
    match = _PROXY_STREAM_RE.search(path)
    if match:
        target = match.group(1)
    else:
        direct = _DIRECT_STREAM_RE.match(path)
        if not direct:
            return None
        target = direct.group(1)
    target = target.strip().rstrip("/").lower()
    return target or None


def path_key(url: str) -> str | None:
    """提取 URL 路径（解码 + 小写），用于匹配 `/<分组>/<频道名>` 形式的请求。"""
    if not url:
        return None
    try:
        path = unquote(urlsplit(url).path)
    except ValueError:
        return None
    path = path.strip().rstrip("/").lower()
    return path or None


def build_channel_index(channels: list[Channel]) -> dict[str, Channel]:
    """建立「流地址 / 路径 -> 频道」索引，供播放状态反查频道名。"""
    index: dict[str, Channel] = {}
    for ch in channels:
        for key in (stream_key(ch.url), path_key(ch.url)):
            if key and key not in index:
                index[key] = ch
    return index


def lookup_channel(index: dict[str, Channel], service_url: str) -> Channel | None:
    """按 rtp2httpd 上报的 serviceUrl 反查频道（先按流地址，再按路径）。"""
    if not index or not service_url:
        return None
    for key in (stream_key(service_url), path_key(service_url)):
        if key and key in index:
            return index[key]
    return None
