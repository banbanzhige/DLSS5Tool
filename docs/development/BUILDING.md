# 开发指南

[项目首页](../../README.md) · [English](BUILDING.en.md)

普通使用请选择免安装版；修改代码或自行构建时，需要：

- Windows 10 / 11 x64、Python 3.10+（含 Tkinter 和 `py` 启动器）、Git。
- 编译原生宿主时，需要 Visual Studio 2022 Build Tools 的「使用 C++ 的桌面开发」工作负载及 Windows SDK。
- 实际运行神经渲染时，需要兼容的 NVIDIA 显卡、驱动和有权使用的 NVIDIA 运行库。

在项目根目录执行：

```powershell
# 1. 创建 .venv 并安装 Python 依赖
.\setup.bat

# 2. 获取 NVIDIA DLSS SDK，阅读并接受其许可证后编译宿主
git clone --depth 1 https://github.com/NVIDIA/DLSS.git third_party/NVIDIA-DLSS
.\native\host_v2\build.bat

# 3. 把有权使用的 nvngx_dlssnr.dll 放到 runtime/ 后启动
.\run.bat
```

如需 2× / 4× 超分，另行准备 RTX Video SDK 1.1，解压到 `third_party/RTX_Video_SDK`，或将环境变量 `NV_RTX_VIDEO_SDK` 指向 SDK 根目录，然后执行：

```powershell
.\native\vsr_host\build.bat
```

该脚本会将 `vsr_host.dll` 和 SDK 中的 `nvngx_vsr.dll` 放到 `runtime/`；编译中间产物进入 `build/native/`。

源码集中在 `dlss5tool/`，开发设置、队列和日志集中在 `var/`。开发唯一入口是根目录 `run.bat`
（内部启动 `gui.py`）。目录约定与文档索引见 [docs/README.md](../../docs/README.md)。

## 测试与打包

单元测试不需要 GPU、NVIDIA SDK 或专有 DLL；真实 GPU / HDR / 超分效果需要另行实机验证。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

构建完整免安装包前，需要准备 `dlssnr_host_v2.dll`、`nvngx_dlssnr.dll`、`vsr_host.dll`、`nvngx_vsr.dll`、应用图标，以及 RTX Video SDK 的原始许可证文件。打包脚本默认安装构建依赖、运行测试，再生成便携目录和 ZIP：

```powershell
.\scripts\build_release.ps1
```

增强组件需独立构建，重打基础应用不会更新推理组件。构建方式见 [mods 维护者说明](../../mods/README.md#maintainer-build-not-end-user-setup)，发行流程见 [打包记录与索引](../../docs/release/PACKAGING_INDEX.md)。

开发约定见 [CONTRIBUTING.md](../../CONTRIBUTING.md)，安全问题请遵循 [SECURITY.md](../../SECURITY.md)。性能实验与验证记录统一放在[技术文档](../../docs/README.md)，不放入产品首页。
