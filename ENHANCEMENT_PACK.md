# 免安装增强组件：实现与验证（2026-09-08）

**最新进展：已完成 CUDA 本机验证，见 [GPU_GUIDANCE.md](GPU_GUIDANCE.md)。**
以下 CPU 产物及其性能限制是早期验证记录，不再代表当前根目录 mods 中的组件。

## 零路径配置优化

开发版与打包版统一使用根目录 `mods`。开发机现有 CPU 组件和 RAFT／DAV2-Large
已复制到该目录，原测试文件保留，不改用户设置。不配置任何路径、深度型号为自动时，
真实双模型连续帧推理已通过。

组件管理位于「引导 → 附加组件」，提供状态、打开目录／刷新／详情；常用推理参数直接显示在「引导」页，高级性能设置默认折叠。
「替换 DLL / 模型」是引导页默认展开的一级分组，没有嵌套折叠。如果之前手动设置过 tmp 路径，点击
「引导 → 替换 DLL / 模型 → 恢复自动识别」一次即可。
附加 ZIP 现在自带 `mods` 层级，解压到程序目录，不再要求用户找到 enhancement 的父目录。
组件可声明 `default_depth_encoder`；新设置默认自动识别已知深度权重文件名，旧手动型号保留。

开发测试启动：`.venv\Scripts\python.exe gui.py`；引导仍默认关闭。
现有组件是 CPU 本地验证版，建议在「引导 → 通用推理」把长边降到256试跑，不适合实时播放。
开发机 mods 仍被 Git 和基础打包排除，下面的 CUDA／许可归档限制不变。

本轮验证：203 项测试通过（含附加 ZIP 解压后零配置检测、自动型号及显式覆盖）；
中英文／浅深色20张截图无回调错误；默认 mods 实际双模型推理通过。
新基础测试包位于 `tmp/addons-default-layout-20260908/DLSS5Tool-v2.0.1`，已检查仅携带
`mods/README.md`，未收集开发目录的 Torch、组件或权重。

## 已实现边界

- 基础包独立可用，默认关闭引导，保留内置 DLSS；不打包 Torch、模型架构或权重。
- 增强组件是 `mods/enhancement` 整目录，包含独立 EXE、自带依赖和架构。
- 用户只需放入组件，无需安装 Python、pip、源码或 CUDA Toolkit。
- RAFT-Large 与 Depth Anything V2 S/B/L 的兼容 state_dict 权重在 EXE 外，可替换；其他架构需要适配器。
- `mods/models` 覆盖组件默认权重，支持自定义目录及文件路径。深度型号须匹配。
- 固定组件名称与协议 v1 校验，启用引导才执行。缺组件、协议错误、错权重不静默安装或替换。
- 界面按渐进展开原则保留四行状态，将路径／推理选项折叠；删除 Python 与源码配置，中英文同步。

## 验证证据

- 全部 199 项单元测试通过；最后路径检查微调后另复测 26 项组件与 i18n 测试通过。
- 实际冻结 CPU 组件：仅光流、仅深度、混合模式均通过；外部自定义权重路径、运动／深度输出与重置有效。
- PATH 中仅保留 Windows System32；增强子进程仍能正常执行，无需系统 Python。
- 原生 DLSS 输入联动、异步提交，以及冻结主程序 → 冻结组件链路三种模式均通过。
- 冻结基础包无组件时处理成功；开启引导时缺组件明确报错；将 RAFT 权重送入 Depth Anything 被严格拒绝。
- 基础包文件树及 Python 压缩模块目录均无 Torch、Depth Anything 或 guidance_worker。
- 中文／英文 × 浅色／深色截图，共20张；无界面回调错误，人工检查摘要、路径及缺失状态。

原始结果：`output/mods-smoke.json`；截图：`output/mods-ui/`。
这些是功能与集成测试，不代表已经证明所有视频的重绘稳定性提升，也不是性能基准。

## 本地测试产物

- 基础包：`tmp/portable-base-test/DLSS5Tool-v2.0.1/`。
- CPU 组件：`tmp/portable-enhancement-test/enhancement/`，约601 MiB，不含模型。
- 测试使用 `tmp/dlss5standaloneV2` 已有 RAFT 和 DAV2-Large 权重；临时测试目录已自动清理，未更改原始权重。
- CPU 环境：Torch 2.8.0+cpu、torchvision 0.23.0+cpu、NumPy 2.5.2、OpenCV 5.0.0.93、einops 0.8.1、PyInstaller 6.22.2。

**CPU 产物仅供本地验证，不是正式增强发行包。** CUDA 构建／显卡驱动矩阵尚未验证。
官方许可证下载因审批服务限流失败，未绕过审批获取；正式组件需补齐许可归档。
Depth Anything 权重的适用条件应以 [上游许可说明](https://github.com/DepthAnything/Depth-Anything-V2#license) 为准。

## 后续打包入口

维护者使用 `scripts/build_enhancement.py`，指定已有深度架构源码、官方许可证和新输出目录。
可选地附带外置权重及授权说明；`--zip` 封装 `mods/enhancement/` 和说明，用户解压到程序目录。
该脚本不安装依赖，不覆盖已有输出目录，不下载权重；CPU/CUDA 版本取决于维护者构建环境。
用户使用说明见 [mods/README.md](mods/README.md)。

复测命令（仅维护者执行，不是用户安装步骤）：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -q
.venv\Scripts\python.exe scripts\mods_smoke.py --component tmp\portable-enhancement-test\enhancement --reference tmp\dlss5standaloneV2 --native --frozen-exe tmp\portable-base-test\DLSS5Tool-v2.0.1\DLSS5Tool.exe --modes 1 2 3
```
