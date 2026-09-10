# 项目目录与文档索引

## 目录约定

| 目录 | 用途 |
| --- | --- |
| `dlss5tool/` | 应用 Python 包；统一使用 `dlss5tool.*` 导入 |
| `native/host_v2/`、`native/vsr_host/`、`native/amd_probe/` | 原生源码与各自构建入口 |
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

- 当前源码版本：[v2.1.2 说明](release/RELEASE_NOTES_v2.1.2.md)、[NVOFA 集成验证](experiments/NVOFA_INTEGRATION.md)、[深度下线状态](experiments/DEPTH_REFERENCE_STATUS.md)、[实施核对](development/IMPLEMENTATION_READINESS.md)

- 用户指南：[简体中文](USER_GUIDE.md)、[English](USER_GUIDE.en.md)
- 开发上手：[源码运行与构建](development/BUILDING.md)、[English](development/BUILDING.en.md)
- 引导与增强：[参数说明](guidance/GUIDANCE_PARAMETERS.md)、[激活验证](guidance/GUIDANCE_ACTIVATION.md)、[增强组件](guidance/ENHANCEMENT_PACK.md)、[GPU 引导](guidance/GPU_GUIDANCE.md)
- 缓存与传输：[缓存](guidance/GUIDANCE_CACHE.md)、[共享缓存](guidance/GUIDANCE_SHARED_CACHE.md)、[进程传输](guidance/GUIDANCE_TRANSPORT.md)
- 实验记录：[首轮优化](experiments/FIRST_PASS_OPTIMIZATION.md)、[光流质量](experiments/FLOW_QUALITY_TEST.md)、[实时路径](experiments/REALTIME_ROUTES.md)、[原生上传](experiments/NATIVE_UPLOAD_OPTIMIZATION.md)
- 开发与发行：[AMD 验证](development/AMD_DEVTEST.md)、[打包索引](release/PACKAGING_INDEX.md)、[历史发行说明](release/RELEASE_NOTES_v2.1.1.md)、[v2.1.2 待办：暂时下线深度引导](development/TODO_v2.1.2.md)、[NVOFA 接入待办](development/TODO_NVOFA.md)

实验记录中的历史部署路径和哈希描述当时的验证环境，不代表当前文件仍位于旧目录。
