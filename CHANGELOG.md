# 更新日志

本文记录 DLSS5Tool 面向用户可见的版本变化。

## v1.1.0 — 2026-09-04

这是从 v1.0.3 升级而来的 HDR 与兼容性版本。核心变化是真正打通 HDR10/PQ、HLG 的 16-bit 浮点 Neural Rendering 链路，同时改善参数输入、宿主格式切换和 RTX 30/40/50 系运行时选择。

### 新增：HDR10 / HLG 高精度处理

- 自动读取视频的像素格式、色彩范围、色域、矩阵和传递特性，识别 `smpte2084`（HDR10/PQ）与 `arib-std-b67`（HLG）。
- HDR 视频不再先转换为 8-bit SDR：FFmpeg 直接解码为归一化 RGBA16F，经过 Feature 18 后再编码为 10-bit HDR。
- v2 原生宿主新增 `DXGI_FORMAT_R16G16B16A16_FLOAT` 输入/输出合同，并在创建 Feature 18 时设置 `NVSDK_NGX_DLSS_Feature_Flags_IsHDR`。
- 显式提交 `DLSS.Pre.Exposure=1.0` 与 `DLSS.Exposure.Scale=1.0`。
- PQ/HLG 的原图与处理结果在解码后的线性光中混合，避免直接混合传递编码值造成亮度错误。
- HDR 导出使用 HEVC Main 10、10-bit 4:2:0，并保留 BT.2020、PQ/HLG 和 limited-range 色彩标签。
- 优先使用 `hevc_nvenc`；不可用时自动回退 `libx265` Main10。
- HDR 预览单独执行 SDR tone-map，不改变最终 HDR 导出信号。
- 新增「HDR10 / HLG 高精度处理」开关。检测到 HDR 源时默认启用，并自动锁定严格单会话导出。
- 主机或 FFmpeg 不满足高精度合同条件时，界面会显示明确的检测结果和降级原因。

### 宿主与数据通道

- `dlss_engine.py` 新增 `rgba8` / `rgba16f` 帧格式，以及 sRGB、scRGB、HDR10/PQ、HLG 色彩配置。
- 隔离宿主进程和共享内存支持 NumPy `float16` 输入/输出；RGBA16F 每帧使用 8 bytes/px。
- 帧格式或色彩合同变化时自动替换隔离工作进程并重建 D3D12 资源，避免复用不兼容资源。
- HDR/scRGB 强制使用 v2 宿主，不会静默回退到仅支持 RGBA8 的旧宿主。
- 原生宿主新增 `dlssnr_configure_format` 导出，并根据当前格式动态计算上传、回读 row pitch。
- 修复 `native_host_v2/build.bat` 在括号块中过早展开 `vswhere` 路径的问题，Visual Studio 工具链发现更可靠。

### 导出与媒体处理

- 新增 `ffprobe` 色彩检测；没有 `ffprobe` 时可从 FFmpeg 输入信息降级识别 PQ/HLG。
- 新增高精度 HDR 解码器、HEVC Main10 写出器及 NVENC 能力探测。
- HDR 导出沿用现有源音轨直通/AAC 兼容转换逻辑。
- HDR 源关闭高精度模式时，会明确 tone-map 为 SDR 后走现有 RGBA8/H.264 路径。
- SDR 视频和图片继续沿用 v1.0.3 的处理方式，不受 HDR 开关影响。

### 参数与界面体验

- 强度、本地色调、本地结构、皮肤结构和输出混合滑条从 5% 提升为 1% 步进。
- 每个滑条增加可编辑数值框，支持 `0.73` 或 `75%` 等输入方式；输入会自动校验、取整并限制在 `0.00–1.00`。
- 参数关闭时数值框同步禁用，但仍保留上次设置，重新开启后恢复。
- 皮肤结构强度为 `0` 时现在是真正的 no-op：不会仅因为开关打开而额外启用自动皮肤蒙版。
- HDR 状态采用固定的内联提示区域，直接显示当前源是 SDR、HDR10/PQ、HLG，或正在使用的兼容降级路径。
- README 增加效果对比原图和完整 HDR/DLL 使用说明。

### RTX 30 / 40 / 50 系 DLL

- RTX 30 发行包由来源不明的修改版 `310.8.0.0` 更新为经过社区 Ampere 集成验证的 `310.8.SF-v2` FP16 版本。
  - 文件版本：`310.8.SF.0`
  - DLL SHA-256：`6EB209E764F39872625DEBD6ABAF45E2BB6322F6F270F781F70C059AE30B3927`
  - CUDA 架构：`sm_75 / sm_86 / sm_89 / sm_120`
- RTX 40 默认 DLL 保持不变。RTX 4070 SUPER 实测中，它与 SF-v2 逐帧输出完全一致，速度也处于同一水平；另一份 RHI RTX40 修改版略慢。
  - DLL SHA-256：`CEB6432F6FBDF44D886014BCD47241932BF8B67439FEEF9BBDD0961436662650`
- RTX 50 继续使用 NVIDIA 签名的正式 `310.8.0.0`，这是当前最新的 `nvngx_dlssnr.dll`。
  - DLL SHA-256：`E16BCF15E16E13F527491CDF7845B2FE6521A738D8F7C9C721866A8496E1FC8E`
  - CUDA 架构：`sm_120`
- 2026-09-03 发布的 `310.9.0` 属于 DLSS Super Resolution、Ray Reconstruction 和 Frame Generation，不是本项目使用的 Neural Rendering DLL。

### 开发与验证

- 新增 HDR 元数据分类、PQ/HLG 传递函数、线性光混合、tone-map 和 RGBA16F 共享内存测试。
- 新增 `scripts/hdr_probe.py`，可在真实 NVIDIA 硬件上验证 RGBA16F Feature 18 与 HDR 编码链路。
- 49 个单元测试全部通过。
- v2 原生宿主在 Visual Studio 2022 工具链下重新编译通过。
- RTX 4070 SUPER 实机验证：
  - SDR RGBA8 Feature 18 回归通过；
  - HDR10/PQ RGBA16F 处理通过；
  - HLG RGBA16F 处理通过；
  - PQ 输出确认为 HEVC Main 10 / `yuv420p10le` / BT.2020 / SMPTE ST 2084；
  - HLG 输出确认为 HEVC Main 10 / `yuv420p10le` / BT.2020 / ARIB STD-B67；
  - 内置 FFmpeg 的 `zscale`、H.264/HEVC NVENC 和 libx264/libx265 回退路径验证通过。
- PyInstaller v1.1.0 便携版构建与 ZIP 完整性检查通过。

### 从 v1.0.3 升级

- 可以直接把 v1.1.0 解压到新目录运行；不建议覆盖旧版完整目录。
- 现有设置会继续读取，新加入的 HDR 高精度开关默认开启。
- RTX 40 用户直接使用主便携包，不需要替换 DLL。
- RTX 30 用户请用 v1.1.0 Release 的 `30系.zip` 覆盖 `_internal\nvngx_dlssnr.dll`。
- RTX 50 用户请用 `50系.zip` 覆盖 `_internal\nvngx_dlssnr.dll`。

### 已知限制

- HDR 高精度路径当前只用于带正确 PQ/HLG 标签的视频；静态 HDR 图片尚未接入。
- 普通显示器中的 HDR 预览是便于调参的 SDR tone-map，不是母版监看输出。
- 当前保留基础 HDR10/HLG 色彩标签，不复制 Dolby Vision、HDR10+ 动态元数据或源文件的 mastering-display / MaxCLL SEI。
- HDR 固定使用严格单会话；并行分段仅用于 SDR。
- RTX 30 的 SF-v2 路径计算成本较高。后续可考虑加入低模型分辨率与残差回填模式，在画质和速度之间提供选择。
- 本次 RTX 30 DLL 已通过架构、哈希以及 RTX 40 兼容性/HDR 探针检查，但仍需要 RTX 30 用户完成最终 Ampere 实机回归。

### 参考与上游

- [RankFTW/rhi-repo Releases](https://github.com/RankFTW/rhi-repo/releases)
- [DLSS NR 310.8.SF-v2](https://github.com/RankFTW/rhi-repo/releases/tag/dlssnr-310.8.SF-v2)
- [DLSS NR 310.8.0](https://github.com/RankFTW/rhi-repo/releases/tag/dlssnr-310.8.0)
- [SAOG0721/Magpie](https://github.com/SAOG0721/Magpie)
- [faisalkindi/DLSS5oneclick](https://github.com/faisalkindi/DLSS5oneclick)
