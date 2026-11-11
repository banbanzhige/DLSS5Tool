# 原生内存持续增长：最新进展与建议方案 · 2026-11-11

> **同日后续修正：** 已通过令牌权限单变量对照定位当轮沙箱快速增长的触发条件，
> 并完成普通账户 NR／VSR 各五分钟空闲、三十分钟连续处理，均通过本轮增长门槛。
> 本文每分钟数十 MiB 的数字仅代表当时诊断上下文，不能推广到普通用户运行。
> 184-byte 驱动保留独立存在，原用户完整场景仍待回验。
> 最新结论和建议以[MessageBus 令牌权限深查](NVMESSAGEBUS_TOKEN_ACL_ROOT_CAUSE_20261111.md)为准，
> 下文保留该阶段的公开源码研究、原始测量及其当时的边界。

后续已完成[任务边界回收与 NR 特征重建实测](NATIVE_WORKER_RECYCLING_20261111.md)：
空闲增长仍在，独立任务回收短测有效；参数修改和重复重建未复现所查 NeuralScreen 的显存泄漏形态。
正式／补测、原生关闭超时试运行和系统提交受背景活动干扰的窗口均分别保留。

更新器生命周期修复已先单独提交：`e5f91b8`，尚未推送。

**截至本次核查，未找到可以直接移植、并已证明解决本项目触发条件的公开根治补丁。
本机现装 621.14 驱动在当轮诊断上下文仍能复现持续增长。** 开源项目有可借鉴的诊断、资源退休和导出恢复实现，
但不能将其他项目的 VRAM 修补或“能够重启继续”当作这条 CPU 私有提交增长已经解决。

建议下一步优先做**任务边界回收与可恢复导出**，同时推进可核验的驱动／插件环境对照。
保留现有内存保护和画质合同，不再把单纯换宿主、调低 Commit 预留或清空工作集作为根治方案。

## 一、本次最新本机证据

Windows、RTX 4070 SUPER、64 GiB RAM、Python 3.13.3，NVIDIA 驱动 **621.14**。
该版官方发布日期为 2026-11-04；这不是本次安装的驱动。[官方版本页](https://www.nvidia.com/en-eu/geforce/drivers/details/283983/)

三项逐个运行，各空闲观察 300 秒；使用同一基础解释器并复用 `.venv` 依赖。
排除空闲最初 10 秒，按前后各五分之一采样的中位数及对应时间差计算斜率，
算法复用 `scripts/compare_p0_stability.py`。以下数据只取同一 PID＋创建时间的处理进程，
不合并 Python 启动器、控制进程、更新器或其他应用。

| 负载 | 前后分窗私有提交差 | 增长率 | 测量窗口更新器数 | 处理与退出 |
| --- | ---: | ---: | ---: | --- |
| CUDA 初始化＋D3D11 一次空计算，无项目处理 DLL | +289.6 MiB | **74.7 MiB/分钟** | 0 | 完成，无外部兜底回收 |
| NR 首帧后空闲，640×360 | +235.7 MiB | **61.0 MiB/分钟** | 0 | 32 帧及退出完成 |
| VSR 首帧后空闲，320×180 → 640×360 | +244.1 MiB | **63.1 MiB/分钟** | 0 | 32 帧及退出完成 |

三项的产品源码／运行库清单在各自测量期间不变；没有触发安全停止，最终无所测进程残留。
这只表示处理与退休完成。三项均明显超过既有 P0 的 0.25 MiB/分钟、16 MiB 分窗差门槛，
**内存平台验收均未通过**。不将这五分钟外推成小时级或所有显卡／驱动的结论。
监督程序每 20 秒读取一次 `nvidia-smi`；本次没有做完全无主动 GPU 查询的控制，
不能排除观察活动对驱动状态的影响，后续因果对照应增加低干预复测。

本机模块快照仍包含 `nvwgf2umx.dll`、`NvMessageBus.dll`、`nvspcap64.dll`、`nvppex.dll`。
后两项的存在不能证明对应功能启用，也不能单独证明它们导致增长；本次仍未形成有效的
“相关可选插件完全不加载”对照。未重新抓取每块分配的调用栈，不能把新驱动的全部增量
直接命名为旧版已追踪的 184 字节分配。

VSR 初次测量被旧诊断包装器的加载函数签名失配阻止，未进入有效测量。
将包装器改为透传 `_load_host` 的参数后，重新完成上述 VSR 300 秒观察；
这次仅修诊断脚本兼容性，没有改产品实现或运行库。原失败与成功记录都保留。

## 二、公开修复进展与开源参考

### 1. 精确对应的驱动线程泄漏：有复现，没有已核实的公开修复

[ArtistDeveloper/nvwgf2umx-thread-leak](https://github.com/ArtistDeveloper/nvwgf2umx-thread-leak)
提供 MIT 许可的最小复现与 WinDbg 证据，最新核对提交为 `533aff21`，仓库最后推送于
2026-08-31。作者记录 NVIDIA 已复现并转交 DirectX 团队；这是作者的状态记录。
关联公开缺陷编号为 **6672628**，未找到公开结案或明确修复版本。
[复现与状态记录](https://github.com/ArtistDeveloper/nvwgf2umx-thread-leak/blob/533aff215d351c02f2bf908c5abb041b048b2f29/README.md)
／[开发者论坛报告](https://forums.developer.nvidia.com/t/nvwgf2umx-dll-leaks-184-bytes-per-thread-in-its-dll-thread-attach-handler-595-95-580-88/381379)

它适合作为供应商沟通和驱动 A/B 的独立样本，不是一键修复工具。
旧版驱动的报告不能单独证明新版仍有同一分配栈；本机 621.14 的新实测则证明当前环境的
相同负载仍增长。因此，暂不推荐未经验证地用“升级到 621.14”作为解决方案。

### 2. Veyra：最值得参考事务续导，不能把显存保护当作本问题根治

已核对 2026-11-10 发布的 [Veyra 2.1.5](https://github.com/Likely7/Veyra-NRVideo/releases/tag/v2.1.5)
及源码 `d57ac320`，GPL-3.0。本次仅静态核对新源码，没有下载整包或执行 2.1.5；
旧记录中 Veyra 1.4.4 的实测不冒充最新版本结果。

值得借鉴的是：Worker 在设备故障前保存有效输出片段和续导位置，父进程用新 Worker
继续同一任务，限制重试次数，无法继续时保留已有片段。
[父进程续导实现](https://github.com/Likely7/Veyra-NRVideo/blob/d57ac320ca9b542226dc94fc0d6e49e3d97dd96c/src/engine/ExportJobManager.cpp#L199)
／[片段收尾实现](https://github.com/Likely7/Veyra-NRVideo/blob/d57ac320ca9b542226dc94fc0d6e49e3d97dd96c/src/engine/VideoExportJob.cpp#L365)

它的全屏 VRAM 保护明确定位为遏制，现场根因并未确认；某些保留内存仍需重启进程。
那是另一类负载，不是本次最小 CUDA＋D3D11 的私有提交证据。
[保护边界说明](https://github.com/Likely7/Veyra-NRVideo/blob/d57ac320ca9b542226dc94fc0d6e49e3d97dd96c/docs/history/releases/RELEASE_NOTES_2.0.2.md#L15)

适配本项目时，应使用已有 `VideoTimeline` 的源帧索引、原始 PTS 和音轨偏移设计检查点，
先验证 VFR、音画同步和重叠预热；不直接复制其按半帧时间计算续导位置的规则。
其 Qt 软件界面绘制调整用于另一类交换链／叠加层问题，也不能直接替代本项目的 Worker 修复。

### 3. OptiScaler NR：值得参考精确所有权和 Fence 退休

核对 `wilsjo2/OptiScaler-DLSSNR-PreSR-Multipass` 的 `6125a75b`，GPL-3.0。
它有明确的应用侧修补，如参数表来源区分、引用释放和 GPU 在途资源退休。
[参数表所有权修补](https://github.com/wilsjo2/OptiScaler-DLSSNR-PreSR-Multipass/commit/8d00d9e581b2970afddcf2eeac9c524f82210fe7)
／[GPU 生命周期实现](https://github.com/wilsjo2/OptiScaler-DLSSNR-PreSR-Multipass/blob/6125a75b37f7e2a408e31934f4827e5660429203/OptiScaler/dlssnr/DlssNr_GpuLifetime.cpp)

可借鉴“GPU 确认完成后再释放”的原则：区分活跃资源、待退休资源和已退休资源，记录其字节数、
年龄和对应 Fence。它的实现也会在提交状态无法确认时保守保留所有权，不能据此承诺零泄漏。
本项目自己掌控命令队列，可在现有队列上补记账和上限，不必照搬游戏注入侧的整套 Hook。

这些修补针对该项目自身的资源／引用问题，不能修复一个没有调用 NGX 的最小负载。
本项目 NR 已使用 AllocateParameters／DestroyParameters API，尚无证据直接套用它的
参数表补丁即可解决当前增长。本次没有移植外部代码或执行其二进制。

## 三、建议执行方案

### 优先级 1：让长任务有界、可恢复

1. **以真实进程身份观测原生占用。** 使用现有保留句柄，记录 Private Commit、阶段、
   预热基线和持续增长，区分缓存、模型正常峰值与持续增长。判断恢复时机同时考虑系统
   Commit 余量、下一阶段需求以及保存／重建的峰值，保留现有安全预留。
2. **先覆盖独立图片与队列任务边界。** 保存已完成项目，及时退休不再需要的空闲原生会话；
   内存紧张时停止新分配、排空安全在途工作、保存进度、关闭所属 Job，再从下一未完成项继续。
   绑定／回收失败继续保留所有者，禁止创建更多 Worker；恢复次数有上限。
   若进程已结束而实际提交仍未释放，停止恢复循环并保留诊断，不能把新 Worker 启动成功当作回收成功。
3. **连续视频另设门槛。** 保存源帧／PTS、已提交输出和音轨状态；需要新进程时先做输入预热，
   丢弃重复输出再续接。先验证边界像素、时域质量、帧数、VFR 和音画同步，保持原有质量阈值。
   不在活跃视频中按固定秒数无条件重置 NR 历史，也不靠降低精度或关闭功能达标。

这属于可用性保护，不是宣布驱动内存分配已经修复。建议先实施独立图片／队列部分，
再做长视频续导；避免同时改动太多媒体与时域逻辑。

### 优先级 2：形成有效环境因果对照

- 用本次三个负载和同一运行库作为基线，补充 D3D11 不初始化 CUDA 的匹配控制。
- 对可选 NVIDIA App／覆盖层／监控插件，先记录真实模块路径和版本，再安排单变量环境对照。
  只有实际模块缺席或明确功能路径证据，才能称为有效对照，不能只看开关状态。
- 对新旧环境各取短时外部线程／分配栈证据，确认 `NvMessageBus` 线程创建与原生堆增量是否同变。
  当前尚未证实哪一组件独立负责释放，不能仅凭 DLL 存在性定责。
- 有明确官方修复线索时，再选一个官方驱动或 SDK 候选做同样对照；不先下载整套环境、
  不复用来源不明的 DLL，也不把历史已失败的 CUDA preload 开关、重启服务当成新方案。

本次没有安装／卸载程序、重启服务或系统、改注册表或 NVIDIA 全局设置。
需要变更环境的阶段另行安排，当前报告已能独立用于评估下一步。

### 优先级 3：应用侧资源审计，按证据落地

沿用已修复的合成器所有权，在换尺寸、失败、清缓存和退出路径补资源代次、
Fence 完成值、参数表创建／销毁和待退休字节的观测。先定位实际不配对对象，再移植相应模式。
若是 SDK／驱动内部持续分配，避免反复重建整张图却不验证回收效果；退出所有者后的实际
提交回落仍需单独检查。关闭工作集或强行释放不明驱动内存不算修复。

## 四、验收与独立分支

- 五分钟最小对照和预热后平台沿用既有门槛，不放宽数字。
- 队列验证多图／混合尺寸、压力恢复、取消、失败重试和父进程退出；确认已有输出不丢失、
  不重复，进程树与提交额度实际退休，且不操作其他应用。
- 长视频至少完成原定长稳窗口，并增加恢复边界的像素／时域及完整时间线验收。
  短测、任务正常结束或强制进程回收，均不代替长期平台通过。

另有 2026-10-19 的 [dwm.exe／DxgK 内核池报告](https://forums.developer.nvidia.com/t/dxgk-kernel-pool-leak-in-dwm-exe-one-allocation-per-frame-inside-d3dkmtpresentmultiplaneoverlay3-rtx-3080-591-86-and-617-42/386274)：
它涉及每次呈现的内核池分配，区别于每线程用户堆和更新器堆积。若确认本程序整个进程树
退出后系统仍持续增长，应单独采样内核池／DWM；本次未复现或归因这一分支，也未执行其所提
环境修改。它不能解释本次已归属到 Worker 的私有提交曲线。

## 五、证据、重建与收尾

结果与来源固定在 [研究证据](native-memory-research-20261111/verification.json)：
三项原始样本／事件／模块／前后清单、失败的初次 VSR、固定提交的 18 份源码来源元数据，
以及本次结论使用的测量汇总。源码仅静态阅读，未下载整套开源项目或模型环境。

新注册任务目录并设置 TEMP/TMP 后，使用基础 Python 解释器，PYTHONPATH 指向现有
`.venv/Lib/site-packages`，运行下列命令；首次 VSR 需要本次诊断包装兼容性修补。

```powershell
python -B scripts/verify_p0_stability.py --task tmp/<任务> --case native-device11 --device-compute --device-cuda --frames 32 --idle-seconds 300 --timeout 420 --label cuda-d3d11
python -B scripts/verify_p0_stability.py --task tmp/<任务> --case native-nr --frames 32 --idle-seconds 300 --timeout 420 --label nr
python -B scripts/verify_p0_stability.py --task tmp/<任务> --case native-sr --frames 32 --idle-seconds 300 --timeout 420 --label vsr
```

任务目录 `tmp/memory-growth-research-20261111/`，磁盘新增峰值预算小于 128 MiB，
起始 F 盘约 47.1 GiB 可用；复用现有依赖。小型证据归档与校验后只清理本任务可重建产物，
保留任务登记至 2026-11-18 复核，准确清理与剩余占用记入 `verification.json`。
原有五份 AGENTS／发布文档修改继续保留，不混入本次提交。
