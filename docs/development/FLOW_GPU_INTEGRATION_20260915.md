# 光流 GPU 直连正式源码接入 · 2026-09-15

## 交付范围

已将上一轮单进程实验接入实际 `Live → GuidanceSession → guidance_worker` 跨进程链路，
并验证 GUI 使用的 `ProcessLive` 三进程生命周期。本机 `runtime/dlssnr_host_v2.dll` 已升级。
`run.bat` 已有的 `DLSS5TOOL_GUIDANCE_PYTHON` 入口复用当前源码与现有 CUDA 环境，所以重启开发版即可使用。

**未重打冻结引导组件、免安装 EXE 或发行包；没有发布、上传或更新正式增量包。**
旧冻结组件依旧运行 CPU 搬运路径，其兼容性已实测。发版时仍须按发布规则重新构建组件及三形态/增量包。

## 行为与兼容性

- 默认 `guidance_gpu_transport=auto`；同卡 CUDA RAFT、仅光流、v2 宿主、持久缓冲、非分块时协商直连。
  `off` 可用于维护者 A/B 或强制旧路径。新设定进入参数验证和会话配置变更判定。
- 兼容提交和合并提交均支持。每个在途槽有独立共享原始光流 buffer，避免下一帧覆盖在途输入。
- 旧宿主、旧组件、CPU/NVOFA、深度混合、非持久缓冲、管道传输或小尺寸下采样保留原路径。
  不改模型、精度、迭代次数、所选显卡或用户现有设置；同卡匹配失败仅回退搬运，不切换 GPU。
- 主程序和 D3D12 宿主不导入 Torch；仅引导工作进程导入 CUDA Driver API。
- 开启原始光流缓存时保留现有 RAM 缓存及共享预算。缓存写入可能仍回读小尺寸原始光流；
  缓存命中时上传小尺寸结果，避免全尺寸 CPU 放大、half 转换与上传。不能声称所有模式全程零回读。
- 光流可视化/分析图继续返回 CPU 数组；首次预览后可协商 GPU，已启用后也可按帧切回 CPU 预览。
- 跳转/切镜 reset 不读取旧显存，shader 清零 motion texture；输入/输出帧序号和 GPU 槽号均核对。

## 所有权和同步

1. 宿主创建每槽 D3D12 shared DEFAULT buffer，传递固定尺寸、分配大小、同卡 LUID 和导出句柄。
2. 工作进程仅用 PROCESS_DUP_HANDLE 权限从经认证且受 watch_parent 监控的宿主复制句柄。
   Windows venv 会额外启动进程，不能拿 os.getppid() 代替真实宿主 PID；握手 PID 与 --parent 必须一致。
3. 请求写入某槽前，宿主确认它不处于 pending 并等待其 D3D12 fence；GPU 完成前不会复用该槽。
4. CUDA 同 stream 复制原始张量并同步，完成后才确认序号/槽号。宿主确认后仅为下一次提交 arm 该槽。
5. D3D12 shader 使用已验证的坐标/尺度常量，放大、转换，再在同命令队列交给 DLSS。
   每次转换 scratch buffer 的状态跃迁与消费均有明确顺序。
6. 关闭/重建时先停止工作进程，再等待 D3D12 消费并释放导出资源；resize 在销毁原纹理前关闭引导。
   继续沿用已有隔离进程退出策略，不在 Evaluate 后调用已知会卡住的 NGX Shutdown。

本轮使用 CPU 完成确认＋CUDA stream synchronize＋D3D12 fence，未引入外部 semaphore 全 GPU 流水线。
它已经去掉主要数据绕行，并不意味着消除了所有同步或 GPU 内复制。

## 验证

- 全量 unittest：575 项，565 通过、10 跳过。新增 12 项描述符/ABI/兼容协商/错序/超时/
  预览/输入失败/资源释放顺序测试通过。并非多 GPU、驱动故障或无限长视频认证。
- `merged-parent-fix`：1440×1440，RAFT 512，三槽队列；CPU→GPU→GPU→CPU 对照输出逐像素一致。
- `compatibility-preview`：1080×1920，RAFT 720，默认兼容提交；先预览再处理，24 帧对照含 reset，输出一致。
- `cache-forward-queue`：1440×1440，64 MiB 缓存、forward_negated、四槽队列；24 帧中 22 个缓存命中，输出一致。
- `lifecycle-main`：真正 ProcessLive 三进程，256×256→320×192→256×256；每个尺寸比较 CPU/GPU，
  查看光流、恢复处理、队列满时拒绝且不推进历史、排空，全部通过；关闭约 0.11 秒；GUI 进程未导入 Torch。
- `old-host-fallback`、`frozen-worker-fallback`：旧 DLL / 实际旧冻结组件自动保留 CPU 路径，输出一致。
- `deployed-runtime`：直接加载已替换的 runtime DLL，在默认兼容模式确认直连协商成功、各帧实际使用 GPU、最终输出与旧路径一致。
- 最早 `first-merged` 因 venv 中间父进程导致同卡句柄导入被拒，正确回退但不满足直连断言；已修正握手身份检查。

报告保存在 `flow-gpu-integration-20260915/`。计时包含跨进程引导与 DLSS，但不包含视频解码、编码和 GUI。
各轮有明显后台负载变化，不能把此前单进程的 15%～21% 作为正式 GUI 固定增益承诺。
以 `cache-forward-queue` 的预热缓存场景为例，旧路径约 28.64 ms，新路径约 20.33 ms；
该场景是缓存命中，不应宣传为首次模型推理性能。

## 构建与部署

复用 MSVC、`third_party/NVIDIA-DLSS` 和既有 `tmp/guidance-cuda-env`，新增产物不足 2 MiB，没有复制依赖。
构建命令（先登记任务、检查磁盘）：

```powershell
$env:TEMP='F:\project\DLSS5Tool\tmp\flow-gpu-upgrade-20260914'
$env:TMP=$env:TEMP
cmd /c scripts\build_isolated_host.bat native\host_v2\dlssnr_host_v2.cpp tmp\flow-gpu-upgrade-20260914
.venv/Scripts/python.exe -B scripts/gpu_flow_integration_probe.py --work tmp/flow-gpu-upgrade-20260914 --source '<视频>' --label fresh-run --frames 24 --submission compatibility --preview-first
.venv/Scripts/python.exe -B scripts/gpu_flow_lifecycle_probe.py --work tmp/flow-gpu-upgrade-20260914 --label fresh-lifecycle
```

- 新 DLL SHA256：`c8ad631f8f78b2dedec6aec418c7a570d6fc5bf1b9ffa9f3514bcb0edd58fc13`。
- 旧 DLL SHA256：`57b4255055e293f35fd9394e43d42e4b32e2189ff5d6dc90723a17308d5d4428`。
- 回滚文件：`tmp/flow-gpu-upgrade-20260914/dlssnr_host_v2.before-gpu-flow.dll`，已验证与替换前文件一致。
  关闭开发版后可恢复到 `runtime/dlssnr_host_v2.dll`；新 Python 代码检测旧 DLL 后自动使用原 CPU 搬运。
- 只保留一个本任务旧 DLL 回滚，不删除已有其他任务备份。复核日期 2026-09-22。

## 剩余验证范围

多卡实机拒绝/恢复、设备移除/驱动重置、HDR 端到端画质、长视频压力和新冻结组件均未在本轮完成认证。
GPU 推理/传递中途失败会停止并重建会话，不会把空运动向量当成正常帧继续输出。

## 收尾

部署后补跑 51 项相关单测全部通过。小型原始报告已归档并按原始数值文本核对。
本任务编译文件、候选 DLL、重复报告和日志共 19 个文件已清理；只保留升级前 DLL 与任务登记，
合计约 0.28 MiB，供回滚及 2026-09-22 复核。已有模型、环境、其他任务文件和备份未动。
清理文件逻辑体积 707,218 B；清理前后 F 盘可用空间增加 753,664 B。最终保留 290,508 B。
