# NGX 更新器生命周期修复 · 2026-11-11

起始于 2026-11-10，针对 [issue #13](https://github.com/banbanzhige/DLSS5Tool/issues/13)。
本次完成源码修复与本机定向验收，不包含正式打包、上传或对已安装程序的修改。

## 修复范围

- DLSS `ProcessLive`、RTX 超分 `ProcessSuperResolution`、并行视频导出的 NGX Worker
  均在父进程绑定独立 Windows Job 成功后，才获得原生初始化许可。
- Job 采用 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，句柄不继承、不允许 breakaway，
  仅管理本次 Worker 的进程树。不会按名称或同会话范围扫描并终止 NVIDIA 更新器。
- 父进程监测 Worker 的真实进程句柄：自然退出、崩溃或空闲压力退休后，即使没有后续
  API 调用，也会关闭对应 Job；父进程突然退出时由 Windows 关闭其 Job 句柄。
- 正常关闭仍先请求 Worker 收尾；卡住时回收整个 Job。共享帧只在 Worker 已退出、
  退出监测结束后释放。失败的 NR/VSR 会话保留所有权以便重试。
- 并行导出保留回收失败的 Job 与 Popen 所有者；下次导出先重试回收，失败时阻止继续分配。
- 源码模式的并行导出直接启动真实解释器，并保留虚拟环境配置，避免虚拟环境启动器在
  Job 绑定前派生实际 Python 进程、使后续 NGX 子进程逃逸。
- 保留内存余量保护和进程隔离架构，不调整画质、推理精度或 Worker 复用策略。

主要源码：`dlss5tool/process_job.py`、`dlss_host_process.py`、`super_resolution.py`、
`parallel_export.py`、`parallel_export_worker.py`。

## 定向验证

本机：Windows、Python 3.13.3、RTX 4070 SUPER、NVIDIA 驱动 621.14，复用既有 `.venv`
和运行库。未下载模型、复制依赖环境、安装驱动、改服务或修改 NVIDIA 全局设置。

### 自动化与真实 Windows 进程树

- 主轮关联检查：277 passed、4 failed、21 subtests passed。
- 并行收尾检查：19 passed、16 subtests passed，包含实际编码与音画同步的两个用例；
  其中部分为对主轮已通过用例的必要复测，不重复累加。
- 合计 280 个不重复用例通过、21 个子用例通过；4 个既有失败保持单列。
- 新增 28 个用例覆盖：初始化许可、Job/监测线程建立失败、初始化失败／超时、
  挂起收尾、Worker 异常退出、GUI 所有者突然退出、反复换尺寸、关闭失败重试、
  无关进程存活、Popen 虚拟环境启动器边界、并行任务失败所有权。
- 真实测试后代包含子进程和孙进程，各自分配 16 MiB；用保留的进程句柄验证它们退出，
  不依赖进程名。另一个测试进程在清理后仍存活。

4 个失败均为 `test_enhancement_bypass.py` 中
`test_real_cache_pipeline_zero_keeps_sr_source_and_restores_nr[2-*]`：
恢复增强后，`manager.spatial is spatial` 断言失败，原 spatial 为 None。
单独进程以内存加载修改前 HEAD `3bbe3da` 的五个产品模块，再运行同四项，
得到同样 4 个失败。未修改缓存管线、该用例或阈值来掩盖失败，也未运行全量回归。

### 真实 NGX 连续重建

使用 `scripts/ngx_updater_lifecycle_probe.py --cycles 20 --timeout 400`：

| 项目 | 结果 |
| --- | --- |
| 真实 Worker 会话 | 22：首次创建＋20 次换尺寸＋关闭后重开 |
| 处理帧数 | 66，每个会话 3 帧 |
| 观测到的更新器 | 110 个精确 PID＋创建时间身份，均属于本次测试 |
| 每个 Worker 的更新器 | 5 |
| 同时存活峰值 | 5 |
| 最终残留／探针强制清理 | 0／0 |
| 640×360／800×448 输出 | 每种尺寸各有一个稳定 SHA256 |
| 单独修改 style | Worker PID 不变 |
| 退出观察 | 最终关闭后等 10 秒，应用退出后再观察 20 秒，无残留 |

本机驱动的更新器在所测条件下自行退出；该结果不能模拟用户现场的长期网络等待。
Job 对滞留后代的实际回收由上述真实 Windows 睡眠子进程／孙进程测试单独验证。

原探针固定 180 秒上限不足以完成 20 次真实初始化，初轮在第 18 个会话附近超时，
没有当作产品失败或验收通过。确认该轮工作负载已退出后，增加可配置上限并重跑完成。
探针同时修正了虚拟环境启动器和 PID 复用的归属记录；清理仍须核对精确创建时间。

### 真实 RTX 超分

`scripts/verify_vsr_lifetime.py` 创建 3 个真实 D3D12 VSR 会话，每个处理 3 帧，
640×360 RGBA8 → 1280×720 RGBA8，合计 9 帧。
三个会话均绑定 Job，输出 SHA256 一致；关闭后 Worker／Job 所有者退休，
原有共享帧名称无法重新打开。

## 证据与重建

证据目录：[ngx-updater-lifecycle-20261111](ngx-updater-lifecycle-20261111/verification.json)。
包含结果汇总、原生运行库和源码 SHA256、NGX 原始操作与进程观测、VSR 报告，
以及主轮／并行收尾／HEAD 对照的压缩 JUnit 记录。

注册新任务目录、检查容量并设置 TEMP/TMP 后，可运行：

```powershell
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --basetemp=tmp/<任务>/pytest tests/test_process_job.py tests/test_dlss_host_lifetime.py tests/test_parallel_lifetime.py tests/test_dlss_host_process.py tests/test_super_resolution.py tests/test_p0_memory_boundaries.py tests/test_p0_owner_retirement.py tests/test_p0_runtime_pressure.py tests/test_p0_review_regressions.py tests/test_video_encoder_selection.py tests/test_resident_sr.py tests/test_adapter_probe.py
.venv/Scripts/python.exe -B scripts/ngx_updater_lifecycle_probe.py --work tmp/<任务>/native --cycles 20 --timeout 400
.venv/Scripts/python.exe -B scripts/verify_vsr_lifetime.py --output tmp/<任务>/vsr.json
```

## 边界与收尾

- 尚未覆盖用户 RTX 3080／596.21 环境、冻结 EXE 或小时级内存平台。
- 本修复管理今后由这些 Worker 创建的后代，不追溯接管已经逃逸的历史进程。
- 当时已独立定位的 Worker 进程内原生增长仍需单独跟踪；不能据此宣称全部内存问题根治。
- 工作目录为 `tmp/ngx-lifecycle-fix-20261110/`，初始 F 盘可用 50,540,204,032 bytes，
  新增磁盘峰值预算小于 128 MiB。可复用脚本已留在 `scripts/`；证据归档校验和
  临时文件清理结果写入 `verification.json`，仅保留小型任务登记，2026-11-17 复核。
- 既有 AGENTS.md 与发布文档的未提交改动独立保留。
