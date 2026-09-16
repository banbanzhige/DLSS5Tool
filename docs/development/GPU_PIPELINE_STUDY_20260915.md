# GPU 常驻与整帧搬运研究 · 2026-09-15

## 结论与边界

用户在读取 TODO 后授权将六类方向推进为独立实验。本轮有真实硬件原型与对照数据，
**不是六项生产接入完成，也不是新版本发布验收**。不修改默认设置、模型、精度、正式 DLL、
免安装包或更新基线；不提交、推送、上传。继续使用同一条 DLSS 时间历史，保留 CPU 完成等待，
未引入多段渲染、外部 semaphore、默认 NVOFA 替换或自动参数推荐。

最值得下一步投入的是 **DLSS 输出→NVENC** 和 **DLSSG 生成帧→NVENC** 的显存传递。
**VSR 直连、HDR GPU 算术和深度 GPU 后处理未过一致性门槛，不接入默认路径。**
RAFT 上一帧输入常驻与硬解码不能笼统承诺收益。

## 本机与测量口径

- RTX 4070 SUPER，驱动 616.92，Torch 2.8.0+cu128 / CUDA 12.8；使用现有 CUDA 环境。
- RAFT 使用已有 torchvision large FP32 权重、6 次更新；深度隔离使用已有 Depth Anything V2 vits FP32。
- 单 GPU 实验依次运行；计时包含同步。预热后交叉 A/B，报告保留逐帧耗时、哈希和 reset。
- `raft-portrait-forward.json` 的后半段与完整单测执行重叠，**其性能数字作废**；用
  `raft-portrait-clean.json` 的无本任务单测重叠重测替代。原始记录保留，不择优宣传。
- 各项口径不同，不能相加收益。算子耗时、FFmpeg 子进程总耗时、进程间流水线耗时分别说明。
- 时间只代表本机短测；不代表长视频、多 GPU、HDR 全链路、驱动恢复、缓存和 GUI 验收。
- `measurements_completed` 只表示测量完成。HDR/深度的 `promotion_gate` 明确为拒绝，不能读成通过。

## 1. RAFT 上一帧输入显存复用

实现：`scripts/gpu_pipeline_candidates.py::ResidentInputs`。只保留最近帧对，用输入张量的强引用
与对象身份匹配；准备好的 CPU 张量必须不可变，模型实例/设备固定。只在实验 harness 中替换
`_infer_flow`，应用不导入候选。继续使用原 CPU resize/LUT 归一化，没有改变模型算术。

| 场景 | 基线→候选，ms/源帧 | 一致性与判断 |
| --- | --- | --- |
| 1440×1440，RAFT 512，32 帧，3 轮 | 55.377→55.114 | 原始输出光流和最终 DLSS 哈希均一致；仅约 0.26 ms，收益很小 |
| 1080×1920，RAFT 720，64 帧，forward_negated，16 MiB 原始缓存，3 轮干净重测 | 67.939→68.059 | 哈希均一致；没有测得速度收益 |

计时为同进程真实 RAFT＋现有 CPU 光流输出路径＋真实 DLSS，不包括解码、编码、GUI 或正式跨进程 GPU 光流桥。
每轮 64 帧、2 个 reset 时，从 62 次帧对推理的 124 张上传，减少到候选计数 64 张上传，
但减少传输次数并不保证整条链路更快。当前不为此增加生产状态与生命周期复杂度。

证据：[方形冷运行](gpu-pipeline-20260915/raft-square-cold.json)、
[竖屏干净重测](gpu-pipeline-20260915/raft-portrait-clean.json)。

## 2. DLSS 输出直接交给 NVENC

原型链路：D3D12 DLSS output → shared DEFAULT buffer → 同卡 CUDA external-memory import →
NVENC 已注册 CUDA 指针。只返回压缩数据；每帧编码完成并 unmap 后才允许 D3D12 再写同一缓冲。
保留 D3D12 CPU fence 等待，不是全异步，也不是零次 GPU 内复制。

基线使用**相同编码器、相同图像和相同编码参数**，区别只在 DLSS 输出先回 CPU，再上传。
候选与基线使用同样的编码输入缓冲。逐帧原始输出哈希、整条压缩码流 SHA256 均相同；
FFmpeg 重新解码检查帧数通过。

- 320×180 / 24 帧仅验证功能，不用小尺寸波动宣传收益。
- 1440×1440 / 32 帧 / 3 轮：**16.453→12.900 ms/帧**，减少约 3.55 ms（耗时减少 21.6%）。

**重要限制：**使用 H.264 P5 low-latency、固定 QP19、30 fps、无 B 帧/无 lookahead 的实验编码策略，
不是应用的 P5 HQ CQ/VBR。NVENC 接收 ABGR 并完成自身颜色转换，不等于当前 FFmpeg 的 RGB→YUV
转换。与相同策略的 CPU-bounce 基线相同，不代表已与生产编码画质/色彩策略等价。
未接音频、容器、HDR、预览、最终画面 RAM 缓存或混合/输出视图。正式接入必须先补这些合同，
不能把原型的编码设置覆盖用户配置。

证据：[真实 DLSS→NVENC](gpu-pipeline-20260915/nvenc-direct-square.json)。

## 3. VSR 超分→DLSS 共享显存

已实现两个进程、两个同卡 D3D12 device 间的 shared buffer。句柄从实际生产者 PID 复制，
核对 LUID；VSR 写完后 CPU 等 fence，DLSS 完整消费后才发下一请求。SDR 直接复制像素，
**HDR 明确拒绝**：现有 VSR 的 R10G10B10A2→RGBA16F 特定舍入尚未迁移。

试验过程必须区分：

1. 最早初始化失败是候选 DLL 不在运行库旁边，NGX 没有正确的 feature 搜索路径；
   显式 `FeatureCommonInfo.PathListInfo` 指向只读正式运行库后解决。不归因于显卡不支持或同进程冲突。
   调用现有 `ProcessSuperResolution(320,180,2)` 的独立基线也验证可初始化。
2. 最初 CPU 对照通过 pipe 传完整输出，得到约 97→48 ms 的大幅差距；**不能作为应用收益**，
   因为应用已经使用共享 RAM。该组只作为传递可行性/重复性证据。
3. 改为与应用相同的共享 RAM 输入/输出、`np.copyto` 输入和独立输出副本，再与共享 VRAM 比较。
   4K 竖屏 2160×3840、24 帧首轮：48.426→35.508 ms，但候选第 6–11 帧输出哈希不同；
   **立即中止该组，不采用这两个耗时宣称合格优化**。
4. 另一组同时取消 DLSS 输出回读，在第 10–11 帧出现差异，也单独拒绝；小尺寸通过不能抵消大尺寸失败。

随后共享 RAM 原路径的 24 帧、3 轮重复性控制全部一致（约 50.8–51.7 ms/帧）；
640×360 共享 RAM 对照的直连也一致（约 3.92→3.83 ms/帧），不能抵消 4K 候选的失败。
证据：[共享 RAM 控制](gpu-pipeline-20260915/vsr-sharedram-control.json)、
[小尺寸直连](gpu-pipeline-20260915/vsr-sharedram-small.json)。

此处尚未证明差异来自 VSR 输出、DLSS 内部重复性还是时序变化。新增诊断会改变调度，
因此不能用加上回读后“恰好通过”替代原候选验收。下一步应隔离预先保存的相同 VSR 输出、
检查 DLSS 输入纹理与输出，而不是删掉等待或放宽哈希门槛。

证据：[共享 RAM 的关键失败](gpu-pipeline-20260915/vsr-4k-sharedram.json)、
[叠加取消回读的失败](gpu-pipeline-20260915/vsr-share-portrait4k.json)、
[旧 pipe 基线的输入直连对照](gpu-pipeline-20260915/vsr-4k-input-only.json)。

## 4. HDR 分析、混合与深度后处理

使用独立 Torch GPU 算子，没有改应用计算。对照 NumPy/OpenCV 原函数，不以视觉“差不多”代替一致性。

### HDR

1920×1080、固定随机半精度 RGBA 输入，只做算子隔离：

- PQ 分析：CPU 中位约 556 ms，GPU 常驻约 2.44 ms；8,294,400 通道值中 10 个不同，最大 1/255。
- PQ 混合 0.7：CPU 中位约 794 ms，GPU 常驻约 3.67 ms；7,488 个半精度通道不同，最大 0.00048828125。
- HLG 分析与混合也有差异。即便数量很小，分析图仍可能改变后续光流，不直接接入。
- mix=1 的端点输出一致；CPU 原函数仍在早返回之前做全帧 FP32 转换。这提示可独立研究
  **无效 CPU 转换的提前跳过**，不必为端点另造 GPU 算子。本轮没有改该函数。

这些是合成输入的算子耗时，不是用户视频导出加速倍数；GPU 常驻计时不含上传/回读。
报告另列部分 CPU→GPU→CPU 计时，没有把它混成“全程留卡”。

证据：[HDR 算子对照与拒绝门槛](gpu-pipeline-20260915/hdr-post-1080.json)。

### 深度

使用真实 vits 原始预测，比较百分位、跨帧平滑范围、归一化、放大。12 帧中 GPU/CPU
百分位和平滑范围一致，最终放大结果不一致：最大约 1.59e-5；大量通道虽差异很小但不逐位相同。
候选约 1 ms，CPU 约 3 ms，仅指后处理，不含模型/上传给 DLSS。

再用原 CPU 归一化数据隔离 resize，并尝试复用光流研究的 double→float 坐标构造，
4 帧仍有差异，不能把此前光流的验证直接套在单通道深度与 Torch 实现上。
**未进入最终 DLSS 渲染比较，未打通深度显存直连，不能报告该项已完成。**

证据：[真实深度后处理](gpu-pipeline-20260915/depth-square.json)、
[坐标隔离失败](gpu-pipeline-20260915/depth-coordinate.json)。

## 5. NVDEC 解码常驻

复用本机 FFmpeg 7.1.1 full。比较软件解码与 `-hwaccel cuda -hwaccel_output_format cuda`：
把同一输出统一为 NV12/P010 后的逐帧 MD5 相同；实际 NVENC 编码到 null muxer，检查编码帧数。
没有下载媒体或产生长视频。

| 素材 / 帧数 | 软件解码→NVENC | GPU 解码→NVENC | 判断 |
| --- | --- | --- | --- |
| 1440×1440 SDR / 96 | 0.574 s | 0.587 s | 没有收益 |
| 320×180 HDR / 24 | 0.226 s | 0.223 s | 短片启动开销下差异很小 |
| 4K 素材 / 96 | 1.460 s | 1.350 s | 约省 0.11 s（7.5%），只代表此编解码子链路 |

时间包含 FFmpeg 启动，不能按每帧外推长片；本轮尚未将 NVDEC 表面接进 DLSS/CUDA 预处理。
NV12/P010→RGBA、范围/矩阵/传递函数、时间戳与 seek 都是下一步合同，不能只改硬解参数就声称消除了上传。

证据：[4K](gpu-pipeline-20260915/codecs-4k.json)、[SDR](gpu-pipeline-20260915/codecs-square.json)、
[HDR](gpu-pipeline-20260915/codecs-hdr.json)。

## 6. DLSSG 生成帧→NVENC

在原 worker 上加 **编译期 `GPU_PIPELINE_PROBE` 开关**；正常构建不带该宏，协议不变。
实验变体支持 SDR 2×，输出由 D3D12 shared buffer 跨进程交给 CUDA/NVENC，命令回执替代整帧回传。
编码完成后父进程才发送下一帧，保留每次 GPU 完成等待，不更改当前的 NVOFA 传输或引导默认。

输入为真实解码图片、**固定零运动向量**，专门隔离输出传递；不是插帧质量或光流准确性验证。
两路径都保留颜色/运动的 CPU 输入管道。24 输入帧含两个 reset，双方同样不编码 reset 的生成结果，
检查其余 22 个生成帧；不代表应用完整的原帧＋生成帧混合导出。

- 小尺寸 320×180：3.918→4.403 ms/输入帧，反而慢。
- 1440×1440 / 24 帧 / 2 轮：**24.950→19.698 ms/输入帧**，约省 5.25 ms（21.0%）。
- 两路径的有效性标志、原始生成画面哈希、压缩码流均相同，重解码 22 帧通过。
- 使用第 2 项同一隔离编码策略，不包含 HDR、3×/4×、声音、完整输出时间线和真实光流时序验收。

最早探针错误地只查 `runtime/nvngx_dlssg.dll`，而本机生产解析实际回退到仓库 SDK 目录；
复用 `frame_generation.runtime_files()` 后成功，没有复制或替换运行库。

证据：[较大画面](gpu-pipeline-20260915/dlssg-output-square.json)、
[小尺寸反例](gpu-pipeline-20260915/dlssg-output-runtime-path.json)。

## 后续优先级

1. 先将两条已证明可行的编码前直连，补到与应用**相同**的编码配置和颜色转换，再做连续真实光流、
   HDR、时间线、音频、预览/缓存兼容测试；保留 CPU 回退，仍不默认开启。
2. VSR 先定位 4K 差异；HDR/深度先解决数值合同，不将算子测速变成画质通过。
3. 硬解只在真实解码瓶颈素材上继续；RAFT 输入常驻暂缓生产接入，收益不足以优先增加状态。
4. 原 TODO 的光流显存 LRU、异步 fence 与替代光流没有在本轮混合实施。

## 验证、复现与收尾

独立脚本均要求已登记 `TASK.md` 和新的报告标签；不覆盖旧报告。Python 使用 `-B`，编译 TEMP/TMP
指向本任务。原始 JSON 保留每轮信息；失败报告也归档，不能仅拿成功短测作为交付证据。

主要入口：

```powershell
$env:TEMP='F:\project\DLSS5Tool\tmp\gpu-pipeline-20260915'
$env:TMP=$env:TEMP
$env:PYTHONDONTWRITEBYTECODE='1'
cmd /c scripts\build_gpu_pipeline_probe.bat tmp\gpu-pipeline-20260915
tmp/guidance-cuda-env/Scripts/python.exe -B scripts/gpu_pipeline_probe.py --help
.venv/Scripts/python.exe -B scripts/gpu_color_probe.py --help
tmp/guidance-cuda-env/Scripts/python.exe -B scripts/gpu_nvenc_probe.py --help
tmp/guidance-cuda-env/Scripts/python.exe -B scripts/gpu_dlssg_probe.py --help
.venv/Scripts/python.exe -B scripts/gpu_codec_probe.py --help
```

本轮全部临时产物位于 `tmp/gpu-pipeline-20260915`；256 MiB 新增预算，没有复制 Torch、模型或运行库。
2026-09-16 完成归档收尾：清理 80 个已归档/可重建文件，保留约 0.78 MiB 的失败复现依赖与登记。
NVENC 唯一下载为 NVIDIA 官方 VPF 仓库的 MIT 许可头文件（282,466 B，API 12.0），SHA256：
`067c51597e586b72ced8127b2456f48dfe3a9f7d7e0d00dcbb26f9535c05b4ad`。
复现前将同一文件放到已登记任务目录，校验后再编译；不需要下载整套 SDK。
[头文件来源](https://github.com/NVIDIA/VideoProcessingFramework/blob/master/src/TC/third_party/nvEncodeAPI.h)。

完整单测运行 577 项：1 失败、2 错误、8 跳过。3 个问题均位于未修改的
`tests/test_release_updates.py`：测试构建 v2.2.1 假发行目录，部分校验却读取当前已包含 v2.2.2 的
真实策略，触发 `Required baseline coverage differs from update policy`。本轮没有删除策略项、
改打包代码或削弱门槛来使测试通过。相关源码和策略的 git diff 为空；应单独修复测试隔离/历史校验问题。

正式 `runtime/dlssnr_host_v2.dll` 仍为
`c8ad631f8f78b2dedec6aec418c7a570d6fc5bf1b9ffa9f3514bcb0edd58fc13`。
小型对照与最终临时清理结果记录在本目录的 `gpu-pipeline-20260915/VERIFICATION.md`。
