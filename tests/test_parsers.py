"""rtp2httpd 集成解析器冒烟测试（不依赖 HA 运行环境）。"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
COMPONENT = BASE / "custom_components" / "rtp2httpd"


# ---- homeassistant 轻量桩（仅让解析模块可独立加载，不做任何 HA 行为）----
import types  # noqa: E402

_ha = types.ModuleType("homeassistant")
_ha.core = types.ModuleType("homeassistant.core")
_ha.core.HomeAssistant = object
_ha.const = types.ModuleType("homeassistant.const")
_ha.const.CONF_HOST = "host"
_ha.const.CONF_NAME = "name"
_ha.const.CONF_PORT = "port"
_ha.const.CONF_TOKEN = "token"
_ha.helpers = types.ModuleType("homeassistant.helpers")
_ha.helpers.aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
_ha.helpers.aiohttp_client.async_get_clientsession = lambda *a, **k: None
_ha.helpers.aiohttp_client.ClientTimeout = lambda total=None: object()
sys.modules["homeassistant"] = _ha
sys.modules["homeassistant.core"] = _ha.core
sys.modules["homeassistant.const"] = _ha.const
sys.modules["homeassistant.helpers"] = _ha.helpers
sys.modules["homeassistant.helpers.aiohttp_client"] = _ha.helpers.aiohttp_client

# 伪包：让 status.py 的相对导入（.const / .m3u）可解析
_pkg = types.ModuleType("rtp2httpd")
_pkg.__path__ = [str(COMPONENT)]
sys.modules["rtp2httpd"] = _pkg


def _load(name: str):
    """绕过包 __init__（会导入 homeassistant），直接按文件加载模块。"""
    spec = importlib.util.spec_from_file_location(
        f"rtp2httpd.{name}", COMPONENT / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"rtp2httpd.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


m3u = _load("m3u")
status_mod = _load("status")


def test_playlist_parse() -> None:
    text = (BASE / "samples/sample_playlist.m3u").read_text(encoding="utf-8")
    channels = m3u.parse_playlist(text, token="secret")

    assert len(channels) == 6, f"期望 6 个频道，实际 {len(channels)}"

    # 1) 常规频道 + 属性
    c1 = channels[0]
    assert c1.name == "CCTV-1"
    assert c1.group == "央视"
    assert c1.tvg_id == "CCTV1"
    assert c1.url == (
        "http://192.168.2.2:5140/CCTV/CCTV-1?r2h-token=secret"
    ), c1.url

    # 2) 同名频道去重后 key 稳定
    keys = [c.key for c in channels]
    assert len(keys) == len(set(keys)), "存在重复 key"

    # 3) $label 清晰度标签应被剥掉，且每个源独立成频道
    gd = [c for c in channels if c.name == "广东卫视"]
    assert len(gd) == 2
    assert all("$HD" not in c.url and "$SD" not in c.url for c in gd)
    assert "catchup" in (gd[0].catchup or "")

    # 4) #EXTGRP 覆盖分组
    hunan = [c for c in channels if c.name == "湖南卫视"][0]
    assert hunan.group == "高清"

    # 5) 已有 token 参数的 URL 不重复注入
    assert hunan.url.count("r2h-token") == 1

    print(f"[PASS] m3u parse: {len(channels)} channels")


def test_url_helpers() -> None:
    assert m3u.append_query("http://a:1/x", "a=1") == "http://a:1/x?a=1"
    assert m3u.append_query("http://a:1/x?fcc=1", "a=1") == "http://a:1/x?fcc=1&a=1"
    assert m3u.snapshot_url("http://a:1/x?t=1") == "http://a:1/x?t=1&snapshot=1"
    assert m3u.apply_token("http://a:1/x", "tok") == "http://a:1/x?r2h-token=tok"
    assert m3u.apply_token("http://a:1/x?r2h-token=old", "tok") == (
        "http://a:1/x?r2h-token=old"
    )
    print("[PASS] url helpers")


def test_status_parse() -> None:
    raw = (BASE / "samples/sample_status_sse.json").read_text(encoding="utf-8")
    compact = json.dumps(json.loads(raw), ensure_ascii=False, separators=(",", ":"))
    # 模拟真实 SSE：整条 JSON 放在一行 data: 后
    sse = f"data: {compact}\n\n"
    status = status_mod.parse_status(sse)

    assert status.online is True
    assert status.version == "1.0.0"
    assert status.uptime_ms == 86400000
    assert status.max_clients == 20
    assert status.log_level == 3
    assert status.total_clients == 3
    assert status.total_bytes == 367001600
    assert status.total_bandwidth == 3984384
    assert len(status.clients) == 3
    assert len(status.workers) == 2

    c1 = status.clients[0]
    assert c1.addr == "192.168.2.100"
    assert c1.state == 29
    assert c1.state_name == "推流中"
    assert c1.bandwidth == 1048576
    assert c1.slow is False
    assert status.clients[1].state_name == "FCC 单播播放"
    assert status.clients[2].state_name == "RTSP 播放中"

    # 纯 JSON（无 SSE 包装）也应可解析
    status2 = status_mod.parse_status(raw)
    assert status2.total_clients == 3

    print("[PASS] status parse")


def test_status_parse_plain_json_line() -> None:
    """单行 SSE（紧凑 JSON）也应可解析。"""
    raw = (BASE / "samples/sample_status_sse.json").read_text(encoding="utf-8")
    compact = json.dumps(json.loads(raw), ensure_ascii=False, separators=(",", ":"))
    status = status_mod.parse_status("data: " + compact + "\n\n")
    assert status.online is True
    print("[PASS] status parse (single-line SSE)")


if __name__ == "__main__":
    test_url_helpers()
    test_playlist_parse()
    test_status_parse()
    test_status_parse_plain_json_line()
    print("ALL TESTS PASSED")
