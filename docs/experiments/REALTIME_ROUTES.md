# 深度／光流实时路线与调参缓存实验 · 2026-09-08

## 结论

目前没有一条完成测试的路线达到 **整片稳定30fps**。用户提出的“只改DLSS参数复用引导”成立，
且比降低模型质量更值得优先集成：独立实验保留Large模型、720引导和原始RAFT，
缓存命中后约25fps（核心处理），修改强度后的243帧与重新推理结果全部一致。

本轮只新增独立实验脚本、测试和本文；**没有修改生产引擎、GUI、用户设置、模型目录或部署组件**。
测试结果不会自动让当前程序加速。不宣称“已实现正式调参缓存”或“无损实时30fps”。

## 方法与环境

- RTX 4070 SUPER 12GB，Torch 2.8.0+cu128。用户程序保持运行，未关闭其他进程。
- 初始整卡占用约8GB，后续各独立测试前约4.3–4.5GB；不是独占GPU或锁频测试。
  `*-gpu-before.txt`保留后期逐项开跑前的采样，不能代表全程峰值。
- 竖屏原片 `minimaxH3_00008-audio.mp4`：1088×1920，243帧，24fps。
  方形原片 `9月1日.mp4`：1440×1440，165帧，30fps。源文件未改。
- 与生产共用 `guidance_worker.Models` 和当前原生v2 DLL，但模型在独立测试进程内调用，
  **不含冻结组件IPC、GUI、音频和真实导出按钮流程**。
- 深度SDPA＋AMP FP16；RAFT仍6次完整输出，未使用已撤回的final-only优化。
- 竖屏方向固定为用户保存的`forward_negated`，方形全片另测`backward`。
- 后期受控配置：Torch/OpenCV CPU线程均4、v2 merged提交、同步单帧。
  默认线程／compatibility的先导数据另存，不混为同一A/B。
- 抽测每片首／中／尾三个12帧连续窗口，每窗口前3帧不计时，共27计时帧；完整保留36帧结果。
  原始基线在最后重新运行，RGB输出完全一致，速度8.27→8.64fps，说明性能仍有波动。
- 全片吞吐测试先预热4帧，再从0帧重新连续处理；计时含全部源帧与切镜，不丢帧、不插帧。
  “解码＋核心fps”专用测试不做哈希、画质差分、视频写出。P95是核心process调用时间，
  不是端到端显示时延。其他验画质实验的循环总时间含哈希／编码，不能混用为纯处理性能。

## 路线对照：竖屏连续窗口

| 路线 | 核心fps | process P95 ms | 说明 |
|---|---:|---:|---|
| Large 720＋原始RAFT | 8.27 | 128.06 | 基线，双Stream |
| Large 512＋原始RAFT | 13.04 | 82.89 | 引导缩小，输出仍1088×1920 |
| Large 384＋原始RAFT | 15.64 | 74.42 | 不到实时 |
| Small 512＋原始RAFT | 15.12 | 71.12 | 需额外Small权重 |
| Small 384＋原始RAFT | 18.04 | 60.72 | 光流成为主要负担之一 |
| Small 384＋RAFT AMP | 16.80 | 64.14 | 此配置未见提速，不能默认启用 |
| Large 720＋RAFT AMP | 9.75 | 109.37 | 有数值变化，不同轮收益不稳定 |
| Large 720＋输入/特征复用 | 9.51 | 109.68 | 实验串行路径；不是同调度纯特征消融 |
| Small 384＋NVOFA | 26.56 | 38.82 | 最接近目标，仍不足30 |
| Small 512＋NVOFA | 23.64 | 45.91 | 同样未达目标 |

报告：`output/realtime-routes-portrait-controlled/report.json`。
先导`portrait-r0`中Small三项因下载中断无法加载；保留失败日志，重试校验权重后在r1和受控轮成功，
不能把失败项当作模型兼容问题。r0/r1不能替代后期受控结果。

## 全片吞吐（含解码，不含GUI／IPC／编码）

| 输入 / 路线 | 解码＋核心fps | 核心fps | 核心P95 ms |
|---|---:|---:|---:|
| 竖屏 Large720原始RAFT | 8.73 | 9.06 | 121.90 |
| 竖屏 Small384原始RAFT | 16.76 | 17.96 | 62.23 |
| 竖屏 Small384 RAFT AMP | 13.64 | 14.55 | 95.32 |
| 竖屏 Small512 NVOFA | 21.77 | 23.89 | 49.02 |
| 竖屏 Small384 NVOFA | 22.48 | 24.82 | 48.61 |
| 竖屏 Small256 NVOFA | 22.28 | 24.79 | 51.32 |
| 方形 Large720原始RAFT | 5.14 | 5.26 | 201.93 |
| 方形 Small384 NVOFA | 16.65 | 18.24 | 63.16 |
| 方形 Small512 NVOFA | 18.15 | 19.74 | 58.87 |

降到256也没有继续明显提速，说明该原型的CPU前后处理／原生上传与DLSS等开销已不能忽略。
此为观察，不是GPU内核时间线归因。竖屏最后复测Large720为7.95fps（含解码），再次说明背景波动。

## NVOFA探针及质量边界

使用NVIDIA公开API 2.0头文件对应的CUDA接口、驱动自带`nvofapi64.dll`，没有另装替换DLL。
输入灰度，4×4输出网格，SLOW质量档；有符号S10.5除以32转换，矢量已是输入像素单位，
放大网格不能再乘4。关闭temporal hints，以便独立帧对／seek可重现。
当前原型同步上传两张图、同步回读，**不是全GPU零拷贝**。

512×384已知平移(+8,+4)测试20个计时样本：均值0.89ms，P95 1.08ms；
内部ROI平均端点误差0.0011像素，反向中位数(-8,-4)，无NaN/Inf。
这是合成光流步骤结果，不是整链路或真实复杂遮挡的精度。

真实竖屏窗口相对原始RAFT的每8像素采样光流差异：Small384 NVOFA平均端点差约1.47个输出像素；
不是对真实光流的误差。最终RGB平均绝对差约0.97/255，最大51/255。
降分辨率、AMP和特征复用均可能改变最终输出，不作无损承诺。
Small和Large在相同光流配置下最终输出相同，不等于深度预测相同或所有素材都可关深度。

人工查看基线与Small384 NVOFA的第121帧完整图，未见显著结构破坏；
**未完成连续播放盲测／时序画质验收**，不以静态图片通过替代此前抖动问题的验证。
全片硬件光流输出视频成功解码243帧：
`output/realtime-routes-portrait-hardware-video/small384_nvof/normal.mp4`（无音频）。

## 调参缓存：已证实可复用，但尚未生产集成

当前生产`Live.update()`在仅改外观时一般保留模型会话，但`_prepare_guidance()`仍每帧调用推理。
GUI的最终画面缓存键包含style/intensity等，所以调参后缓存失效并重复跑引导。
输出混合／对比显示不在核心设置哈希中，已经是另一类后处理，不能笼统说所有控件都必须重跑DLSS。

实验缓存保存：低分辨率float32原始深度预测＋有方向的帧对光流。
**不直接缓存归一化后的深度当作任意时序都可用的结果**：1/99百分位后的平滑边界依赖之前的帧，
回放仍按相同reset／切镜规则顺序更新。缓存命中不跳过这一状态推进。

243帧原片第一遍483次模型调用（243次深度，240次光流；首帧及自动切镜跳过光流），
约806.6MiB原始缓存，不是全尺寸float32引导约5.7GiB。模型实例内固定配置、RGB内容哈希键，
帧对有序；反向帧对不串用。当前字典只适合有界实验，没有生产磁盘缓存或LRU上限。

优化版（跳过缓存命中后的模型输入预处理、4线程＋merged）结果：

| 过程 | 核心fps | 核心P95 ms | 命中/模型调用 |
|---|---:|---:|---:|
| 首遍构建缓存 | 7.59 | 142.92 | 0 / 483 |
| 相同参数重渲染 | 25.48 | 45.46 | 483 / 0 |
| 强度1.0→0.55，重渲染 | 24.98 | 47.99 | 483 / 0 |
| 改参后再重复一次 | 24.36 | 48.96 | 483 / 0 |
| 0.55强度，禁用缓存重新推理 | 8.08 | 138.55 | 0 / 483 |

全部243帧：相同参数回放与冷缓存输出哈希一致；0.55强度的缓存回放、重复回放、
禁用缓存重新推理的输出哈希一致；光流与深度数组哈希也全部一致。
这是同一进程会话内的全片验证，不是持久缓存跨进程／跨版本保证。
缓存的25fps是核心process吞吐，不含哈希比较、编码、GUI；不能写成端到端25fps。

### 保留的未通过实验

`output/realtime-cache-portrait-indexed/`额外尝试源文件哈希＋帧号键、缓存每帧深度百分位，
核心约26–28fps，但`warm_same`和`warm_changed_repeat`最终输出哈希未全部一致。
前者不同帧为144–242（99帧），后者为84–132（49帧），帧号从0开始。
光流／深度数组逐帧仍一致，禁用缓存重新推理与`warm_changed`一致。
尚未定位是原生宿主时序／初始化／其他状态原因，不能归因于模型误差，也不能删除失败记录后宣称无损。
该变体未验收，不作为推荐部署方案。

追加三个连续窗口复测`output/realtime-cache-indexed-repeat-windows/`：缓存核心约31.1–31.2fps，
36帧的重复／改参／禁用缓存对照全部一致。但短窗口不能推翻上述完整视频失败，
也不含解码／GUI／IPC，因此仍不算稳定30fps达标。

### 正式缓存建议

1. 独立于最终DLSS画面缓存，由视频会话／独立缓存管理器持有；改DLSS参数只清最终画面。
2. 深度键：源内容身份、帧身份、真实输入预处理/色彩空间/尺寸、权重哈希、架构、精度、后端版本。
   光流键还含**有序两帧身份**、方向、迭代数、光流算法与相关设置。
3. 深度／光流分开失效：只换深度模型，不必丢弃仍兼容的光流缓存；反之亦然。
4. 跳转不必清空整片原始预测，但要重置DLSS历史与深度平滑，按真实相邻关系查询光流；
   缺帧、切镜、模式切换、失败恢复不能继续使用过期状态。
5. 低分辨率缓存、内存LRU＋可选磁盘层；共享总内存预算，不能在现有8GB画面缓存外无上限追加。
   不用FP16压缩冒充数值无损。磁盘写入须有版本、校验、原子完成标记和清理入口。
6. 模型进程重启、原片换内容、预览缩放／超分改变模型实际输入时重新验证命名空间；
   不以“同一路径”或“同帧号”判断可复用。

## 文件与复现

- `scripts/realtime_routes_probe.py`：独立路线、全片吞吐与缓存实验；`--output`必须新目录。
- `scripts/nvof_probe.py`：驱动光流API和方向／单位探针。
- `scripts/realtime_routes_summary.py`：汇总已保存数据并实际解码检查输出视频。
- `tests/test_realtime_routes_probe.py`：CPU缓存复用、跳转归一化、逆序帧对、内容变动、引导配置失效、
  禁用缓存控制、源身份隔离、百分位缓存测试。
- `output/realtime-routes-summary.json`：汇总，包括失败项与一致性检查，不能只摘最高fps。

Small权重只下载到`tmp/realtime-routes-20260908/`，没有放进`mods/models`。
大小99,218,434字节；SHA256与作者发布页一致：
`715fade13be8f229f8a70cc02066f656f2423a59effd0579197bbf57860e1378`。

官方来源：[Small模型](https://huggingface.co/depth-anything/Depth-Anything-V2-Small/blob/main/depth_anything_v2_vits.pth)、
[NVIDIA API头文件](https://github.com/NVIDIA/NVIDIAOpticalFlowSDK)、
[NVOFA编程指南](https://docs.nvidia.com/video-technologies/optical-flow-sdk/nvofa-programming-guide/index.html)。

```powershell
tmp/guidance-cuda-env/Scripts/python.exe scripts/realtime_routes_probe.py --source "原片.mp4" --output output/routes-new --threads 4 --submission merged --variants large720 small384 small384_nvof --full --speed-only
tmp/guidance-cuda-env/Scripts/python.exe scripts/realtime_routes_probe.py --source "原片.mp4" --output output/cache-new --threads 4 --submission merged --variants large720 --cache --full --no-reference
.venv/Scripts/python.exe -m unittest tests.test_realtime_routes_probe -v
```

完整测试限制原片最多600帧，避免误把长片缓存实验变成无界内存占用。
本轮未测试TensorRT／torch.compile、CUDA↔D3D12零拷贝或新生产GUI缓存；这些不是已完成成果。

回归：基础环境全量250项，247通过、3项Torch专用测试按设计跳过。
本轮新增8项CPU缓存测试均通过；NVOFA真实驱动平移／反向验证另行执行并保存JSON。
生成的5个测试视频均实际解码为243帧。测试文件与生产代码分离，未提交或替换发布包。
