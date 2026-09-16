# 验证与收尾（2026-09-16）

## 测试

- `.venv/Scripts/python.exe -B -m unittest tests.test_gpu_pipeline_candidates tests.test_gpu_flow tests.test_hdr_pipeline tests.test_flow_input_candidate -q`
  27 项，23 通过、4 跳过（基础环境没有 Torch）。
- `tmp/guidance-cuda-env/Scripts/python.exe -B -m unittest tests.test_gpu_pipeline_candidates tests.test_flow_input_candidate -q`
  7 项全部通过，含真实 CUDA 张量复用、尺寸/方向切换、清空、HDR 端点、CPU 预处理原有回归。
- 独立编译得到 VSR/color/NVENC DLL、带实验宏的 DLSSG worker，以及不带宏的正常 control worker。
  上次工具会话跨日后已不可恢复，不把丢失的最终终端输出当新证据；正常 control.exe 已实际启动，
  用原 `NativeStream` 协议处理 4 帧：每帧返回一个 320×180 RGBA 结果，reset 帧无效标志为 False，
  后三帧均 True，进程正常关闭。正式 worker 没有替换。
- 上一阶段完整单测 577 项：566 通过、8 跳过、1 失败、2 错误。3 项失败原因见主报告，
  涉及未改的发布测试/当前基线策略。未为通过测试更改策略。
- `git diff --check`、实验 Python AST 检查通过。未提交、推送或打包。

## 证据口径

本目录 62 个 JSON/log 文件从任务目录逐文件复制，源/目标 SHA256 一致后才删除重复源文件。
包含失败证据、被后台单测干扰的计时及初始化失败日志，不能把它们都算成成功测量。
初始 VSR 同进程失败被手动终止，只有日志，没有完整 JSON；后续带超时隔离并写出了失败报告。
最终源码包含后续修正，历史报告中的脚本 hash 可能不同；不得声称所有历史数据来自最终同一源码快照。

禁止推广项：4K VSR 候选的输出差异、HDR GPU 算术差异、深度 resize 差异。
两类 NVENC 路径只是特定编码策略的隔离原型；见主报告中的生产编码/时间线/颜色合同缺口。
计时不能用于宣称 GUI 端到端固定提升，也不能将各项百分比相加。

## 本任务清理

- 目标严格限定为 `F:\project\DLSS5Tool\tmp\gpu-pipeline-20260915` 的具名文件。
- 删除前检查根/子目录无 reparse point，查询本任务 Python/候选/编译进程，没有匹配在运行任务；
  检查源码引用只来自独立探针，正常应用不引用这些临时候选。日志与报告均有校验过的归档。
- 用原生 PowerShell `Remove-Item -LiteralPath` 删除 80 个文件，逻辑大小 3,729,418 B。
  编码与 DLSSG 成功实验的候选二进制、正常 control、所有 obj/lib/exp 以及重复报告日志已清理；
  对应脚本保留在 `scripts/`，可重新编译。删除文件不承诺可从回收站恢复。
- F 盘清理前可用 102,123,708,416 B，清理后 102,127,607,808 B；观测增加 3,899,392 B。
  磁盘可能有其他活动，此数为前后观测值，不把整盘其他空间变化归因于本任务。
- 保留文件共 814,754 B（登记更新前）：`TASK.md`、官方 `nvEncodeAPI.h`、
  `native-color/candidate.dll`、`native-vsr/candidate.dll`。登记补充后占用会略增。
- 保留原因：4K VSR 不一致尚未定位，保留一个明确候选配对便于复现；头文件仅 282,466 B，供后续重建。
  复核日期 2026-09-22。不是第二套环境，没有复制正式 DLL 或另留整套模型。
- 原有 `tmp/` 依赖、用户素材、备份、正式发布基线均未清理。

保留候选 SHA256：

- color：`d2618fb8466d4d4678681c9064ca68170b2cae72509df38e04cbc04838ee6fd9`
- VSR：`028558f9c919f15edeb4f18a6cda7c028bdd78485a88af5143806540a9bc2dad`

跨日恢复时工作区出现本任务之外的 GUI/诊断/预览等改动，均保留未编辑；本轮研究不为那些改动提供验收结论。
