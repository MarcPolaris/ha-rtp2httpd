# rtp2httpd 集成 for Home Assistant

把 iStoreOS / OpenWrt 旁路由上的 **rtp2httpd**（组播 RTP/RTSP/HLS → HTTP 单播的
IPTV 转发服务器，[stackia/rtp2httpd](https://github.com/stackia/rtp2httpd)）
接入 Home Assistant：实时监控服务状态、控制服务、查看接入设备，可选频道直播。

> 接口基于 rtp2httpd 源码（src/status.c、src/connection.c）验证：
> `GET /status/sse`（状态）、`POST /status/api/*`（控制），默认端口 5140。

---

## 一、功能一览

| 类型 | 实体 | 说明 |
|---|---|---|
| sensor | `服务状态` | 运行中/离线（永远可用），属性含版本、日志级别、启动时间、worker 明细、错误原因 |
| sensor | `运行时长` | 服务运行时长，显示 `12h 24m 56s`，属性另存秒数 |
| sensor | `最大客户端数` | rtp2httpd 配置的上限 |
| sensor | `工作进程数` | worker 数量（属性含每个 worker 的 PID/连接数/带宽） |
| sensor | `在线客户端数` | 当前正在播放的流会话数量 |
| sensor | `当前总带宽` | 所有客户端推流带宽合计（**Mbps**，属性保留 MB/s 显示） |
| sensor | `累计流量` | 服务启动以来发送总量（**GB**，属性含易读格式与原始字节） |
| sensor | `当前播放频道` | 现在电视/播放器在看哪个台（`未播放` / 频道名 / 组播地址），属性含全部会话明细（客户端、组播地址、已播放时长、带宽、台标、tvg-id） |
| sensor | `客户端 <IP>`（动态） | 每个接入设备一个：状态（推流中/连接中/…）、频道、带宽、队列、丢包 |
| sensor | `客户端 <IP> 频道`（动态） | 每个接入设备一个：该设备正在播放的频道名 |
| binary_sensor | `服务在线` | 一直可用，适合做掉线自动化（binary_sensor 的 on/off） |
| switch | `连接 <IP>`（动态） | 每个客户端一个：开=传输中，**关闭即强制断开** |
| button | `重载配置` / `重启工作进程` / `清空日志` | 等效 SIGHUP / SIGUSR1 / 清日志 |
| button | `断开 <IP>`（动态） | 强制断开某个客户端（该 IP 会被封禁 5 秒） |
| select | `日志级别` | 致命(0)~调试(4) 运行时调整，立即生效 |
| media_player | 每个频道一个（可选） | 逻辑播放器，供服务/自动化/语音调用 |
| camera | 每个频道一个（可选） | 面板内直接看直播（走 HA stream 组件） |

动态实体随客户端连入/断开自动创建/**彻底删除**（连同实体注册表条目一起清理），
设备页不会残留"不可用"的僵尸实体。

## 二、安装

### 方式 A：HACS（推荐）

1. HACS → 右上角 ⋮ → **自定义存储库** → 添加
   `https://github.com/MarcPolaris/ha-rtp2httpd`，类别选 **Integration**
2. 搜索 `rtp2httpd` 安装并重启 HA

### 方式 B：手动

把 `custom_components/rtp2httpd/` 整个目录拷贝到 HA 的
`<config>/custom_components/rtp2httpd/`，然后重启 HA。

> 要求：Home Assistant ≥ 2024.1；`stream` 集成由依赖自动装载，无需手动配置。

## 三、配置

设置 → 设备与服务 → 添加集成 → 搜索 `rtp2httpd`：

| 字段 | 填写 |
|---|---|
| 实例名称 | 如 `rtp2httpd` |
| rtp2httpd 地址 | `192.168.2.2`（你的 iStoreOS 旁路由） |
| 端口 | `5140`（如改过则填实际端口） |
| r2h-token | 若 rtp2httpd 配置了 `r2h-token` 则必填，否则留空 |

添加后即生成状态/控制/设备类实体。如需频道直播，在「选项」里开启
`创建媒体播放器实体` / `创建摄像头实体`，并在频道列表里勾选要导入的频道。

### 选项说明

| 选项 | 默认 | 说明 |
|---|---|---|
| 播放列表 URL | `http://<host>:<port>/playlist.m3u` | 频道来源，一般不用改 |
| 状态地址 | `http://<host>:<port>/status/sse` | 状态轮询地址（如改过 status-page-path 需调整） |
| 频道列表刷新间隔 | 3600 秒 | 频道增删后生效 |
| 状态轮询间隔 | 10 秒 | 客户端/带宽刷新频率 |
| 创建媒体播放器实体 | 关 | 每个频道一个 media_player |
| 创建摄像头实体 | 关 | 每个频道一个 camera（面板内看直播） |
| 摄像头截图 | 关 | 缩略图走 `?snapshot=1`，**需 rtp2httpd 配置 ffmpeg-path**，OpenWrt 上 CPU 开销大，建议频道多时保持关闭 |
| 频道名称来源 | 空 | 一份**含 `rtp://` / `udp://` 原始组播地址**的 M3U，用于把播放中的 `/rtp/238.1.78.210:7552` 反查成频道名。填了「当前播放频道」才显示台名，留空则只显示组播地址 |

> 「频道名称来源」和「播放列表 URL」不是一回事：`/playlist.m3u` 是 rtp2httpd
> **转换后**的列表（地址已被改写成 `/分组/频道名`），拿不到组播地址；要反查频道名
> 必须用 rtp2httpd 的 `external-m3u` 原始文件。iStoreOS 常见的做法是把原始列表放到
> web 根目录，例如 `http://192.168.2.2/iptv.m3u`。

保存选项后集成自动重载生效。

## 四、使用示例

### 仪表盘：接入设备监控卡片

```yaml
type: entities
entities:
  - sensor.rtp2httpd_service_status
  - sensor.rtp2httpd_client_count
  - sensor.rtp2httpd_bandwidth
  - sensor.rtp2httpd_total_bytes
  - button.rtp2httpd_reload_config
  - select.rtp2httpd_log_level
```

（实际 entity_id 以生成的为准，可在「设置 → 实体」中查看。）

### 自动化：客户端断开提醒

```yaml
trigger:
  - platform: state
    entity_id: sensor.rtp2httpd_client_count
notification:
  message: "rtp2httpd 在线客户端变为 {{ states('sensor.rtp2httpd_client_count') }} 个"
```

### 自动化：定时重载配置（如外部 M3U 更新）

```yaml
trigger:
  - platform: time_pattern
    hours: "/6"
action:
  - service: button.press
    target:
      entity_id: button.rtp2httpd_reload_config
```

### 语音换台（需先开启媒体播放器实体）

暴露 `media_player.rtp2httpd_cctv_1` 等实体到 Assist，别名设为频道名，
即可语音"播放中央一台"。

### 语音查询「电视在看什么台」

`sensor.rtp2httpd_dang_qian_bo_fang_pin_dao`（`当前播放频道`）的 state 就是频道名，
例如 `枞阳电视台`；无人在看时是 `未播放`。

1. 设置 → 语音助手 → 暴露 → 勾选该实体
2. 给实体加别名（设置 → 实体 → 当前播放频道 → 别名）：`电视`、`电视在看什么`、`频道`
3. 之后问「电视在看什么台」即可得到播报

属性结构（模板 / 自动化可直接取）：

```yaml
# 属性示例
client_count: 1
multicast: "238.1.78.210:7552"      # 主会话的组播地址
channel: "枞阳电视台"
client_addr: "192.168.2.100:60456"
duration: "1h 6m 23s"
duration_seconds: 3983
service_url: "/rtp/238.1.78.210:7552?fcc=117.71.18.200:15970"
sessions:                            # 多客户端时全部列出（按时长倒序）
  - client_addr: "192.168.2.100:60456"
    channel: "枞阳电视台"
    multicast: "238.1.78.210:7552"
    duration: "1h 6m 23s"
    bandwidth: 2.6                   # Mbps
```

常见自动化：根据频道切换场景

```yaml
trigger:
  - platform: state
    entity_id: sensor.rtp2httpd_dang_qian_bo_fang_pin_dao
    to: "CCTV-5 体育"
action:
  - service: light.turn_on
    target:
      entity_id: light.living_room
```

## 五、控制接口对照（rtp2httpd 原生）

| HA 实体 | rtp2httpd API | 等效 |
|---|---|---|
| 重载配置 | `POST /status/api/reload-config` | SIGHUP |
| 重启工作进程 | `POST /status/api/restart-workers` | SIGUSR1 |
| 清空日志 | `POST /status/api/clear-logs` | — |
| 断开 <IP> | `POST /status/api/disconnect`（body `client_id=`） | 断开并封禁 IP 5s |
| 日志级别 | `PUT /status/api/log-level`（body `level=0~4`） | 运行时生效 |

> 注意：rtp2httpd 的 HTTP API **没有**"启用/禁用整个服务"的接口（那是 OpenWrt
> 服务管理，需 SSH/init.d）。集成的"开关"类实体均为运行期控制；若需要服务
> 启停开关，可另行配合 HA 的 SSH 集成或路由器计划任务。

## 六、故障排查

| 现象 | 处理 |
|---|---|
| 添加集成提示"无法连接" | 检查旁路由 IP/端口、HA 到 192.168.2.2 网络是否通（`ping`、`curl http://192.168.2.2:5140/status/sse`） |
| 提示"r2h-token 无效" | rtp2httpd 配置里 `r2h-token` 与 HA 填写的必须一致 |
| 服务状态为"离线"但网页正常 | `status-page-path` 被改过 → 在选项里把状态地址改成实际路径 |
| 所有数据实体"不可用"（unavailable） | 多为旧版集成的 SSE 长连接超时问题 → 更新到 1.1.0+（改用 `/status/sse?snapshot=1` 快照轮询）；若仍不可用，检查 r2h-token 是否填写正确 |
| 传感器一直显示"未知" | 1.1.2 之前的版本缺少 `SensorEntity` 基类，已修复，请更新到 1.3.0+ 并重启 HA |
| 所有频道为空 | rtp2httpd 未配置 M3U（`external-m3u` / `[services]`），`/playlist.m3u` 无内容 |
| 摄像头无画面 | 确认选项开启 camera 且 `stream` 已装载；频道 URL 需 HA 可直接访问 |
| 摄像头截图不显示 | `video-snapshot` 未开启或 `ffmpeg-path` 未配置（rtp2httpd 侧） |

## 七、数据流

```
安徽电信组播 RTP ──► rtp2httpd (192.168.2.2:5140)
                        │  /status/sse ──► HA 传感器/设备实体（每 10s 轮询）
                        │  /status/api/* ──► HA 按钮/选择（控制）
                        │  /playlist.m3u ──► HA 频道实体（可选，media_player/camera）
                        └─ 组播转 HTTP 单播 ──► 各播放设备
```

## 八、验证情况

在真实环境（HAOS 2026.10.4 + rtp2httpd 3.16.0，iStoreOS 旁路由）长期运行验证：

- 状态轮询每 10 秒成功，7 个静态传感器状态机取值正确
- 客户端实体随播放设备连入/断开自动创建与清理，无残留"不可用"
- 控制类实体（重载/重启/清日志/日志级别/断开/连接开关）全部实测可用
- 解析器单测：`python tests/test_parsers.py`（M3U 与状态 SSE 解析用例）

## 九、文件结构

```
custom_components/rtp2httpd/
├── __init__.py        入口：协调器创建与平台装载
├── config_flow.py     首次配置与选项流程
├── const.py           常量/默认值
├── coordinator.py     频道/状态协调器
├── m3u.py             M3U 解析与 URL 工具
├── status.py          状态 SSE 解析与控制 API 封装
├── sensor.py          状态/客户端传感器（动态实体）
├── binary_sensor.py   服务在线二进制传感器
├── switch.py          客户端连接开关（动态实体）
├── button.py          控制与断开按钮（动态实体）
├── select.py          日志级别选择
├── registry_utils.py  断开客户端时清理残留实体注册条目
├── media_player.py    频道媒体播放器（可选）
├── camera.py          频道直播摄像头（可选）
├── brand/             集成图标（HA 2026.3+ 自动显示）
├── manifest.json
└── strings.json / translations/zh-Hans.json
```

## 十、品牌图标

`brand/icon.png`（256×256）与 `brand/icon@2x.png`（512×512）为集成图标，
Home Assistant **2026.3 及以上**会自动读取并在集成卡片显示；更低版本只是不显示图标，
不影响功能。

## 十一、免责声明

本项目是社区第三方集成，与 Home Assistant 官方及 rtp2httpd 作者无关。
使用 MIT 许可证。IPTV 源与频道的合法性由使用者自行负责。
