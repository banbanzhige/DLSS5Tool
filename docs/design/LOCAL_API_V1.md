# 本机视频任务 API v1 设计

状态：接口设计草案，尚未实现 HTTP 服务。日期：2026-11-12。
源码参考：v3.1.3 / `e5e06dc`。本文描述拟新增的行为，不代表当前发行包已经提供这些接口。
机器可读合同：[OpenAPI 3.1.1](local-api-v1.openapi.yaml)。

## 1. 第一版交付什么

外部程序通过 HTTP 提交本机视频，立即取得任务 ID，轮询进度，完成后取得本地结果路径或下载视频。
第一版由桌面应用手动启用服务，后续视频任务可全程由脚本操作；应用退出时服务随之停止。
执行核心不依赖 Tk 控件，便于后续增加独立启动方式。

| 项目 | v1 约定 |
| --- | --- |
| 地址 | `http://127.0.0.1:17865/api/v1`；端口可在本地设置中修改，冲突时明确报错 |
| 输入 | 单个本地视频的绝对路径；由用户在本地配置允许读取的目录 |
| 输出 | 统一写到本地配置的 API 输出目录，每个任务一个子目录 |
| 处理 | 基础神经渲染、2× / 4× 超分、2× 插帧、可选 NVOFA / RAFT、保留 HDR 或转 SDR |
| 参数 | 显式 JSON 参数和固定的 API 默认值；每项能力取决于本机组件与组合检查 |
| 调度 | 自动处理 API 队列；整个应用同时最多执行一个导出任务 |
| 认证 | 所有接口都要求本机生成的 Bearer token，包括查询和下载 |
| 查询 | 客户端每 2 秒轮询一次；第一版采用普通 HTTP 请求 |

图片、图片序列、实时窗口控制、上传视频、输入 URL、回调通知、流式进度、3× / 4× 实验插帧、
多用户、远程部署和中途续导留待后续独立设计。命名预设也后续再加；第一版可直接复用同一份请求 JSON。
高级残差、保护区域、分块和宿主调度参数暂不作为公共字段开放。

## 2. 接口清单

以下路径均相对 `/api/v1`。JSON 响应使用 UTF-8，时间统一为带 `Z` 的 UTC RFC 3339 字符串。

| 方法 | 路径 | 用途 | 正常 HTTP 状态 |
| --- | --- | --- | --- |
| GET | `/openapi.json` | 获取当前服务的机器可读接口规格 | 200 |
| GET | `/status` | 服务、API 队列和应用 GPU 占用 | 200 |
| GET | `/capabilities` | 参数范围、功能可用性、固定默认值和限制 | 200 |
| POST | `/jobs` | 提交视频任务 | 202 |
| GET | `/jobs` | 分页查询 API 任务 | 200 |
| GET | `/jobs/{job_id}` | 查询一项任务及结果元数据 | 200 |
| POST | `/jobs/{job_id}/cancel` | 取消等待项或请求取消执行中的任务 | 200 / 202 |
| POST | `/jobs/{job_id}/retry` | 为失败、取消或中断项新建一次处理任务 | 202 |
| GET | `/jobs/{job_id}/result` | 下载已完成任务的视频 | 200，视频字节流 |
| POST | `/queue/pause` | 当前 API 任务结束后暂停后续调度 | 200 |
| POST | `/queue/resume` | 恢复 API 队列调度 | 200 |

任务接口只操作 `origin=api` 的记录，不能借任务 ID 操作桌面手动任务。
暂停和恢复只影响 API 逻辑队列；桌面批量队列仍按用户原有操作启动或暂停。
两种队列使用同一任务所有者协调导出资源，不允许各自启动一套并发导出。

请求头：

```http
Authorization: Bearer <本地设置中取得的 token>
Content-Type: application/json
Idempotency-Key: 649af056-bbe5-4197-9fa6-9832226ea6d0
```

`Content-Type` 用于有 JSON 正文的提交；`Idempotency-Key` 在提交和重试接口必填。
所有响应包含 `X-Request-Id`，JSON 和下载响应均使用 `Cache-Control: no-store`。

## 3. 提交任务

`POST /jobs` 的最小正文：

```json
{
  "input": {"path": "D:/Videos/demo.mp4"}
}
```

较完整的请求：

```json
{
  "input": {"path": "D:/Videos/demo.mp4"},
  "processing": {
    "enhancement": {
      "enabled": true,
      "style": "natural",
      "intensity": 0.8,
      "local_tone": 1.0,
      "local_struct": 1.0,
      "use_auto_mask": true,
      "skin_struct": 1.0,
      "output_mix": 1.0
    },
    "super_resolution": {"scale": 2},
    "frame_generation": {"multiplier": 1},
    "optical_flow": "off"
  },
  "output": {
    "container": "mp4",
    "file_name": "demo_enhanced.mp4",
    "dynamic_range": "preserve",
    "encoding": {"mode": "quality", "quality_profile": "high", "preset": "p5"}
  },
  "client_tag": "video-pipeline-001"
}
```

所有示例路径均为说明用途，不创建这些目录。服务在本地配置的输出根下面生成
`<job_id>/demo_enhanced.mp4`；客户端只能指定文件名，不能传输出目录或覆盖既有文件。

| 字段 | 默认值 | 范围 / 含义 |
| --- | --- | --- |
| `input.path` | 必填 | Windows 本地盘绝对文件路径，可用 `/` 或 JSON 转义的 `\\` |
| `processing.enhancement.enabled` | true | false 时绕过神经渲染，仍可进行超分或插帧 |
| `processing.enhancement.style` | `default` | `default` / `natural` / `cinema`，对应现有风格 0 / 1 / 2 |
| `intensity` / `local_tone` / `local_struct` / `skin_struct` | 1 | 各自 0–5，语义沿用现有参数合同 |
| `use_auto_mask` | true | 自动肤色遮罩；关闭后 `skin_struct` 不参与处理 |
| `output_mix` | 1 | 0–5；1 为现有标准合成，超过 1 不是百分比 |
| `processing.super_resolution.scale` | 1 | 1 / 2 / 4；1 表示不超分 |
| `processing.frame_generation.multiplier` | 1 | 1 / 2；1 表示不插帧 |
| `processing.optical_flow` | `off` | `off` / `nvofa` / `raft`；只作增强的时序引导，不等于插帧 |
| `output.container` | `mp4` | `mp4` / `mkv` / `mov` |
| `output.file_name` | 按源名称生成 | 单个文件名，扩展名须匹配 container |
| `output.dynamic_range` | `preserve` | HDR 源按已有 HDR10 / HLG 合同保留；`sdr` 显式转 SDR；SDR 源保持 SDR |
| `output.encoding` | 画质 high / p5 | 见下面两种互斥格式 |
| `client_tag` | null | 最多 128 字符，调用方关联标记，可重复，不提供去重语义 |

`intensity` 等简写字段在上表中均位于 `processing.enhancement`。所有对象拒绝未知字段，
拒绝类型转换、NaN / Infinity 和超范围值；不将错误参数悄悄夹到边界。
关闭增强后仍检查增强字段是否合法；此时 `optical_flow` 必须为 `off`。

编码参数二选一：

```json
{"mode": "quality", "quality_profile": "high", "preset": "p5"}
```

```json
{"mode": "bitrate", "bitrate_mbps": 20.0, "preset": "p5"}
```

画质为 `maximum` / `high` / `balanced` / `compact`；码率 0.5–500 Mbps；
preset 为 `p1` / `p3` / `p5` / `p7`。明确指定编码模式时 `mode` 必填，禁止混传两种模式的专有字段。
编码器、位深、尺寸对齐和音轨处理沿用桌面选路；结果返回实际编码器及音轨处理方式。
保留 HDR 不包含 Dolby Vision / HDR10+ 动态元数据，沿用现有用户指南的边界。

### 接收时的检查与快照

接收阶段只做有界、无 GPU 初始化的检查：认证、JSON、字段、路径授权、源文件是否存在、
队列容量、已知缺失组件和确定不支持的组合。不能以扩展名作为视频可解码的证明。
媒体探测、完整时间线检查、实际 GPU 初始化和运行期组合检查属于异步任务，可能随后失败。

固定默认值由 `defaults_version=1` 标识，不能读取 GUI 当时滑条来补齐缺省字段。
服务先补全公共参数，再快照当前本地选择的 GPU、运行库文件身份及执行配置，随后持久化任务。
API 导出固定采用顺序任务模式和处理结果视图；未开放的残差 / 画质参数为中性值、区域保护关闭，
其余执行策略使用版本化默认值，不继承 GUI 隐藏调参。设备 / 运行库选择和经过验证的安全执行策略才进入执行配置快照。
排队后修改 GUI 不改变该任务；运行库被替换或设备消失时明确失败，不能换库、换卡或降画质完成。
请求参数和执行快照不得包含可变的 GPU 会话、共享内存句柄或 Tk 对象。

源身份保存文件标识、大小及高精度修改时间，执行前重新检查，变化时失败 `INPUT_CHANGED`。
身份检查不能承诺识别刻意伪造文件身份的内容替换；执行期应保持源只读且阻止写入 / 替换，
具体句柄策略要与独立解码进程一起验收。

### 202 与重复提交

```http
HTTP/1.1 202 Accepted
Location: /api/v1/jobs/61be504e3a8948e29b5dd98b27533d0a
Retry-After: 2
Idempotency-Replayed: false
```

正文是完整 Job 对象。202 表示任务和去重记录已持久化，不表示视频已验证完毕或开始处理。

- 去重键按“当前安装数据域＋HTTP 方法＋规范路径”分区，保留至少 7 天，重启后仍有效。
- 同键同请求返回第一次创建的同一任务及其当前状态，仍为 202，`Idempotency-Replayed: true`。
  这个重放判断先于再次检查源文件、队列容量或组件状态，避免完成后重发被误当成新任务。
- 同键不同请求返回 409 `IDEMPOTENCY_CONFLICT`。不同键代表新任务，即使路径和参数相同。
- 请求指纹基于收到的 JSON 结构：对象键排序、数字按 JSON 数值规范化、数组保序；
  缺省字段与显式填写默认值视为不同请求。不把当前 GUI / 默认值 / 文件状态混入指纹。
- 一个所有者串行提交“任务＋去重映射＋输出路径预留”，持久化成功后才返回；并发同键只能创建一项。
- 校验失败、队列满或未成功落盘不占用键。响应丢失时必须用原键、原请求重试。
  键过期后可能创建新任务，调用方应为每次业务提交保存自己的键与返回 ID。

## 4. 查询、状态与结果

`GET /jobs/{job_id}` 返回示例：

```json
{
  "job_id": "61be504e3a8948e29b5dd98b27533d0a",
  "retry_of": null,
  "client_tag": "video-pipeline-001",
  "state": "running",
  "phase": "processing",
  "queue_position": null,
  "blocked_reason": null,
  "attempt": 1,
  "defaults_version": 1,
  "progress": {"done_frames": 450, "total_frames": 1800, "fraction": 0.25},
  "created_at": "2026-11-12T02:00:00Z",
  "started_at": "2026-11-12T02:00:01Z",
  "updated_at": "2026-11-12T02:00:20Z",
  "finished_at": null,
  "result": null,
  "error": null
}
```

查询响应还包含补全默认值后的 `request` 和 `warnings`；上例为便于阅读省略这两项。
`request` 是公共请求快照，不暴露内部 DLL 路径、模型路径或宿主参数。

| state | 含义 | 下一步 |
| --- | --- | --- |
| `pending` | 已接收，尚未执行；也可能在等 GPU 或等队列恢复 | 等待、取消 |
| `running` | 准备、处理、内存恢复、封装或正常收尾中 | 等待、取消 |
| `cancelling` | 已记录取消请求，正在停止和回收 | 等待退出确认 |
| `completed` | 输出验证、所有者退休和最终文件提交完成 | 获取结果 |
| `failed` | 当前任务失败，错误已记录 | 排除原因后重试 |
| `cancelled` | 取消已完成，所属工作进程已退出 | 可新建重试 |
| `interrupted` | 应用异常结束，当前执行没有完成确认 | 可新建重试 |

四个终态为 `completed` / `failed` / `cancelled` / `interrupted`。
`failed` 只表示处理结果失败；如果资源回收还没完成，`status.gpu.state` 仍为 `releasing`，
队列保持阻塞，不能仅凭任务终态启动下一项。

`phase` 是当前主要阶段：`queued`、`waiting_gpu`、`preparing`、`inspecting_timestamps`、
`processing`、`encoding`、`muxing`、`waiting_memory`、`finalizing`、`releasing`、`finished`。
流水线阶段可能重叠，不承诺按列表逐个出现。内存恢复中的任务保持 running，phase 为 waiting_memory。

`progress` 只统计当前 attempt 的源帧进度，插帧不把生成帧计入分母；尚未知道帧数时 total / fraction 为 null。
fraction 到 1 仍可能正在封装或回收，调用方只以 state 判断完成。自动重试时 attempt 增加、帧进度从 0 开始；
这不是从中断帧继续。`queue_position` 为当前 API 等待队列中的 1 基快照；其他状态为 null，
它不承诺等待时长或越过桌面任务的顺序。等待期间可通过 blocked_reason 区分 gpu_busy、queue_paused、cleanup_pending。

完成后的 result 示例：

```json
{
  "output_path": "D:/Processed/61be504e3a8948e29b5dd98b27533d0a/demo_enhanced.mp4",
  "download_url": "/api/v1/jobs/61be504e3a8948e29b5dd98b27533d0a/result",
  "content_type": "video/mp4",
  "size_bytes": 183456789,
  "width": 3840,
  "height": 2160,
  "frame_count": 1800,
  "frame_rate": {"numerator": 30, "denominator": 1},
  "duration_seconds": 60.0,
  "dynamic_range": "sdr",
  "codec": "h264",
  "audio_mode": "copied"
}
```

帧率用有理数避免把 30000/1001 写成精确的 29.97；变帧率视频的 frame_rate 为 null，
duration_seconds 来自最终媒体时间线。`frame_count` 是实际输出帧数。

`download_url` 是同源相对地址，仍需同一个认证头，不在 URL 内嵌 token。
GET result 只允许读取该任务已经提交的输出，不接收任意文件路径；未完成返回 409 `RESULT_NOT_READY`。
输出被移动、替换或修改后返回 410 `RESULT_UNAVAILABLE`，任务历史仍保持 completed。
打开下载文件时重新检查文件身份与路径，在持有文件句柄期间阻止替换；不跟随重解析点。
v1 完整流式下载，带 Content-Type / Content-Length / Content-Disposition；不宣告分段下载能力，
收到 Range 时忽略该头并返回完整 200，不返回 206。下载不占 GPU 执行槽。

列表查询支持 `state`、`client_tag` 精确过滤、`limit`（默认 20，最多 100）和不透明 `cursor`。
按 created_at、job_id 倒序，响应为 `{items, next_cursor}`；游标绑定过滤条件，失效或不匹配返回 400。

## 5. 取消、重试和队列控制

取消接口无正文，重复请求不叠加停止动作：

| 当前情况 | 返回和效果 |
| --- | --- |
| 等待中的 pending | 原子移出等待队列，200，state=cancelled |
| 正在运行 | 记录取消意图，202，state=cancelling；停止、回收后才变 cancelled |
| 已在 cancelling | 202，返回同一任务 |
| 已 cancelled | 200，返回同一任务 |
| 其余终态 | 409 `INVALID_STATE`，不能把已完成任务改成取消 |

取消与成功提交最终输出共享一处提交仲裁：取消意图先落盘则不发布最终文件；
最终输出提交先完成则取消返回 409。不能出现同一任务既完成又取消的两个最终结果。
回收失败时不能提前报告 cancelled，保留 cancelling / releasing，status 显示阻塞原因供诊断。

重试接口无正文，要求新的 Idempotency-Key。只接受 failed / cancelled / interrupted，
复制原参数和执行配置快照，新建 job_id，写 retry_of，追加到 API 队尾；旧记录和输出不变。
重试仍核对原源身份和组件身份。旧资源尚未退休时，新任务可以排队但不能启动。
重试不会隐式恢复暂停的 API 队列。它也不支持修改参数；改参数应重新 POST jobs。

API 队列正常时自动调度；GPU 被实时会话、桌面导出、诊断或回收占用时接收请求并等待，
不抢占现有任务，不自动停止实时会话。全局调度只从已经获准运行的任务中按入队顺序选择。
桌面尚未点击开始的等待项不会因 API 提交而自动启动。

pause 幂等，只停止调度下一项，不取消当前项；当前项包括其已启动的有限内存恢复流程。
resume 幂等，只恢复本 API 队列，不改动桌面队列。已知回收失败尚未排除时返回 409 `RESOURCE_CLEANUP_PENDING`；
普通 GPU 忙只进入等待。API 可以在队列暂停时继续接收等待项，status 必须明确呈现暂停原因。

内存恢复复用既有结构化压力、退休、余量检查和有限重试：每项最多 2 次自动重试，
每次等待最多 120 秒。超时 / 次数用尽后该项 failed，API 队列暂停，后续项保持 pending；
普通输入、设备错误不自动重试。不得为了 HTTP 服务另做一套无限重试或降低安全预留。

服务重启时确认旧所有者已退出，再把原 running / cancelling 记录标记 interrupted。
保留 pending，API 队列暂停并标注 service_restart，等待显式 resume；不重放 completed。
首次手动启用且无恢复记录时队列为 running。关闭 API 会停止接收新请求，取消当前 API 任务并等待回收，
保留待处理记录；应用退出沿用既有关闭协议。下一次启用对未完成记录执行同一恢复规则。

## 6. 服务发现与限制

status 返回应用版本、API 版本、accepting_jobs、API 队列状态、等待数、当前 API job_id，
以及应用级 GPU 占用者。GPU 空闲仅是准入快照，不承诺系统中其他应用没有使用 GPU。

capabilities 返回：

- `defaults_version`、补齐后的固定 `defaults`（processing / output）。
- 功能状态 `ready` / `unavailable` / `unknown` 及原因码；增强、超分、插帧、NVOFA、RAFT、HDR 分别报告。
- 公共参数 schema 地址、当前只读 execution_profile_id、配置好的允许输入目录和输出根。
- 队列、请求大小和轮询限制，以及自动内存恢复上限。

GET capabilities 使用已有检查缓存，不因为查询就初始化 GPU、下载组件或替换运行库。
功能 ready 不等于任意组合都支持。确定不支持的组合返回 422；只有运行时才可确认的故障写入异步 error。
允许现有桌面已经验证的 CPU/GPU 选路差异，实际选路写入诊断；不静默关闭用户请求的功能或改变画质。

v1 默认限制：JSON 正文 64 KiB、API pending 项 100、API 同时执行 1、应用全局导出槽 1、
同时下载 2、建议轮询 2 秒。队列满 / 下载槽满返回 429，临时不可接收返回 503，均带 Retry-After。
有界媒体探测也受任务取消与超时监督；HTTP 查询不等待 GPU 或长任务完成。

## 7. 错误格式

HTTP 错误统一为 `application/problem+json`：

```json
{
  "type": "urn:dlss5tool:problem:validation-failed",
  "title": "请求参数无效",
  "status": 422,
  "detail": "超分倍率只能为 1、2 或 4。",
  "instance": "/api/v1/jobs",
  "code": "VALIDATION_FAILED",
  "request_id": "af2d9903d1f441b69265bf7473de8331",
  "invalid_params": [{"name": "/processing/super_resolution/scale", "reason": "取值不在允许范围内"}]
}
```

| HTTP | 常见 code | 调用方处理 |
| --- | --- | --- |
| 400 | `INVALID_JSON` / `INVALID_CURSOR` | 修正正文或游标 |
| 401 | `AUTH_REQUIRED` / `INVALID_TOKEN` | 检查 token；响应带 WWW-Authenticate |
| 403 | `INPUT_FORBIDDEN` / `ORIGIN_FORBIDDEN` / `HOST_FORBIDDEN` | 检查本地授权目录或请求来源 |
| 404 | `JOB_NOT_FOUND` | 检查 job_id；非 API 任务也返回此错误 |
| 409 | `IDEMPOTENCY_CONFLICT` / `INVALID_STATE` / `RESULT_NOT_READY` / `RESOURCE_CLEANUP_PENDING` | 按冲突原因处理 |
| 410 | `RESULT_UNAVAILABLE` | 输出已不可用，历史记录不改写 |
| 413 / 415 | `BODY_TOO_LARGE` / `UNSUPPORTED_MEDIA_TYPE` | 缩小 JSON / 使用 application/json |
| 422 | `VALIDATION_FAILED` / `INPUT_NOT_FOUND` / `INPUT_CHANGED` / `COMPONENT_UNAVAILABLE` / `UNSUPPORTED_COMBINATION` | 修正输入或本机组件 |
| 429 / 503 | `QUEUE_FULL` / `DOWNLOAD_BUSY` / `SERVICE_UNAVAILABLE` | 遵守 Retry-After，以原去重键重试提交 |
| 500 | `INTERNAL_ERROR` | 保存 request_id 排查；提交结果不明时仍以原去重键查询 / 重发 |

异步失败仍由 GET job 返回 HTTP 200，state=failed，error 为 `{code, message, retryable}`。
retryable 表示排除暂时原因后值得人工 / 调用方重试，不表示后台会自动无限重试。
机器按 code / state / phase 判断，不能解析中文 message；响应不带原始 traceback、token 或任意本地日志。

## 8. 本机访问和文件生命周期

只监听 127.0.0.1，Host 必须匹配配置的 loopback authority。默认拒绝带非允许 Origin 的请求，
包括 Origin: null；本机脚本可不发送 Origin。浏览器接入后续只对白名单来源放行 CORS，
不使用 Cookie、不把 token 放 query。token 在本机生成、按用户保护保存，日志脱敏。

输入允许目录与 API 输出根由本地设置控制，默认不开放整盘；启用前必须完成配置。
拒绝 UNC、设备路径、相对路径、NTFS ADS 和路径穿越。解析绝对路径并检查每一层重解析点；
输入只读，输出只允许服务分配的 job 子目录，拒绝 Windows 保留名和非法文件名。
这些检查在使用文件时重新验证，不能只检查字符串前缀；输入源绝不覆盖。

API 输出、已完成任务历史默认不按时间自动删除；由用户在本地明确清理。
未提交媒体只由其任务事务清理，不提供“删除路径”接口，不清空用户选择的输出根。
去重映射至少保留 7 天，回收映射不删除视频和任务历史。输出元数据在文件丢失后仍可查询。

## 9. 接入示例

下面是拟议接口的 Python 标准库示例；服务实现之前不能实际调用。默认采用已经配置好的本地输出，
下载地址用于需要拷走视频的接入方，示例直接使用本地结果路径。

```python
import json
import os
import time
import uuid
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:17865/api/v1"
TOKEN = os.environ["DLSS5TOOL_API_TOKEN"]

def api(method, path, payload=None, key=None):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    if key is not None:
        headers["Idempotency-Key"] = key
    with urlopen(Request(BASE + path, data=data, headers=headers, method=method), timeout=15) as response:
        return json.load(response)

submission_key = uuid.uuid4().hex  # 调用方应保存；网络重发时复用此键和原正文。
job = api("POST", "/jobs", {"input": {"path": "D:/Videos/demo.mp4"}}, submission_key)
deadline = time.monotonic() + 6 * 3600  # 调用方自行选择总等待上限。
while job["state"] not in {"completed", "failed", "cancelled", "interrupted"}:
    status = api("GET", "/status")
    if job["state"] == "pending" and status["queue"]["state"] == "paused":
        raise RuntimeError(f"API 队列暂停：{status['queue']['pause_reason']}，需显式恢复")
    if time.monotonic() >= deadline:
        raise TimeoutError(f"停止等待任务 {job['job_id']}；服务器任务仍可能执行，可另行取消")
    time.sleep(2)
    job = api("GET", f"/jobs/{job['job_id']}")
if job["state"] != "completed":
    raise RuntimeError(f"任务 {job['state']}: {job.get('error')}")
print(job["result"]["output_path"])
```

HTTP 错误和网络重试交由调用方捕获；取消需调用 cancel 后继续轮询到终态，不能把一次 202 当成资源已释放。

API 路径版本与应用版本独立。v1 的默认处理结果语义保持固定；破坏参数、状态或结果含义的修改进入 v2。
v1 可以新增可选能力和响应字段，调用方忽略不认识的响应字段。请求始终严格拒绝未知字段，避免拼写错误被吞掉。

## 10. 与当前代码的衔接及验收

建议按 `HTTP 适配层 → TaskService → 既有导出 / worker` 实施。TaskService 持有调度、
状态、取消与持久化；HTTP 工作线程只发命令、读取状态快照，不直接操作 Tk 或原生 GPU 会话。
桌面入口也通过相同执行核心提交请求，API 字段由显式适配器转换成内部 settings / export_settings。

| 公共字段 | 现有内部语义 |
| --- | --- |
| `enhancement.style` | default / natural / cinema → style 0 / 1 / 2 |
| `enhancement.enabled=false` | 通过既有 source bypass 跳过 NR，不以四个基本滑条归零代替 |
| 增强数值字段 / 肤色开关 | 映射同名参数；关闭肤色遮罩时沿用现有有效值转换 |
| `super_resolution.scale` / `frame_generation.multiplier` | super_resolution_scale / frame_generation_multiplier |
| `optical_flow` | off → guidance_mode=0；其余 → guidance_mode=1 并选择 nvofa / raft |
| `dynamic_range` | preserve → hdr_mode=true；sdr → hdr_mode=false；源色彩来自媒体探测 |
| `encoding` | mode → rate_control；bitrate_mbps → video_bitrate_mbps；preset → nvenc_preset |

| 现有模块 | 复用 / 必须补齐 |
| --- | --- |
| `export_queue.py` | 复用 job_id、参数快照、状态恢复；新增 origin、phase、取消意图、attempt、去重映射等版本化数据 |
| `gui.py` 的 `_export_video_source` | 提取执行逻辑；默认值、进度、取消、资源预检与确认策略脱离控件 |
| `gpu_resources.py` | 保留不抢占、先释放再准入；把 GUI 状态读取适配为任务所有者的可查询快照 |
| `queue_memory_recovery.py` | 复用 2 次 / 120 秒策略与结构化错误，抽离 Tk 定时调度 |
| `parameter_contract.py` / `appearance.py` | 复用范围和语义；公共字段先严格校验，不把内部字典整个开放 |
| `device_sr_plan.py` / resident / frame_generation | 沿用真实选路、音轨 / 时间线、输出事务与有界进程退休 |

API 逻辑队列与桌面队列可以按 origin 分区，但持久化只能有一个所有者，
不能让 HTTP 和 GUI 各自读取后覆盖同一 JSON。新增状态文件需带版本并保留旧数据；
旧队列迁移失败时明确报错，不能以清空队列代替迁移。API 未启用时现有启动与导出不受影响。

非交互执行沿用资源硬限制；资源估算提示记录为 warnings，不弹对话框、不自动降质。
未知或不足的资源按既有硬性准入阻止执行，确定不兼容的组合明确失败。
恢复阶段判断、取消和输出提交必须由同一任务所有者串行仲裁。

实施验收至少覆盖以下局部场景，不要求无故运行全仓库回归：

1. 同输入 / 参数与桌面输出的像素、尺寸、帧数、HDR 和音画时间线一致；基础增强、SR、FG、光流分别覆盖受支持组合。
2. 同键并发、响应丢失重发、重启后重发均只有一项；同键不同请求冲突；未落盘不返回 202。
3. pending / running / finalizing / releasing 阶段取消与完成竞态；取消后输出和所有者状态一致。
4. 内存恢复增加 attempt、进度归零、次数耗尽暂停、普通错误不重试；资源回收失败时不能启动下一项。
5. GUI 改参数、换运行库、实时占用、桌面队列暂停与 API 队列暂停之间的隔离。
6. 重启恢复、游标过滤、未知字段、组件缺失、恶意路径、链接替换、授权、失效输出及大视频流式下载。
7. 一组代表性长队列验证新入口的内存与退出行为；短测通过不扩大为所有驱动 / 整夜认证。

## 11. 规范依据

接口文档采用 [OpenAPI 3.1.1](https://spec.openapis.org/oas/v3.1.1.html)；
202、409、410 等响应语义参考 [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html)；
HTTP 错误采用 [RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457.html)。
去重键、任务状态、队列策略和默认容量是本项目的设计约定。
