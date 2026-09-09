# CUDA 引导落地与实测（2026-09-08）

后续数据搬运优化已完成：见 [docs/guidance/GUIDANCE_TRANSPORT.md](GUIDANCE_TRANSPORT.md)。下表保留旧管道组件的历史基准，不能当作新共享内存链路的测速。

## 实现

- 独立 CUDA 组件：PyTorch 2.8.0+cu128、torchvision 0.23.0+cu128、CUDA 12.8。
  版本组合来自 [PyTorch 官方历史版本说明](https://pytorch.org/get-started/previous-versions/#v280)。
- 继续使用已有 RAFT-Large 与 Depth Anything V2 Large 权重，不需要“GPU 专用模型”。
- 输入／权重为 FP32，未启用 AMP、未新增 TF32 开关；长边720、RAFT 6次更新不变。
- 自动／GPU 模式必须确认 CUDA 可用；CPU 只能主动选择。主程序也校验握手设备，拒绝旧组件的静默 CPU 回退。
- CUDA OOM 提供明确提示，不自动换模型、改分辨率或切 CPU。
- 摘要区分构建类型；实际设备通过模型进程 → DLSS 宿主 → GUI 回传，首次处理写入日志；详情显示最近确认设备。
- 增加推理分项计时和 Torch 显存峰值。计时包含输出同步及 CPU 后处理，不是纯 GPU kernel 时间。
- 206 项测试通过；中英文浅／深色20张界面截图无回调错误。实际冻结主程序＋冻结GPU组件联调通过，含基础无组件、缺组件报错、混合模式、重置与错权重拒绝；测试 PATH 中没有 Python。

## 小样本 CPU／GPU 对比

1280×720 合成移动画面；引导长边720；同一权重、同一尺寸；预热后计时6帧。
不含 DLSS 与编码，包含引导 IPC。首个计时帧也包含第一次实际光流计算，不能视为充分预热的稳定态基准。

| 模式 | CPU 平均秒/帧 | GPU 平均秒/帧 |
|---|---:|---:|
| 光流 | 1.609 | 0.106 |
| 深度 | 5.663 | 0.145 |
| 深度＋光流 | 7.524 | 0.300 |

原始数据：`output/gpu-upgrade-cpu-baseline/report.json`、`output/gpu-upgrade-cuda-baseline/report.json`。
CPU 使用原验证组件，GPU 使用本轮冻结组件；CPU 旧组件不提供分项计时，不能把其0值当作实际耗时。

## 10秒导出测试

RTX 4070 SUPER 12GB，驱动616.64。1280×720、30fps、300帧合成输入，单任务。
链路为冻结引导组件＋隔离进程 DLSS v2＋NVENC H.264，输出均成功解码出300帧。
输入由已有测试图片生成，不含真实源视频解码／音频处理，不是用户原片的复测。

| 模式 | 初次准备/预热 | 300帧处理与编码 | 合计 |
|---|---:|---:|---:|
| 关闭引导 | 2.28秒 | 3.83秒 | 6.12秒 |
| 仅光流 | 7.58秒 | 32.50秒 | 40.08秒 |
| 仅深度 | 12.43秒 | 67.64秒 | 80.06秒 |
| 深度＋光流 | 12.05秒 | 84.29秒 | 96.34秒 |

原始报告和视频：`output/gpu-upgrade-10s-export/`。
GPU 引导显著快于 CPU 验证包，但这套 Large＋FP32＋720 配置仍不是实时播放方案。
测试时有约6GiB其他 GPU 占用，后台活动也会影响结果；不要直接套用到不同分辨率、帧率或显卡。

## 显存与体积

- 光流：Torch 分配峰值约328MiB，保留峰值470MiB。
- 深度：分配峰值约1701MiB，保留峰值1994MiB。
- 混合：分配峰值约1828MiB，保留峰值2012MiB。
- 上述仅为引导模型进程，不含 DLSS、CUDA上下文等全部开销。混合测试整卡采样峰值约8975MiB，包含后台占用，1秒采样也可能漏掉短时峰值。
- 本轮 CUDA 组件约4.43GiB（解压后，不含权重）；基础包仍不包含 Torch、引导工作程序或权重。

## 使用与维护

本机已部署 GPU 组件到根目录 `mods/enhancement`，部署后默认路径 GPU 推理复验通过；原 CPU 组件保留到
`tmp/guidance-cpu-backup-20260908`，`mods/models` 不变，不修改用户保存的参数。
重启开发版，设备保持自动或GPU；日志应显示 `NVIDIA GeForce RTX 4070 SUPER`。
基础测试包：`tmp/gpu-base-20260908/DLSS5Tool-v2.0.1`。

这仍是本机验证组件，不是完成分发审查的公开发行包。其他显卡／驱动组合未实测，
完整上游许可归档仍待补齐（前序官方许可证下载被审批服务阻止，未绕过）。

复测脚本为 `scripts/guidance_benchmark.py` 和 `scripts/mods_smoke.py`。
GPU 构建环境在 `tmp/guidance-cuda-env`，主项目 `.venv` 没有安装 Torch。
