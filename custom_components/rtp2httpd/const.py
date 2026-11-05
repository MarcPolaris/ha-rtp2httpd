"""常量定义：rtp2httpd 集成。

数据模型
--------
- entry.data:    host / port / token / name（连接参数，重装集成才会变）
- entry.options: playlist_url / status_url / refresh_interval /
                 status_interval / create_media_players / create_cameras /
                 use_snapshot / channels（可在「选项」中随时修改，保存后自动重载）
"""
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_TOKEN

DOMAIN = "rtp2httpd"

# ---- 平台 ----
PLATFORMS = [
    "media_player",
    "camera",
    "sensor",
    "binary_sensor",
    "button",
    "switch",
    "select",
]

# ---- 配置项 ----
CONF_PLAYLIST_URL = "playlist_url"
CONF_STATUS_URL = "status_url"
CONF_REFRESH_INTERVAL = "refresh_interval"        # 频道列表刷新周期（秒）
CONF_STATUS_INTERVAL = "status_interval"          # 状态轮询周期（秒）
CONF_CREATE_MEDIA_PLAYERS = "create_media_players"
CONF_CREATE_CAMERAS = "create_cameras"
CONF_USE_SNAPSHOT = "use_snapshot"
CONF_CHANNELS = "channels"
CONF_CHANNEL_MAP_URL = "channel_map_url"           # 原始 M3U（含 rtp:// 地址）用于反查频道名

# ---- 默认值 ----
DEFAULT_PORT = 5140
DEFAULT_REFRESH_INTERVAL = 3600
DEFAULT_STATUS_INTERVAL = 10
DEFAULT_CREATE_MEDIA_PLAYERS = False
DEFAULT_CREATE_CAMERAS = False
DEFAULT_USE_SNAPSHOT = False

# ---- rtp2httpd 内置路径 ----
PLAYLIST_PATH = "playlist.m3u"
STATUS_SSE_PATH = "status/sse"
SNAPSHOT_QUERY = "snapshot=1"

# ---- 状态 API ----
API_RELOAD_CONFIG = "status/api/reload-config"
API_RESTART_WORKERS = "status/api/restart-workers"
API_CLEAR_LOGS = "status/api/clear-logs"
API_DISCONNECT = "status/api/disconnect"
API_LOG_LEVEL = "status/api/log-level"

# ---- 实体附加属性 ----
ATTR_GROUP = "group"
ATTR_LOGO = "logo"
ATTR_TVG_ID = "tvg_id"
ATTR_CHANNEL_URL = "channel_url"
ATTR_CATCHUP = "catchup"
ATTR_VERSION = "version"
ATTR_UPTIME = "uptime"
ATTR_STARTED = "started_at"
ATTR_LOG_LEVEL = "log_level"
ATTR_MAX_CLIENTS = "max_clients"
ATTR_WORKERS = "workers"
ATTR_TOTAL_CLIENTS = "total_clients"
ATTR_TOTAL_BYTES = "total_bytes"
ATTR_TOTAL_BANDWIDTH = "total_bandwidth"
ATTR_CLIENT_ID = "client_id"
ATTR_CLIENT_ADDR = "client_addr"
ATTR_SERVICE_URL = "service_url"
ATTR_STATE_CODE = "state_code"
ATTR_BYTES_SENT = "bytes_sent"
ATTR_BANDWIDTH = "bandwidth"
ATTR_QUEUE_BYTES = "queue_bytes"
ATTR_QUEUE_LIMIT = "queue_limit_bytes"
ATTR_QUEUE_HIGHWATER = "queue_highwater_bytes"
ATTR_DROPPED_BYTES = "dropped_bytes"
ATTR_SLOW = "slow"
ATTR_CHANNEL = "channel"                 # 反查到的频道名
ATTR_MULTICAST = "multicast"             # 组播/单播地址 host:port
ATTR_CHANNEL_GROUP = "channel_group"
ATTR_CHANNEL_LOGO = "channel_logo"
ATTR_CHANNEL_TVG_ID = "channel_tvg_id"
ATTR_DURATION = "duration"               # 本次连接已持续时长（12h 24m 56s）
ATTR_DURATION_SECONDS = "duration_seconds"
ATTR_PLAYING_SECONDS = "playing_seconds"

# ---- 日志级别选项（rtp2httpd: 0=FATAL..4=DEBUG）----
LOG_LEVELS = {
    "致命(0)": 0,
    "错误(1)": 1,
    "警告(2)": 2,
    "信息(3)": 3,
    "调试(4)": 4,
}
LOG_LEVEL_NAME = {0: "FATAL", 1: "ERROR", 2: "WARN", 3: "INFO", 4: "DEBUG"}

