# 内存保护与缓存协调实施记录 · 2026-10-26

## 结果

用户授权的[优化清单 M1～M9](RAM_HEADROOM_OPTIMIZATION_CHECKLIST_20261026.md)已完成源码实现、代表性本机采样及定向验收。正式包仍须按独立的发行工作流构建；本轮交付是源码和证据。

修复前的直接触发条件和默认设置核对见[反馈排查](RAM_HEADROOM_FEEDBACK_20261026.md)。现场评论尚无诊断包，因此本轮证明的是已复现策略冲突得到修复，不把所有用户的内存问题归为同一原因。此前 VSR/NR 长期原生增长继续独立追踪。

## M1～M9 对账

| 项目 | 完成内容 | 证据或落点 |
| --- | --- | --- |
| M1 估算纠错 | 按实际启用的 VSR/NR、解码预读、有效槽数和 FG 输出尺寸估算；HDR 1 倍排除 VSR。推荐量用于预警，强制准入使用必要新增需求和系统保护余量。NR/VSR 初始化只登记自身资源，已经创建的共享缓冲不再次当作待分配字节；VSR worker 复用既有帧时不再另要求两帧新增空间。 | `super_resolution.py`、`gui.py`、`export_preflight.py`、`frame_generation.py`；估算和自定义输出尺寸用例。 |
| M2 处理额度 | 共享账本独立管理待分配和已获准额度；分配后消费额度，退出作用域归还。竞争者不能同时花掉同一份工具内预算；崩溃所有者回收，清理未完成的启动所有者保留额度到实际退役。 | `shared_cache_budget.py`、`memory_budget.py`；并发、真实 spawn 崩溃及失败所有者保留用例。 |
| M3 协调回收 | 分配前请求回收，等待代次确认，绕过 250 ms 缓存重新读取 OS 余量，再准入。回收有截止时间和取消检查；分配、回调和跨进程等待均在账本锁外。 | 暂停中的子进程、旧账本兼容及账面释放而 OS 余量未恢复的测试。 |
| M4 处理优先 | 分层渲染区分当前帧/重建当前所需历史与后台预取，等待中的当前任务优先；GUI 暂时缩短额外预取窗口，各缓存插入入口遵守处理额度。回收保护正在等待的目标帧。 | `render_cache.py`、`guidance_cache.py`、`gui.py`；优先级、回填阻止和目标帧保护用例；真实缓存导出。 |
| M5 空闲恢复 | 普通空闲压力先请求回收，持续 5 秒仍不足时退役；严重压力立即退役。回收后至少等待 0.5 秒且预留后余量达到 256 MiB 才恢复额外预取。停止命令继续可用，原生调用及写出不会自动重跑。 | 空闲严重/短暂压力、恢复间隔及真实 worker 退役用例。 |
| M6 诊断与提示 | 区分物理限制、可提交量限制、保护预留、回收超时、额度竞争和空闲退役；保存失败瞬间快照、阶段、必要/推荐需求、预留、额度和缓存情况。NR/VSR 子进程错误转发来源 PID 及结构化数据；GUI 报告包含最近 64 条事件和当前策略，通用导出失败结果保存相关事件。 | 中英文文案、`memory_events()`、GUI 诊断及 `result.json`。 |
| M7 定向验收 | 308 项不同用例均有通过记录，原基础环境跳过的 2 项模型缓存用例在既有 Torch 环境通过；Tk 初始化失败用例独立通过。真实缓存导出、处理和编码比较见下文。 | [去重验收清单](ram-coordination-20261026/verification-summary.json)。 |
| M8 实际峰值 | 一台本机、7 类实际负载，调整前后各一次；另补最终 FG 一次。50 ms 采样，覆盖初始化、处理及退出，共 15 次有界处理采样。 | [保留的阶段峰值样本](ram-coordination-20261026/phase-peak-samples.json)及各 summary。 |
| M9 新门槛 | 最低 2 GiB，按 RAM 的 1/8 增长，封顶 4 GiB；物理和当前进程可提交量分别计算可用空间并保留限制原因。413 组模拟边界及调整后全部 7 类处理通过，帧哈希与调整前相同。 | [门槛候选与边界矩阵](ram-coordination-20261026/final-policy-policy-evaluation.json)。 |

## 兼容与记账规则

- 原缓存映射继续使用 `D5CACHE2` 的字节布局，处理额度/代次放在独立的 `-processing-v3` 映射。旧冻结模型 worker 通过原缓存预算字段收到收缩目标，新代码保存的配置上限不变。
- 新缓存所有者显式确认回收代次；旧 worker 由其账本实际占用是否收缩到目标来确认，然后仍须通过新 OS 快照。实际采样观察到 `guidance_worker.exe`，未重建或替换该现有组件。
- 已分配缓冲体现在 OS 快照中；待分配额度在实际分配后扣减。逻辑缓存占用不等于实际已释放物理内存，仍被引用的数组或分配器保留的页面不能当作已回收空间。
- 零新增分配的控制/复用检查不排在需分配的等待者后面，避免等待者与当前 worker 相互等待。严重压力保护仍有效。
- 本账本协调同一工具会话内已登记需求；外部程序突发占用、未公开驱动工作区及实际分配失败仍由新快照、错误回滚与退役路径处理。

## 新数值及依据

```text
普通系统/模型预留 = min(4 GiB, max(2 GiB, 总物理内存 / 8))
物理可用处理空间 = max(0, 实际可用物理内存 - 预留)
提交可用处理空间 = max(0, 当前进程可提交量 - 预留)
准入空间 = 两者的较小值，再扣其他已获准但尚未分配的额度
```

回收目标额外留出 128 MiB 波动空间；恢复额外预取需要 256 MiB 余量和 0.5 秒间隔。回收等待上限 1.5 秒，每 25 ms 检查进展和取消。新缓存服务每 50 ms 处理回收，旧模型 worker 沿用其 250 ms 空闲收缩周期。

严重压力线为 `clamp(总 RAM/128, 256 MiB, 512 MiB)`。它用于及时退役，普通分配仍使用上面的较高预留和已知新增需求。

| 总 RAM（模拟容量） | 修复前预留 | 当前预留 | 256 MiB 新需求所需物理/提交余量 |
| --- | --- | --- | --- |
| 8 / 16 GiB | 2 GiB | 2 GiB | 2.25 GiB |
| 32 GiB | 4 GiB | 4 GiB | 4.25 GiB |
| 64 GiB | 8 GiB | 4 GiB | 4.25 GiB |
| 128 GiB | 16 GiB | 4 GiB | 4.25 GiB |

选择 4 GiB 上限的依据是：调整后本机采样中，进程树峰值扣除基础进程和已知缓冲的最大差额约 2.56 GiB，来自光流路径；再保留 1 GiB 观察余量，向上取整为 4 GiB。2 GiB 上限不足以覆盖这组观察值。小内存机器保留既有 2 GiB 下限，本轮未声称其对所有模型/驱动最优。

这是有限采样确定的保守候选，不能把私有提交差额解释成纯模型内存或完整 OS 峰值；它也不是全进程硬配额或长时间 OOM 保证。Windows 的当前进程可提交量与系统全局提交余量含义不同，沿用 API 数据并在诊断中明确口径：[Microsoft MEMORYSTATUSEX](https://learn.microsoft.com/en-us/windows/win32/api/sysinfoapi/ns-sysinfoapi-memorystatusex)。

## 本机测量

环境：Windows，实际物理 RAM 约 63.794 GiB，RTX 4070 SUPER 12282 MiB，驱动 617.42。复用 `.venv`、既有 runtime、冻结光流组件和模型。每类 8 个源帧，FG 输出 16 帧；采样间隔 50 ms，可能遗漏更短的瞬时峰值。

调整后采样：

| 负载 | 进程树私有提交峰值 | 扣基础进程与已知缓冲后的差额 |
| --- | --- | --- |
| 1080p SDR NR | 1004.0 MiB | 926.8 MiB |
| 4K HDR NR | 2089.8 MiB | 1679.7 MiB |
| SDR 2× VSR，4K 输出 | 891.7 MiB | 719.3 MiB |
| SDR 4× VSR，4K 输出 | 570.4 MiB | 409.9 MiB |
| HDR 2× VSR，1080p 输出 | 513.0 MiB | 421.8 MiB |
| 光流＋NR | 2659.1 MiB | 2623.7 MiB |
| SDR 2× FG | 1284.2 MiB | 1247.7 MiB |

调整前后 7 类的全部输出帧哈希一致；最终 FG 补验也与调整前一致，共保留 8 组逐帧比较。另有 8 帧实际 `RenderCache → encode_cached` 超分＋NR 导出完成，所有处理子进程已退出。

GPU 编码对 SDR、PQ、HLG 分别比较原 FFmpeg 路径与隔离 GPU 路径，各编码 8 帧，解码帧 MD5 全部一致；创建前取消通过，最终待用/获准额度均为 0。见[编码与取消证据](ram-coordination-20261026/encoder-memory.json)。

## 测试记录与重建

- 关联主批次：254 passed、2 skipped、24 subtests，另有 1 项 Tk 初始化失败，尚未进入该测试主体。
- Tk/HDR 图像序列独立批次：49 passed，包含上述失败项。
- 最后协调与边界批次：111 passed；生命周期补验：32 passed。
- 既有 `tmp/guidance-cuda-env` 中模型缓存补验：2 passed。
- 按测试身份去重后 308 项均有通过记录，无未解决的失败或跳过。原混跑失败 XML 保留，不描述成某一次主批次全绿。没有执行全仓回归。

复建前登记新的单一 `tmp/<任务>-<日期>/TASK.md`，把下面 `<任务目录>` 换为该目录；沿用既有环境和本地依赖：

```powershell
$env:TEMP = 'F:\project\DLSS5Tool\tmp\<任务目录>'
$env:TMP = $env:TEMP
$env:PYTHONDONTWRITEBYTECODE = '1'
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --basetemp=tmp/<任务目录>/tests tests/test_memory_coordination.py tests/test_memory_policy_calibration.py tests/test_memory_budget.py tests/test_shared_cache_budget.py tests/test_render_cache.py tests/test_p0_memory_boundaries.py tests/test_p0_runtime_pressure.py tests/test_p0_review_regressions.py
tmp/guidance-cuda-env/Scripts/python.exe -B -m unittest -v tests.test_guidance_cache.ModelCacheTests
.venv/Scripts/python.exe -B scripts/profile_memory_headroom.py --task tmp/<任务目录> --label samples
.venv/Scripts/python.exe -B scripts/profile_memory_headroom.py --task tmp/<任务目录> --label policy --evaluate samples
.venv/Scripts/python.exe -B scripts/verify_memory_export.py --task tmp/<任务目录> --label encoder
```

`scripts/summarize_memory_headroom.py` 保存测试身份、源文件及 runtime 摘要、候选矩阵、比较结果和各阶段峰值原始行；完整 50 ms 采样流及合成中间素材在核验后清理。证据文件逐一 SHA256 核对，见 `verification-summary.json`。

## 仓库卫生与交付范围

唯一任务目录为 `tmp/ram-coordination-20261026/`，开始 F 盘约 92.97 GiB 可用，新增预算 256 MiB。没有复制依赖、模型或安装程序；实际临时文件约 13 MB，保留证据约 542 KB。

本任务的可重建临时产物在证据校验后按明确绝对路径清理：逻辑删除 12,877,183 bytes，删除前后 F 盘可用空间增加 12,963,840 bytes（约 12.36 MiB）；只留 1,299 bytes 的 TASK.md，2026-11-02 复核。完整路径与盘差值见[收尾记录](ram-coordination-20261026/cleanup.json)。原有其他任务修改、持久依赖及旧发行目录保持其原有归属。本轮未生成发行包。
