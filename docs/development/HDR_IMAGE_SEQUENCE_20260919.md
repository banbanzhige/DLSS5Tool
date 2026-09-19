# HDR 图片序列接入与验证 · 2026-09-19

## 交付范围

接入显式声明的 **BT.2020 / PQ 或 HLG、全范围 16 位三通道 RGB PNG 序列**。
默认仍是 8 位 SDR PNG/JPG；不根据位深推断 HDR，不自动应用嵌入 ICC。
不支持 EXR、HDR TIFF、其他色域、线性数据、灰度、透明通道或单张 HDR 图片。

- 导入窗口增加本地化的色彩选项、常驻说明、扫描期间禁用及错误恢复。
  采用 UI/UX 技能的明确标签和输入说明规则，保留原主题及键盘交互。
- HDR 使用 v2 `.dlssseq` 保存色彩类型；SDR 继续生成原 v1 内容，保持旧队列身份。
  色彩类型计入描述文件摘要和渲染缓存身份，改变解释方式必须重新导入。
- HDRSequenceReader 从 BGR uint16 转为既有 PQ/HLG RGBA16F 渲染协议，无中间 8 位量化。
  FP16 是现有处理协议，不声称逐位保留 PNG 的全部 16 位整数精度。
- 原图预览、异步预览、分析代理、共享渲染、增强、超分、插帧及缓存导出复用现有路径。
  预览在高精度信号上进行 SDR 映射；关闭 HDR 时先映射再量化。
- 输入 RGB 按 full range 解释，输出仍按现有 BT.2020nc limited-range 10 位 HEVC 协议。
  色彩信息错误时不允许图片序列静默回退成 SDR；图片修改检测、取消关闭和无音轨保持有效。

## 自动测试

最终分组结果：**196 passed、20 subtests passed；17 个界面用例分别在独立进程中全部通过**。
合计覆盖 213 个用例；不是声称全部仓库测试或一次单进程全绿。

核心命令：

```powershell
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --basetemp tmp/hdr-sequence-20260919/pytest-complete tests/test_image_sequence.py tests/test_hdr_image_sequence.py tests/test_hdr_pipeline.py tests/test_frame_generation.py tests/test_render_cache.py tests/test_guidance_export.py tests/test_encoding_contract.py tests/test_video_encoding.py tests/test_i18n.py tests/test_original_comparison.py tests/test_preview_latency.py tests/test_gpu_export_routing.py tests/test_export_queue.py tests/test_super_resolution.py -k 'not test_import_dialog and not test_cancel_import and not test_queue_import_buttons and not InlineControlTests and not SuperResolutionUiTests'
```

17 个独立进程用例组成：既有导入窗口 light/dark 两项、取消一项、队列侧栏中英/明暗四项，
新增 HDR 导入错误恢复 PQ/HLG × light-zh_CN/dark-en_US 四项，InlineControlTests 六项。
各用例使用独立 `python -B -m pytest ... <node-id>` 执行。

测试覆盖：16 位读取逐元素对比、超过 256 级输入、RGB/BGR 顺序、随机定位、EOF/关闭、
版本兼容、缓存身份、元数据、显式 SDR 转换、异步预览、分析副本、队列持久化、
错误色彩/尺寸/混合位深/灰度/透明通道拒绝、源修改及取消清理。

验证中遇到的问题：

- 初轮 90 项全部通过；扩大同进程测试时，多次创建 Tk 根窗口会间歇报不同 `.tcl` 文件无法读取，
  在沙箱外也复现。实际文件存在；所有相关用例独立进程复测通过。
  未修改用户 Python/Tk 安装，不将这一环境/进程组合问题宣称已修复。
- 新增测试初期 patch 了共享 subprocess.Popen，误拦截 NumPy 延迟加载时的系统查询；
  已改为仅替换被测模块的 subprocess 引用，最终回归通过。
- `git diff --check` 通过；gui.py 有既有 CRLF→LF 提示，无空白错误。

## 真机端到端

GPU：RTX 4070 SUPER；复用现有 `.venv`、原生组件及 FFmpeg，没有下载/复制模型或环境。
输入：320×180，6 帧，24 fps；16 位 RGB 渐变与移动矩形，**合成素材，不是真实相机/发行 HDR**。
实际执行 RenderCache → 原生增强 / 超分 / NVOFA+插帧 → encode_cached → HEVC → 解码回查。

```powershell
.venv/Scripts/python.exe -B scripts/image_sequence_smoke.py --work-dir tmp/hdr-sequence-20260919/pq --hdr pq
.venv/Scripts/python.exe -B scripts/image_sequence_smoke.py --work-dir tmp/hdr-sequence-20260919/hlg --hdr hlg
```

目录需为新目录；复测前重新登记空间预算。每种色彩运行以下四项，均通过：

| 组合（均含增强阶段） | 输出尺寸 | 输出帧数 / 帧率 | 时长 |
| --- | --- | --- | --- |
| 1× 超分 / 1× 插帧 | 320×180 | 6 / 24 fps | 0.25 s |
| 2× 超分 / 1× 插帧 | 640×360 | 6 / 24 fps | 0.25 s |
| 1× 超分 / 2× 插帧 | 320×180 | 12 / 48 fps | 0.25 s |
| 2× 超分 / 2× 插帧 | 640×360 | 12 / 48 fps | 0.25 s |

8/8 输出均为单一视频流、无音轨、HEVC Main10、yuv420p10le、BT.2020/BT.2020nc/tv，
PQ 为 smpte2084，HLG 为 arib-std-b67。2× 插帧实际生成 5 帧，另有末端保持帧。
解码帧与实际缓存渲染帧比较，最差逐帧归一化码值 MAE 为 0.003186 以下，门槛为 0.01；
各案例解码值至少 1994 个不同取值。此比较检验编码传递，不声称增强结果等同原图或无损压缩。

原始紧凑结果：[PQ](hdr-sequence-20260919/pq.json)、[HLG](hdr-sequence-20260919/hlg.json)。

## 验收边界

- 未验收真实 HDR 素材观感、HDR 显示器亮度、4K/8K 长片和 3×/4× 插帧。
- 小尺寸导出走现有 FFmpeg 写入路线；未将本次验证扩大为 1080p 以上 GPU 色彩转换路线的新认证。
- 未新增 mastering-display/MaxCLL 或动态 HDR 元数据支持。
- 用户已有 Release Notes 修改保留未动；本任务没有打包、推送或发布。

## 临时目录

登记目录：`tmp/hdr-sequence-20260919/`；负责人 Codex；创建 2026-09-19，复核 2026-09-26。
开始 F: 可用约 135.86 GiB、tmp 总量 28.617 GiB；新增预算 100 MiB，未创建大产物。
测试素材、视频、日志与 pytest scratch 可由上述命令重建；收尾清理仅针对该任务目录，
正式结果保留于本报告和两个 JSON。其他任务、持久依赖及备份不在清理范围。

收尾已删除本任务的 16 个可重建子目录（含一次沙箱外测试目录），删除前确认边界、
无目录链接/联接、无该任务运行进程，且引用仅为重建说明。逻辑文件总量 14,602,351 字节，
两次删除窗口测得磁盘可用空间累计增加 15,577,088 字节（约 14.86 MiB；非独占磁盘测量）。
任务目录仅保留小型 TASK.md 作为生命周期记录；其他 tmp 内容未删除。
删除为永久删除，素材/测试输出可通过脚本重建，不能从回收站恢复。
