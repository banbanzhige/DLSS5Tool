# README 光流对比实测 · 2026-09-10

本实验使用当前仓库 `mods/enhancement` 冻结组件，不替换模型、DLL 或用户保存设置。README 的 07 / 08 / 09 是维护者提供的原图与 DLSS 分界、RAFT 光流、NVOFA 光流截图；截图缺少完整帧对与参数元数据，仅作示意，不用于数值评分。

## 结果

NVOFA 1×1 在两段素材上整段耗时减少 31.5–38.3%，光流阶段约快 4.4–5.0 倍。质量代理指标有取舍，不能据此判定任一后端全面更好。实验当时默认后端未变；随后维护者保留 RAFT 默认后端，并将 NVOFA 备选网格设为 1×1。

| 素材 / 后端 | wall 中位数 | 三轮 wall 范围 | 有效 fps | 光流阶段 ms | process P95 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| 方形 / RAFT | 17.08 s | 16.57–17.85 s | 9.66 | 31.21 | 70.71 |
| 方形 / NVOFA 1×1 | 10.54 s | 10.25–10.76 s | 15.65 | 6.19 | 40.31 |
| 竖屏 / RAFT | 20.77 s | 20.64–21.49 s | 11.70 | 23.21 | 65.68 |
| 竖屏 / NVOFA 1×1 | 14.23 s | 13.75–14.62 s | 17.08 | 5.29 | 41.09 |

实际分析尺寸：方形两路都是 512×512，竖屏两路都是 288×512。12 份 MP4 的所有帧均完整解码，ffprobe 确认均为 H.264 / `h264_nvenc`，没有混用 CPU 编码器。测试前后用户设置 SHA-256 相同。

| 素材 | 光流回投 MAE：RAFT / NVOFA | 共同有效区域占全图 |
| --- | ---: | ---: |
| 方形 · 39 对 | 3.099 / 2.592 | 92.74% |
| 竖屏 · 39 对 | 3.469 / 3.652 | 93.16% |

NVOFA 回投误差在方形素材约低 16.4%，在竖屏约高 5.3%；这不是增强画质改善或退化的百分比。

| 素材 / 固定参考光流 | 无光流 DLSS | RAFT 引导 DLSS | NVOFA 引导 DLSS |
| --- | ---: | ---: | ---: |
| 方形 / RAFT 参考 | 1.848 | 1.560 | 1.610 |
| 方形 / NVOFA 参考 | 1.758 | 1.516 | 1.424 |
| 竖屏 / RAFT 参考 | 1.743 | 1.446 | 1.478 |
| 竖屏 / NVOFA 参考 | 1.678 | 1.394 | 1.391 |

上表为增强时序残差，越低表示该固定回投下增强变化更小。两种引导相对无光流基线均降低此代理误差，但后端间排名依赖参考场。不能因此给出最终画质总分。

公开数据：[README_FLOW_BENCHMARK_20260910.json](README_FLOW_BENCHMARK_20260910.json)，包含每轮 wall、光流耗时、初始化握手、素材/组件哈希、窗口帧号及质量汇总；不含原片或个人路径。`git_head` 是当时工作树的基点，不代表未提交工作树的全部状态；实测使用的冻结组件、运行库与宿主以 SHA-256 为准。

## 测试条件

- Windows 11，RTX 4070 SUPER（12 GB），驱动 616.64；GPU 非独占。
- 两个现有本地 SDR 视频：方形 1440×1440 / 165 帧、竖屏 1088×1920 / 243 帧。公开结果只保留源文件 SHA-256，不发布个人路径或原视频。
- 分析长边 512；RAFT-Large 原权重、6 次更新、FP32；NVOFA SLOW、**1×1 网格**、temporal hints 关闭、当前→上一帧。
- 网格 1×1 对应测试开始时的用户保存设置，**实验当时程序默认为 4×4，随后已采用 1×1 作为默认值**。此次不混用旧 4×4 实验结果，也不据此修改默认后端。
- 仅光流、串行执行、缓存 0、严格禁止后端回退；DLSS v2、原尺寸、SDR RGBA8 / sRGB、style=0、intensity/local_tone/local_struct/skin_struct/output_mix=1、auto_mask 开启；不启用深度或超分。
- `guidance_execution=raft_streams` 在仅光流模式也会规范化为串行，因此此次显式 serial 与该模式的实际执行策略相同。未复刻 GUI 的全部显示、颜色管理与导出队列路径。

## 速度口径

通过真实 `ProcessLive → GuidanceSession → 冻结 worker` 运行整段视频，没有替代或注入模型输出。每片每后端独立运行三轮，顺序为 RAFT→NVOFA、NVOFA→RAFT、RAFT→NVOFA，报告三轮中位数与范围。

- **整段 wall**：从处理循环前到编码完成，包括视频解码、首帧 worker 加载、光流、DLSS、跨进程传输、色彩转换、逐帧 SHA-256 与视频编码。不包括独立 preflight、ProcessLive 构造、编码器预检、收尾 seek/reset 和成片完整解码检查；不处理音轨。不是从点击导出到文件完成的 GUI 总等待时间。
- **有效 fps**：帧数 / wall 中位数，不是播放帧率或稳定实时吞吐保证。
- **光流阶段 ms**：worker 的 `flow_ms`，含输入准备、推理、回读、恢复到原图尺寸，不含 DLSS、worker 外部传输、解码或编码；不是纯 GPU 内核耗时。每轮排除前 8 帧和未执行光流的 reset 帧后取均值，再取三轮均值的中位数。wall 不排除任何帧。
- **process P95**：每帧同步 `ProcessLive.process` 的耗时第 95 百分位，再取三轮中位数。
- 所有成片逐帧解码并核对帧数。可解码不代表画质等价。

## 质量口径

速度全部测完后另起诊断，不将指标计算开销混入速度。每片取开头、中间、结尾各 16 帧，每窗口独立 reset，剔除前 3 帧；无额外切镜 reset 时得到 **39 对**。方形起始帧为 0 / 74 / 149，竖屏为 0 / 113 / 227，均为零基编号。

1. **光流回投 MAE**：用各后端的当前→上一帧位移回投上一帧 RGB，再与当前帧比较。两后端共用相同掩码：排除 24 像素边界，并取两个光流均采样不越界的交集；没有用各自“可靠区域”分别挑选有利像素。先按每对有效像素取平均，再对帧对取平均。
2. **增强时序残差**：`R_t = DLSS_t - 原片_t`；计算 `mean(abs(R_t - warp(R_(t-1), reference_flow)))`。分别固定 RAFT 和 NVOFA 两套参考光流，每套参考对无光流 / RAFT / NVOFA 三种 DLSS 输出使用同一个掩码、同一个回投。避免只用各自模型评价各自成片。
3. 全部指标来自编码前 RGBA8 数组与真实 worker 的 float32 光流，不从有损 MP4 或彩色光流截图反算。

以上误差单位为 8-bit RGB 灰度级，越低只表示该代理误差更低。真实视频没有真值光流或真值增强画面，因此不报光流 EPE，也不把相对原片的 PSNR / SSIM 当增强画质。回投仍受光照、遮挡、模糊与压缩影响；时序残差还可能偏好较弱增强或过度平滑。两套参考都不是独立真值，不能把误差下降百分比写成“画质提升百分比”。未进行人工连续播放盲测，不能断言闪烁、拖影、遮挡处理或最终观感等价。

## 复现与数据

从项目根目录执行，使用全新输出目录；需要当前冻结组件、RAFT 权重、兼容 GPU / 驱动和可用 DLSS 宿主。基础 Python 环境无需另装 Torch，推理由附加组件执行。GPU 测速时不要并行运行其他推理。

```powershell
.venv\Scripts\python.exe -B scripts/readme_flow_benchmark.py speed --output output/readme-flow-new --source "<方形原片>" "<竖屏原片>" --grid 1 --edge 512 --repeats 3
.venv\Scripts\python.exe -B scripts/readme_flow_benchmark.py quality --output output/readme-flow-new
.venv\Scripts\python.exe -B scripts/readme_flow_benchmark.py summary --output output/readme-flow-new
.venv\Scripts\python.exe -B -m unittest tests.test_readme_flow_benchmark tests.test_flow_quality_probe
```

本地本次结果位于 `output/readme-flow-20260910/`：每轮报告包含逐帧耗时、worker 指标和编码前输出哈希；每个 quality 子目录包含编码前 `.npy`、同量程光流图、抽样输出和逐帧对指标。该目录被 Git 忽略，README 不链接这些不可随仓库发布的文件。质量数组约占 2.4 GB 磁盘，另需预留视频与截图空间。
