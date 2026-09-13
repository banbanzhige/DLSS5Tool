# 图片光流开关与下线深度路径检查

## 本次改动

- 推理模型页增加「图片跳过光流反推」，`guidance_skip_still_flow` 默认 `true`。
  复用现有 CheckToggle、主题和键盘交互，提供常显说明及悬浮说明。
- 老设置、未带新字段的队列默认跳过；关闭后保留所选光流路径。
  主推理模式为关闭时，新开关不会强制打开光流。
- 图片预览、预取、单图导出、混合队列图片任务均使用同一份只读派生策略；
  不改写队列快照或视频推理偏好。图片模式切换也不再为已跳过的光流做加载预检。
- 缓存键包含开关值；切换时继续沿用现有暂停、回收会话、清缓存与自动保存流程。
- 单帧没有有效帧间运动。关闭开关只是允许原光流路径和模型加载，
  不虚构前一帧：正常 reset 帧仍输出零光流，不代表单张图片能推断出真实运动。
- 大图分块与光流的现有不兼容限制保留；需要分块而未开启跳过时明确提示操作，
  不悄悄覆盖用户选择。光流可视化/导出继续提示单帧不可用。

## 深度检查结论（优化前，代码与无模型测试）

未发现普通模式下仍在运行深度模型，但残留数据路径并非零开销。
首次检查时保留深度实现和原生协议。后续经维护者授权已实施优化，
见 [深度残留开销优化与验证](DEPTH_CLEANUP_20260912.md)；以下描述保留为优化前证据。

1. `guidance_public.py` 将深度模式 2→0、混合模式 3→1；只有显式设置
   `DLSS5TOOL_ENABLE_DEPTH=1` 才恢复维护者深度入口。
2. `guidance_worker.Models.__init__` 仅在模式 2/3 导入架构、加载权重或启用 SDPA。
   普通模式 `self.depth` 为 `None`；执行策略不会为纯光流启动深度双流。
3. `_serial` 的深度输入、模型和后处理受 `self.depth is not None` 保护。
   新增无 Torch/无模型测试验证 reset 和连续帧都不调用深度模型，
   `depth_model_calls=0`，输出深度为零。
4. `_process_frame` 仍计算深度分析尺寸；若光流和深度尺寸不同，仍执行一次
   `cv2.resize` 生成未使用的 `depth_small`。默认尺寸相同且 RAFT 对齐不改变尺寸时
   会复用 `small`，不能把此缩放开销视作所有帧必然发生。
5. `GuidanceBuffers` 固定分配 RGBA8、XY float32、depth float32；深度占 4 字节/像素。
   Worker 每帧为未启用的深度清零；Client 对深度做有限值扫描。
   共享内存主渲染借用视图避免一次复制，但 pipe 路径仍传输/复制深度数组。
6. `native/host_v2/dlssnr_host_v2.cpp` 在未启用 zero-guidance fast path 时，
   仍执行 `PrepareDepth` / `UploadDepth` 或 `RecordUpload`，即使内容是零。
   纯光流不能直接使用同时跳过光流和深度的全零快速路径。

3840×2160 的 float32 深度缓冲为 33,177,600 字节，约 31.64 MiB。
这是单个数组的体积，不是测得的耗时或新增显存总量。
没有做真实 GPU 端到端 A/B，因此不能断言其占比、帧率损失或本次加速百分比。

后续可独立验证：先避免未启用深度的缩放；再以协商能力而非破坏旧协议的方式，
支持按通道省略深度传输、扫描和上传，确保主机仍绑定有效零深度资源。

## 验证与卫生

最终定向回归运行 236 项测试，235 项通过、1 项因环境条件跳过；无失败。
`git diff --check` 通过（仅现有 GUI 文件行尾规范提示）。未进行发行构建或 GPU 性能实测。

复用仓库 `.venv`，设置 `PYTHONDONTWRITEBYTECODE=1`，临时文件统一放在
`tmp/still-flow-20260912/`，不下载、打包或复制模型。测试覆盖设置迁移、
队列快照不变、两种光流后端、图片导出、预检绕过、视频保留预检、
大图限制、控件键盘交互/禁用/持久化及深度不执行。

重建命令（PowerShell，先按仓库卫生守则登记临时目录）：

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:TEMP='F:\project\DLSS5Tool\tmp\still-flow-20260912'
$env:TMP=$env:TEMP
.venv/Scripts/python.exe -B -m unittest tests.test_still_flow_setting tests.test_processing_limits tests.test_guidance_activation tests.test_guidance_public tests.test_guidance_execution tests.test_guidance_parameters tests.test_guidance_startup tests.test_gui_module_reload tests.test_gui_guidance_tab tests.test_gui_player tests.test_export_queue tests.test_mods_guidance tests.test_dlss_host_process tests.test_guidance_export tests.test_guidance_transport
```
