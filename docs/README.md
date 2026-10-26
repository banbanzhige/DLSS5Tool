# 项目目录与文档索引

## 目录约定

| 目录 | 用途 |
| --- | --- |
| `dlss5tool/` | 应用 Python 包；统一使用 `dlss5tool.*` 导入 |
| `native/host_v2/`、`native/vsr_host/`、`native/amd_probe/`、`native/realtime/` | 原生源码与各自构建入口 |
| `scripts/` | 构建、打包、诊断、性能探针与维护脚本 |
| `packaging/` | PyInstaller 配方、Windows 版本资源、增强组件契约 |
| `docs/` | 功能说明、实验记录和发行维护文档 |
| `tests/` | 自动化测试与测试用原生源码 |
| `assets/`、`locales/`、`img/`、`licenses/` | 图标、翻译、展示图片和许可证 |
| `runtime/` | 开发运行所需 DLL，除说明文件外不提交 |
| `var/` | 开发设置、导出队列、日志，不提交 |
| `build/`、`dist/` | 编译中间产物和发行包，不提交 |
| `mods/`、`amd_backend/`、`third_party/` | 外部组件、模型与 SDK，保留现有使用约定 |
| `output/`、`tmp/` | 本地实验输出和临时文件，不提交 |

根目录仅保留项目说明、许可证、仓库配置、依赖清单和启动入口。
新增技术文档、模块、构建配置或运行产物请放入对应目录。
本地发行默认输出到 `E:\dist\DLSS5Tool\<版本>\`；历史 `dist/` 与策略引用的旧基线不自动迁移或删除。

## 启动与构建

以下命令均在仓库根目录运行：

```powershell
.\run.bat

# 普通单元测试，无需 GPU / SDK
.\.venv\Scripts\python.exe -B -m unittest discover -v

# 主应用和独立 AMD 开发验证包
.\scripts\build_release.ps1
.\scripts\build_amd_devtest.ps1
```

原生构建入口：`native/host_v2/build.bat`、`native/vsr_host/build.bat`、
`native/amd_probe/build.bat`。宿主 DLL 默认进入 `runtime/`，中间产物进入
`build/native/`；AMD 开发探针仍进入 `build/amd_probe/`。

`gui.py` 是兼容启动入口，不再存放应用实现。开发模式的旧根目录设置、队列和日志
会在新路径不存在时复制到 `var/`，不会覆盖新文件或删除旧文件。环境变量
`DLSS5TOOL_SETTINGS_PATH`、`DLSS5TOOL_QUEUE_PATH` 的优先级不变。

便携发行版仍将设置与队列保存在 EXE 旁，内置 DLL / 资源在 `_internal/`，
外置组件在 `mods/`；目录调整不要求重新移动用户模型或 SDK。

## 文档导航

- 本机窗口实时增强：[当前使用说明](REALTIME_USER_GUIDE.md)、[实现与验收台账](development/REALTIME_IMPLEMENTATION_20261003.md)；[Magpie / Veyra 原始研究清单（历史计划）](development/MAGPIE_VEYRA_REUSE_CHECKLIST_20261003.md)
- 双模式复用与资源协调：[P3 收尾记录](development/P3_CLOSEOUT_20261011.md)、[参数交互优化](development/PARAMETER_RESPONSE_20261010.md)
- 下一轮研究交接：[光流性能与质量 TODO（用户决策、缓存收益、异步时序安全）](development/TODO_FLOW_PERFORMANCE_20260915.md)
- 当前待办排序：[2026-10-19：内存与退出释放、GPU 吞吐、本地 GPU 合成及后续研究](development/TODO_PRIORITIES_20261019.md)；[P0/P1 复核](development/P0_P1_AUDIT_20261019.md)；[P0 三处边界修复与回归](development/P0_BOUNDARIES_FIX_20261019.md)；[P0 审查四项遗漏修复](development/P0_REVIEW_FIX_20261019.md)
- 本地 GPU 合成交接（四项已本机落地，保留原始计划）：[同卡、HDR 公式、接入与免读回](development/TODO_LOCAL_GPU_COMPOSITION_20261016.md)
- GPU 常驻研究：[2026-09-15 原型](development/GPU_PIPELINE_STUDY_20260915.md)；编码合同补测：[2026-09-16 验收（未批准生产接入）](development/ENCODE_ACCEPTANCE_20260916.md)
- 当前接入进展：[2026-09-17 编码时间戳与SDR GPU转换（候选通过，未切换默认）](development/ENCODER_INTEGRATION_20260917.md)
- 后续落地：[2026-09-19 真实插帧与音视频封装接入（显式候选，HDR转换未完成）](development/GPU_EXPORT_INTEGRATION_20260919.md)
- 最新本地升级：[HDR转换、普通增强、独立导出进程与应用选择已接入](development/GPU_EXPORT_UPGRADE_20260919.md)
- 超分效率研究：[2026-10-24 实卡吞吐、GPU 利用率、场景检测候选与 NVEncC 复用方案](development/SR_EFFICIENCY_20261024.md)
- 超分升级路线：[实施方案、阶段依赖、验收与回滚](development/SR_UPGRADE_ROADMAP_20261024.md)；[流程、SDK、编解码依赖及关联功能清单](development/SR_UPGRADE_CHECKLIST_20261024.md)
- 2026-10-26 状态：[超分升级 TODO 对账](development/SR_UPGRADE_RECONCILIATION_20261026.md)；[内存保护与缓存协调已完成项](development/RAM_HEADROOM_IMPLEMENTATION_20261026.md)
- 超分 S0/S1 已实施：[场景复用、顺序导出、像素与性能验收](development/SR_SCENE_OPTIMIZATION_20261026.md)；[原始测量及验证汇总](development/sr-scene-optimization-20261026/verification-summary.json)
- 超分 S2 基础：[SDR CUDA VSR、设备帧与显式选卡](development/SR_CUDA_IMPLEMENTATION_20261026.md)；[组件／像素／退出证据](development/sr-cuda-20261026/verification-summary.json)。
- 超分 S3 已接入源码：[NVDEC→CUDA VSR→NVENC、速度、像素与退出验收](development/SR_RESIDENT_IMPLEMENTATION_20261026.md)；[验收汇总](development/sr-resident-20261026/verification-final.json)。HDR／NR／FG 的后续接入见 S4。
- 超分 S4 本机已启用：[HDR 显存导出、NR／FG 与预览接入](development/SR_S4_IMPLEMENTATION_20261027.md)；[像素／时间线验收](development/sr-s4-20261027/s4-verification.json)。有界样本、261 项定向测试及安装后 GUI／缓存通过；S4a／S4 源码纳入本次提交，尚未打包，异步与长任务继续 S5。

- 仓库维护：[卫生守则与空间预算](development/REPOSITORY_HYGIENE.md)、[2026-09-11 临时目录盘点](development/TMP_AUDIT_20260911.md)

- 当前版本：[v3.1.0 更新说明](release/RELEASE_NOTES_v3.1.0.md)、[本地打包与发布状态](release/PACKAGING_INDEX.md)。当前准备本地 v3.1.0 打包验收，公开下载以正式 Release 为准；分发许可审查仍待完成。
- 既有功能与历史验证：[NVOFA 集成验证](experiments/NVOFA_INTEGRATION.md)、[深度下线状态](experiments/DEPTH_REFERENCE_STATUS.md)、[实施核对](development/IMPLEMENTATION_READINESS.md)

- 用户指南：[简体中文](USER_GUIDE.md)、[English](USER_GUIDE.en.md)
- 开发上手：[源码运行与构建](development/BUILDING.md)、[English](development/BUILDING.en.md)
- 引导与增强：[参数说明](guidance/GUIDANCE_PARAMETERS.md)、[激活验证](guidance/GUIDANCE_ACTIVATION.md)、[增强组件](guidance/ENHANCEMENT_PACK.md)、[GPU 引导](guidance/GPU_GUIDANCE.md)
- 缓存与传输：[缓存](guidance/GUIDANCE_CACHE.md)、[共享缓存](guidance/GUIDANCE_SHARED_CACHE.md)、[进程传输](guidance/GUIDANCE_TRANSPORT.md)
- 实验记录：[首轮优化](experiments/FIRST_PASS_OPTIMIZATION.md)、[光流质量](experiments/FLOW_QUALITY_TEST.md)、[实时路径](experiments/REALTIME_ROUTES.md)、[原生上传](experiments/NATIVE_UPLOAD_OPTIMIZATION.md)
- 开发与发行：[AMD 验证](development/AMD_DEVTEST.md)、[打包索引](release/PACKAGING_INDEX.md)、[历史发行说明](release/RELEASE_NOTES_v2.1.1.md)、[v2.1.2 待办：暂时下线深度引导](development/TODO_v2.1.2.md)、[NVOFA 接入待办](development/TODO_NVOFA.md)

实验记录中的历史部署路径和哈希描述当时的验证环境，不代表当前文件仍位于旧目录。

- 本机实时增强：[使用说明](REALTIME_USER_GUIDE.md)、[双模式工作台设计与验证](development/WORKBENCH_UI_20261003.md)、[标题栏入口与播放器布局](development/CAPTION_TOOLBAR_20261003.md)、[切换动画与界面本地化](development/WORKBENCH_POLISH_20261003.md)、[19 项实现与验收台账](development/REALTIME_IMPLEMENTATION_20261003.md)、[源码来源与构建](../native/realtime/NOTICE.md)。
