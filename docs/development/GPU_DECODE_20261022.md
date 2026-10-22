# 强制 GPU 视频解码（2026-10-22）

## 行为

- 普通视频默认要求 NVIDIA NVDEC。维护者后续补充“允许 CPU 解码兜底（较慢）”开关，默认关闭；关闭时不支持的素材停止并提示先转码，开启后才允许在开始解码时改用 CPU。
- SDR 使用现有 PyAV 18.1.0，显式设置 `allow_software_fallback=False`，并检查实际解码上下文。AV1 单独指定支持硬解的原生解码器，避免依赖默认选择 libdav1d。
- HDR 使用现有 FFmpeg 色彩链，要求 CUDA 硬件帧经过 `hwdownload`；硬解初始化失败后，软件帧无法通过该链路。
- 超分导出前的真实帧时间戳扫描也使用 GPU 解码，不再用 ffprobe 对整段素材做软件解码。ffprobe 仍读取流元数据。
- 图片和图片序列继续使用读图接口；它们没有视频压缩码流，不能通过 NVDEC 解码。

## 关键实现

- `dlss5tool/gpu_decode.py`：硬解要求、AV1 显式解码、帧读取、精确定位和有限关键帧索引。索引最多保存 4 个素材、每个 4096 个关键帧的整数位置，不保留历史画面。
- `dlss5tool/image_sequence.py`：统一普通视频入口；并行导出协调器与子进程也使用该入口。
- `dlss5tool/video_export.py`：HDR 强制硬解，保留既有色彩转换、浮点帧协议及按帧裁切。
- `dlss5tool/gui.py`、`shared_render_preview.py`：导入与后台读帧失败可见；解码器打开失败也能发送结束标记，避免导出线程永久等待。
- `requirements.txt`、`packaging/DLSS5Tool.spec`：PyAV 18.1.0 成为视频解码所需依赖，不再仅随可选 GPU 编码组件收集。

## 验证

本机：RTX 4070 SUPER，驱动 617.42，PyAV 18.1.0，外部 FFmpeg 7.1.1。真实硬解测试使用 320×180、24 帧合成素材；CPU 解码仅作为测试参考，不是生产回退。

| 素材 | 验证结果 |
| --- | --- |
| H.264 8-bit 4:2:0 | 24 帧 BGR 与旧路径逐像素一致 |
| H.265 8-bit 4:2:0 | 24 帧 BGR 与旧路径逐像素一致 |
| AV1 8-bit 4:2:0 | 显式 GPU 解码；24 帧 BGR 与旧路径逐像素一致 |
| HEVC Main10 PQ / HLG | 两类各 24 帧 RGBA16F 与原色彩链逐像素一致 |
| 变帧率 H.264 | 24 帧像素一致，扫描保留真实时间线，按帧定位一致 |
| 90° 旋转 H.264 | 24 帧尺寸、方向和 BGR 像素与旧路径一致 |
| H.264 10-bit（本机硬件不支持） | 打开时明确失败，不转为软件解码 |

四类 SDR 测试均对第 0、10、3、23、1 帧进行往返定位。关联模块的 382 个唯一 XML 测试身份最终通过，未运行全仓回归。初次的 Tcl 初始化失败单独重跑通过；一个既有队列测试补充固定 RAM 预算的桩，避免把机器瞬时可用内存误当显存策略结果。

验证记录在 `docs/development/gpu-decode-20261022/`：`decode-matrix.json`、`rotation.json`、`test-summary.json` 和 `export-smoke.json`。

完整 GUI 缓存→超分→编码探针被既有显存预检拦截。当时本机剩余显存不足 1 GiB，报告中没有产生帧或遗留模型子进程；不据此宣称完整导出已通过，也不承诺处理 FPS 或长视频耗时改善比例。

## 实测中发现并处理的情况

1. OpenCV 请求 D3D11 并报告加速类型为 D3D11，仍能在本机用不支持的 H.264 10-bit 素材读出画面；只检查加速标记不足以保证没有软件回退，因此未采用这一方案。
2. PyAV 自动 AV1 解码器为 libdav1d。新增显式 GPU AV1 上下文，并在硬件协商前恢复码流像素格式，否则该版本会出现 `get_buffer()` 失败。
3. FFmpeg 自动使用较多 CPU 解码线程时，NVDEC 可能申请超过允许数量的解码表面。硬解读取固定为一个解析线程，避免该初始化失败。
4. 第一批 HDR 合成素材缺少传递函数标签，重新生成包含明确 VUI 的 PQ/HLG 素材后进行验收；未把错误标签素材当作 HDR 通过证据。

## 范围与限制

- 本次强制的是压缩视频的解码。帧仍下载到系统内存，颜色转换、帧搬运以及既有 NGX 上传/回读仍有 CPU 参与，不是整条流水线零回读。
- 定位使用已验证的关键帧位置；首次定位到尚未建立索引的区间时，按 GPU 实际解码计数，不把平均 FPS 猜成变帧率帧号。这类首次远距离定位可能较慢。
- 未复现反馈者的未知原编码与长视频，不将其原始素材猜为 AV1。
- 转为 GPU 解码不等于修复已有原生工作进程内存增长；本次没有改变失败成果保全或自动串行分段策略。

## 重建命令

先在仓库 `tmp/<任务>-<日期>/TASK.md` 登记临时目录与容量；探针拒绝重解析点和覆盖既有报告。每轮短片新增远低于 100 MiB，复用现有依赖。

```powershell
.venv/Scripts/python.exe -B scripts/probe_gpu_decode.py --task F:/project/DLSS5Tool/tmp/gpu-decode-20261022 --label fresh-validation
.venv/Scripts/python.exe -B -m pytest tests/test_gpu_decode.py tests/test_frame_generation.py::TimingTests tests/test_processing_limits.py -q
.venv/Scripts/python.exe -B scripts/profile_cached_export.py --task F:/project/DLSS5Tool/tmp/gpu-decode-20261022 --label fresh-export --frames 8 --width 320 --height 180 --scale 2 --case sr
```

## 临时目录收尾

本任务生成的短视频、测试目录与原生临时日志已清理，报告归档到开发文档目录。清理时按 F 盘前后可用空间记录净增 3,813,376 字节（同期其他程序写入可能影响该数值）；临时目录仅保留 880 字节的 `TASK.md` 登记，2026-10-29 复核。后续局部检查的小型可重建缓存也按同一任务边界清理。


## 维护者补充：保留 CPU 解码兜底开关

- 导出设置增加“允许 CPU 解码兜底（较慢）”，默认关闭并记忆用户选择。导入失败提示同时给出 H.264 8-bit、4:2:0 和 H.265 Main10、4:2:0 的转码建议。
- 开启后仍先尝试 GPU，能硬解的素材继续硬解；只有启动、首帧失败时才切换 CPU。输出帧后发生错误仍停止，不从中途切换解码器。
- 普通视频的 CPU BGR 路径复用原 OpenCV 软件解码与色彩转换，HDR 复用原 FFmpeg 高精度色彩链。增强、超分仍在 GPU 执行。
- 开关应用于导入、原片预览、时间戳扫描、增强、超分和并行任务；运行中的导出保留任务开始时的选择。切换开关会释放已有原片预览解码器，并更换共享渲染、时间戳缓存身份。
- 日志与渲染诊断记录实际解码方式；后台预读通过消费线程写界面日志，不从解码线程访问 Tk。
- 能否硬解由实际启动结果决定，没有按 H.264 10-bit 做全局禁用。验收探针也已允许该素材在支持的显卡上成功解码。

### 补充验证

- 本机 H.264 High10 SDR 24 帧：严格模式拒绝；开启兜底后实际为 CPU，逐帧 BGR 和 5 次往返定位均与原 CPU 路径一致。
- H.264 High10 PQ 24 帧：GPU 拒绝后 CPU 兜底，RGBA16F 与原 HDR 色彩链逐像素一致，时间戳扫描也记录 CPU 后端。
- 已支持的 H.264 素材开启兜底后仍实际使用 NVDEC。
- 开关、设置持久化、队列快照、缓存身份、首帧回退、HDR、界面与内存关联检查，共 324 个唯一 XML 测试身份最终通过。未运行全仓回归。
- 组合测试中有 Tcl 初始化、错误帧释放检查失败，相关用例单独重跑通过；HDR reader 的新增首帧缓冲缺省字段已修复。第一次 CPU 探针发现 PyAV 与旧 OpenCV 在 10-bit 色度转换上有差异，兜底因此恢复使用原 OpenCV BGR 路径，最终逐像素验证通过。
- 小型报告：`fallback-high10.json`、`fallback-hdr.json`、`fallback-tests.json`；首轮探针失败也保留在 `fallback-first-probe.json`。不将短片验证视为长视频内存增长已解决，也未重新运行已被显存预检拦截的完整超分探针。

补充实卡探针：

```powershell
.venv/Scripts/python.exe -B scripts/probe_gpu_decode.py --task F:/project/DLSS5Tool/tmp/gpu-decode-20261022 --label fresh-fallback --case high10
```
