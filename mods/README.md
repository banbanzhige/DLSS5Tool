# 可替换组件 / Replaceable components

基础包独立可用：引导默认关闭，使用自带 DLSS，不需要模型或额外环境。
增强用户将可信来源的附加包直接解压到 **DLSS5Tool.exe 所在目录** 即可，包内自带 `mods` 文件夹，不要再次解压到 mods 内造成 `mods/mods` 嵌套。
**无需安装 Python、pip、PyTorch 或模型源码。不要只复制组件 EXE，保留整个文件夹。**

```text
DLSS5Tool.exe
_internal/                       # 基础包依赖、默认 DLSS
mods/
  nvngx_dlssnr.dll                # 可选：优先于内置 DLSS
  enhancement/                   # 可选：整目录复制、替换
    enhancement.json             # 打包工具生成的协议描述
    guidance_worker.exe
    _internal/                   # 自带推理依赖、Torch、模型架构
    licenses/
    models/                      # 可附带的默认权重，均在 EXE 外
      raft_large_C_T_SKHT_V2-ff5fadd5.pth
      depth_anything_v2_vitl.pth
  models/                        # 用户覆盖权重，优先于组件默认权重
    raft_large_C_T_SKHT_V2-ff5fadd5.pth
    depth_anything_v2_vitl.pth
  dlssnr_on_amd_setup.exe         # 仅预留存放，不自动运行
```

## 使用与替换

1. 轻量用户无需添加组件；没有外部 DLSS 时使用 `_internal/nvngx_dlssnr.dll`。
2. 附加包解压到程序目录后，在「推理模型 → 引导模式」启用所需模式。程序自动识别默认位置，发现组件不会自动开启。
3. 退出应用后替换组件 `models` 中对应 `.pth`，或放入 `mods/models` 覆盖默认权重，也可在路径设置选择其他文件。兼容权重不需要重打包 EXE。
4. 深度支持 Depth Anything V2 的 `vits` / `vitb` / `vitl`，默认根据标准文件名和组件声明自动识别。光流支持 torchvision RAFT-Large state_dict。自定义改名权重若不符合组件默认型号，请在「推理模型 → 深度推理」选择型号。`.pth` 只是格式，其他架构需要组件适配器。
5. 新增文件后刷新或重启。损坏、错架构、不兼容组件明确报错，不自动安装依赖、不静默换模型。关闭引导仍可正常使用。

「设置 → 模型与组件」显示状态及打开目录、刷新、详情三个入口；不要求普通用户配置路径。
「替换 DLL / 模型」是设置页默认折叠的一级分组，可直接编辑路径，没有嵌套折叠。常用推理参数直接显示在「推理模型」页，FP16／双 Stream 位于「设置 → 性能与设备」。详情显示命中路径及问题，“已找到”不代替实际加载验证。
开发版与打包版均自动使用程序根目录的 `mods`。自定义位置位于「替换 DLL / 模型」，旧设置不会被静默覆盖；曾设置临时路径时，可点击「恢复自动识别」一次清除路径覆盖，并恢复自动型号选择。该操作不删除文件。

## 自动检测

- DLSS：存在的指定路径 → 所选／默认 mods 的 `nvngx_dlssnr.dll` → 唯一已识别候选 → 内置库。多个候选不猜选；可手选或强制内置。
- 增强组件：所选目录 `enhancement` → 默认 mods `enhancement`。选中目录已有不完整组件时明确报错，不绕过错误执行另一份。
- 权重：存在的指定路径优先；其后检查所选／默认 mods 的 `models`、`enhancement/models` 及旧 `models/checkpoints` / `torch_home/hub/checkpoints` 布局。自动型号只识别已知文件名，优先用户覆盖目录；同目录多型号优先组件声明的默认型号（旧组件默认 Large），其余按 Small／Base／Large 顺序。手动选定型号不自动更换。
- 仅在引导开启且处理帧时启动固定名称、协议匹配的组件。不扫描系统 Python/PATH，不自动运行安装器。

## 性能、安全与许可

CUDA 版为增强功能主线，自带运行依赖，仍需兼容显卡及系统驱动，用户无需安装 CUDA Toolkit。
“自动”和“GPU”均要求 CUDA 可用，不会静默切到 CPU；CPU 仅保留为主动选择的慢速调试选项。
界面摘要区分 GPU／CPU 构建，首次实际处理的日志报告确认设备和 FP32 精度，详情可查看最近确认的设备。
显存不足时明确报错，建议减小推理长边、减少并行任务或关闭其他显存占用；不会擅自换模型或降精度。
当前仅 SDR 非分块；每个并行导出工作进程独立加载模型，显存／内存随并行数增加。
逐帧处理；跳转、会话切换和明显切镜重置历史。单图光流为零。增强效果不保证对所有素材提升。

DLL 和增强组件是可执行代码，仅使用可信来源。协议检查不是签名或沙箱。
权重以 `weights_only=True` 严格加载，仍须可信来源。用户提供文件不免除许可责任。
Depth Anything V2 Small 权重为 Apache-2.0；Base/Large 为 CC-BY-NC-4.0，见 [官方许可](https://github.com/DepthAnything/Depth-Anything-V2#license)。
基础包只附本说明，不自动收集开发机 mods 内容。

## Maintainer build (not end-user setup)

Use a dedicated Python environment containing matching pinned Torch/torchvision
(CPU or CUDA), NumPy, OpenCV, einops and PyInstaller. Run `scripts/build_enhancement.py`
with `--depth-source` (parent of the complete architecture package), `--depth-license`
(upstream LICENSE), and a new `--output` directory. Optional `--flow-weights` /
`--depth-weights` require `--weights-notice` documenting provenance and licensing.
`--encoder` sets the default depth filename and component default size.
`--zip` includes `mods/enhancement/` and `mods/README.md`, never build intermediates.
Users extract beside the app, not into another mods folder.
Keep dependency licenses; review redistribution rights before publishing.
Base builds exclude Torch, torchvision, architecture and weights.
Protocol v1 exchanges RGBA8 input, float32 motion/depth and reset flags through a
local authenticated pipe. Compatible weight replacement does not change the protocol.

## English quick start

Base use requires no extra models. Extract the complete trusted add-on beside
the app; the archive already contains `mods`. Then enable it on the Guidance tab. No Python, pip, model source
or CUDA Toolkit installation is required. Keep EXE and `_internal` together.
Replace compatible weights in `enhancement/models`, override via `mods/models`,
or select a custom path. RAFT-Large and Depth Anything V2 S/B/L are supported;
depth size is detected automatically from known names and the component default.
Renamed custom checkpoints may require an explicit size under Guidance → Depth.
Status and Open folder / Refresh / Details are under Guidance → Add-ons.
Custom folders and replacement paths are in the top-level Replace DLL / models section, open by default. Common inference settings
are on Guidance; FP16 and dual stream are under Advanced performance.
Development and packaged apps use the same default mods layout.
Other architectures require an adapter.
Auto and GPU require usable CUDA; CPU debugging must be selected explicitly.
No silent CPU fallback. The summary shows build capability, while processing logs
report the actual device and precision. CUDA builds require a compatible GPU/driver.
Missing modules do not affect ordinary use with guidance off. Installers never
run automatically. All upstream code, runtime and model licenses still apply.
