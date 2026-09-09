# xFormers / SDPA 本机对照 · 2026-09-08

后续已完成可选SDPA＋FP16组件集成及原片完整链路测试，见 [DEPTH_ACCELERATION.md](DEPTH_ACCELERATION.md)。本文保留当时独立实验的环境和计时口径。

## 结论

在本机 RTX 4070 SUPER、Torch 2.8.0+cu128 下，xFormers 没有表现出
相对 SDPA 的明确整模型优势。FP32 两者深度步骤均约269ms；深度 AMP FP16
下约87–90ms。优先考虑 SDPA＋深度 FP16 的后续集成，但本轮仅测试，没有部署。

保持 FP32 只换注意力后端，实验核心链路吞吐约提高8%；
高效注意力＋深度 FP16 的核心链路吞吐约为基线2倍。
这不是冻结组件／GUI／完整导出实测，不能将表中 fps 直接当作正式预览帧率。

## 环境与隔离

- GPU：RTX 4070 SUPER 12GB，compute capability 8.9；驱动616.64。
- Python 3.13.3；Torch 2.8.0+cu128；xFormers 0.0.32.post2，wheel 构建 Torch 2.8.0+cu128。
- xFormers 通过 `uv --target --no-deps` 安装在 `tmp/xformers-probe-pypi`，没有升级现有 Torch。
- PyTorch 下载源中途停滞，终止本次下载后，从 PyPI 安装同一版本成功。
  `tmp/xformers-probe-deps` 是首次未完成下载的目标，不是可用环境。
- 使用原有 `tmp/guidance-cuda-env/Scripts/python.exe`，仅在测试子进程添加上述依赖目录。
- 不改生产引擎／模型模块或第三方源码文件。测试脚本在子进程内绑定 attention forward，
  FP32 vanilla 明确调用原 Attention.forward，xFormers 明确调用已有 MemEffAttention.forward，
  SDPA 使用相同 QKV 权重、一次缩放、eval dropout=0。
- 深度保留 FP32 权重，AMP 只包裹深度 forward；输出回 FP32 再进入原有 CPU 深度归一化。
  RAFT 全程保持 FP32、6次更新、原反向估计；每帧光流哈希与基线一致。
- 矩阵乘法 TF32 关闭、cuDNN TF32 开启，与原环境一致。
- 当前部署组件、用户参数和 DLSS DLL 的 SHA-256 前后未变。

## 输入与计时口径

用户原片 `9月1日.mp4`，165帧，1440×1440，实际引导长边720；
RAFT输入720×720，深度输入714×714。
取三个连续窗口0–10、77–87、154–164；每段显式 reset，前3帧预热、后8帧计时。
每个方案一轮24个计时帧；主对照两轮反向顺序运行，每方案48样本。

输入仅解码一次，保存为同一份 `inputs.npy`；每个方案一个独立进程。
模型仍为 RAFT-Large＋Depth Anything V2 Large；外观参数固定。
引导模型与原生 DLSS 放在同一测试进程，绕过冻结组件／主程序 IPC，只对核心处理作比较。

- **深度GPU时间**：CUDA events，包含深度 forward 及末端 FP32 转换，不含其 CPU 预后处理。
- **深度步骤时间**：原 guidance_worker 计时，包含输入准备、模型、同步回读及 CPU 归一化／放大。
- **核心整帧时间**：原生 Live.process，含光流、深度和 DLSS 的上传／执行／回读。
- 解码、加载、每段预热、哈希、差分分析、保存图片、单独 profiler 都不在计时内。
- 不含 GUI、进程 IPC、编码、音频或完整导出。
- GPU 不是独占锁定环境；同样的光流步骤不同运行约83–93ms，反映了运行波动。
  不把几毫秒或几百分点差距解读为稳定优势。

## 两轮主对照（round 2 / 3）

| 深度后端与精度 | 深度GPU ms | 深度步骤 ms | 核心整帧 ms | 核心 fps | Torch峰值分配 MiB |
| --- | ---: | ---: | ---: | ---: | ---: |
| 原普通注意力 FP32 | 294.88 | 308.83 | 425.62 | 2.35 | 2568 |
| xFormers FP32 | 255.13 | 268.89 | 392.42 | 2.55 | 2363 |
| SDPA FP32 | 254.84 | 268.75 | 394.75 | 2.53 | 2363 |
| xFormers AMP FP16 | 75.73 | 89.72 | 217.10 | 4.61 | 3067 |
| SDPA AMP FP16 | 74.40 | 87.35 | 202.03 | 4.95 | 3067 |
| xFormers AMP BF16 | 72.86 | 86.32 | 206.97 | 4.83 | 3067 |
| SDPA AMP BF16 | 77.31 | 90.37 | 209.78 | 4.77 | 3067 |

注意 FP16 两种方案核心整帧差距约15ms，但光流波动就占约9ms，
不能据此宣称 SDPA 的注意力算子稳定快7%。深度步骤实际上只差约2.4ms。

先导测试（round 0）普通注意力＋深度 AMP FP16：深度步骤223.18ms，
核心整帧349.66ms。它说明单独 AMP 有收益，而高效注意力与 AMP 的组合收益更大；
这是单轮辅助结果，不混入上述主对照平均数。

Torch显存是**两个引导模型合计**的分配峰值，不含 D3D12、上下文和其他应用。
AMP 在这个保留 FP32 权重／开启转换缓存的实验中，峰值分配比基线增加约499MiB，
不是“启用半精度必定省显存”。保留峰值：普通FP32 3050MiB，融合FP32 3318MiB，AMP 3818MiB。
未改权重存储精度或单独分析峰值归因。

## 实际内核

独立 profiler 保存于每组 `attention-operators.json`，不计入测速：

- xFormers FP32 和 SDPA FP32：24次 `aten::_efficient_attention_forward`，
  PyTorch `fmha_cutlassF_f32_aligned_64x64_rf_sm80`。
- SDPA FP16/BF16：24次 `aten::_scaled_dot_product_efficient_attention`，
  对应 CUTLASS f16/bf16 内核；本 Windows 构建并未自动用 FlashAttention。
- xFormers FP16/BF16：24次包含 `flash::FlashAttnFwdSm80` / `enable_sm80_to_sm89`
  的融合 CUDA 内核；已安装包的 fa3F 支持最低 compute capability 8.0。
  不是“xFormers 只能跑 CUTLASS”，也不是“安装了 FA3 就一定比 SDPA 快”。
- `xformers.info` 显示 Triton 未安装、FA2 不可用，但本任务使用的路径实际执行成功。
  不为未使用的内核再增装依赖。

## 数值与画质边界

主对照14次运行共336计时帧：没有 NaN/Inf；光流逐帧哈希与 FP32 基线一致；
最终 DLSS 输出也与基线逐像素一致。

深度图本身**不**逐像素一致，归一化0–1深度相对普通 FP32 的误差：

| 方案 | 平均绝对误差（全帧均值） | 最大单像素误差 |
| --- | ---: | ---: |
| xFormers / SDPA FP32 | 0.00000758 | 0.000958 |
| xFormers FP16 | 0.000287 | 0.06797 |
| SDPA FP16 | 0.000294 | 0.07743 |
| xFormers BF16 | 0.001530 | 0.19136 |
| SDPA BF16 | 0.001561 | 0.19635 |

BF16 深度平均误差约为 FP16 的5倍，最大局部误差也更大；这里不建议为几毫秒先选 BF16。
已人工查看保存的中段 FP32／SDPA FP16 深度缩略图，结构相近；不是全片盲测。

**保留异常，不作无损承诺：**先导 round 1 的 SDPA BF16 出现过第一段8帧最终输出差异，
全24帧平均8位RGB误差0.2353，最大29；后续同配置两轮未复现。
该运行保存在 `sdpa_bf16-r1/result.json`，未删除。
尚未确定由原生宿主初始化／运行波动或其他因素引起，不能归因于某一个后端。
全部先导及主对照共23次运行，不能说所有运行输出都一致。

这个片段上大量深度变化未反映到 DLSS 输出，与前序“深度收益未证实”的观察一致；
但不能证明深度在所有视频或 DLL 路径下都无效，也不能只凭最终输出一致认定半精度普遍无损。
正式集成还需冻结组件、完整链路、更多人物运动／遮挡／切镜／纹理素材回归。

## 原始资料与复现

- 主汇总：`output/attention-ab-20260908/summary.json`。
- 主运行：`output/attention-ab-20260908/suite-r2.json`；先导：`suite-r0.json`、`suite-r1.json`。
- 每组 `result.json` 包含逐帧时间、误差、光流／最终输出哈希，后续运行亦含深度哈希。
- `input.json` 包含实际帧号、输入数组 SHA-256；`reference-*.npy` 保存基线深度与输出。
- `scripts/attention_benchmark.py` 为独立测试脚本，不被生产组件导入。
- `scripts/attention_summary.py` 为无 Torch 依赖的汇总脚本。

使用新的输出目录（同一主对照一次跑两轮）：

```powershell
tmp\guidance-cuda-env\Scripts\python.exe scripts\attention_benchmark.py suite --output output\attention-new --deps tmp\xformers-probe-pypi --source "原片路径.mp4" --rounds 2 --variants vanilla_fp32 xformers_fp32 sdpa_fp32 xformers_fp16 sdpa_fp16 xformers_bf16 sdpa_bf16
.venv\Scripts\python.exe scripts\attention_summary.py output\attention-new --rounds 0 1
```

运行会占用GPU，需避免同时导出。没有改动部署组件；用户当前预览不会因本次测试自动变快。
