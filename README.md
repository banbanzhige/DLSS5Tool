<h1 align="center">
  <img src="assets/readme-banner-v1.png" alt="DLSS5Tool — 让视频与图片，在本地获得新的质感。神经渲染 · 2× / 4× 超分 · 实时对比 · 批量导出" width="1200">
</h1>

<p align="center">
  用 DLSS 5 在本地重塑光影与细节，让视频和图片更有真实质感。
</p>

<p align="center">
  <strong>简体中文</strong> ·
  <a href="README.en.md">English</a> ·
  <a href="https://github.com/banbanzhige/DLSS5Tool/releases/latest">下载免安装版</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="CHANGELOG.md">更新日志</a>
</p>

<p align="center">
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/release-v2.1.2-0E7490?style=flat&amp;labelColor=475569" alt="源码版本 v2.1.2" height="20"></a>
  <a href="#快速开始"><img src="https://img.shields.io/badge/platform-Windows_x64-0369A1?style=flat&amp;labelColor=475569" alt="平台 Windows x64" height="20"></a>
  <a href="#2-选择显卡运行库"><img src="https://img.shields.io/badge/GPU-NVIDIA_RTX-0E7490?style=flat&amp;labelColor=475569" alt="显卡 NVIDIA RTX；请按代际选择运行库" height="20"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-0369A1?style=flat&amp;labelColor=475569" alt="项目自有源码采用 MIT 许可证" height="20"></a>
</p>

## 实机演示

<p align="center">
  <a href="img/03.png"><img src="img/03.png" alt="DLSS5Tool 实机运行界面，显示原图与 DLSS 分界对比、参数面板和预览缓存状态" width="760"></a>
</p>

<p align="center"><sub>上图为 v2.1.1 实机截图</sub></p>

## 功能概览

DLSS5Tool 使用 **DLSS 5 Neural Rendering** 增强本地视频与图片，无需接入游戏引擎。

- **画面增强**：默认 / 自然 / 电影三种风格，可调整强度、色调、结构与皮肤蒙版。
- **2× / 4× 超分**：先用 RTX Video 放大，再进行增强；也可保持原尺寸处理。
- **交互对比**：滑动分界、左右并排、缩放与逐帧查看，支持全屏和独立预览窗口。
- **批量导出**：图片与视频混合排队，每项独立保存参数；视频支持 MP4 / MKV / MOV，兼容的原音轨优先保留。
- **HDR 视频**：支持 HDR10 / HLG 高精度处理与 10-bit 导出，使用前请查看下方 HDR 注意事项。
- **光流引导**：模型反推帧间运动，为连续画面增强提供时序参考。更接近真实的画面稳定性和光影准确性。

界面支持简体中文 / English、浅色 / 暗色主题。通过「更多 → 语言」切换语言，重启后生效。

## 效果对比

同一张 AI 生成素材：左图未处理，右图为神经渲染结果。可用于观察材质、光影和细节的变化，不代表所有素材都能获得相同改善
<table>
  <tr>
    <th width="50%">图 1 · 原图</th>
    <th width="50%">图 2 · 神经渲染后</th>
  </tr>
  <tr>
    <td align="center">
      <a href="img/01.png"><img src="img/01.png" alt="未经处理的 AI 生成原图" width="100%"></a>
    </td>
    <td align="center">
      <a href="img/02.png"><img src="img/02.png" alt="经过 DLSS5Tool 神经渲染后的图片" width="100%"></a>
    </td>
  </tr>
</table>

## RAFT / NVOFA 光流对比

开启光流推理能提升视频的整体稳定，光影一致性，减少画面闪烁，黑斑黑影闪动等情况。目前有两种推理方式：RAFT / NVOFA，前者更重质量，后者更重速度。
开启流光图预览后可以直观看出模型推理情况，**颜色表示运动方向，亮度表示位移大小**

<table>
  <tr>
    <th width="33%">07 · 原图 / DLSS 分界对比</th>
    <th width="33%">08 · RAFT 光流</th>
    <th width="33%">09 · NVOFA 光流</th>
  </tr>
  <tr>
    <td align="center"><a href="img/07.png"><img src="img/07.png" alt="同一场景的原图与 DLSS 分界截图" width="100%"></a></td>
    <td align="center"><a href="img/08.png"><img src="img/08.png" alt="RAFT 光流示意：人物和头发的运动区域较连贯" width="100%"></a></td>
    <td align="center"><a href="img/09.png"><img src="img/09.png" alt="NVOFA 光流示意：人物与背景出现较多碎块和局部方向变化" width="100%"></a></td>
  </tr>
</table>

这组截图中，RAFT 的人物轮廓与大面积运动更连贯；NVOFA 在背景、头发和面部有更多碎块与局部方向变化。

### 实测数据

在测试环境中：RTX 4070 SUPER 12 GB / 驱动 616.64。分析长边 512，RAFT-Large 6 次更新 / FP32；NVOFA SLOW / 1×1 网格 / temporal hints 关闭。同参数 DLSS v2、原尺寸 SDR、缓存关闭、禁止回退；每片每后端运行 3 次并交换先后顺序，以下取中位数。

| 素材 | RAFT 整段耗时（有效 fps） | NVOFA 整段耗时（有效 fps） | NVOFA 耗时减少 |
| --- | ---: | ---: | ---: |
| 方形 1440×1440 · 165 帧 | 17.08 s（9.66） | 10.54 s（15.65） | 38.3% |
| 竖屏 1088×1920 · 243 帧 | 20.77 s（11.70） | 14.23 s（17.08） | 31.5% |

单独光流阶段（含输入准备、回读与尺寸恢复，不含 DLSS）的稳态耗时：方形 RAFT / NVOFA 为 **31.21 / 6.19 ms**，竖屏为 **23.21 / 5.29 ms**

| 质量代理指标 ↓（每片 39 对，8-bit RGB 灰度级） | 方形 RAFT | 方形 NVOFA | 竖屏 RAFT | 竖屏 NVOFA |
| --- | ---: | ---: | ---: | ---: |
| 光流回投 MAE · 相同有效区域 | 3.099 | 2.592 | 3.469 | 3.652 |
| DLSS 增强时序残差 · 固定 RAFT 参考 | 1.560 | 1.610 | 1.446 | 1.478 |
| DLSS 增强时序残差 · 固定 NVOFA 参考 | 1.516 | 1.424 | 1.394 | 1.391 |

**结论：本机两段素材上 NVOFA 更快，但画面质量差距并不大。**

测试范围、三轮波动、无光流基线与复现命令见[完整实测说明](docs/experiments/README_FLOW_BENCHMARK.md)，数值与组件哈希见[公开结果 JSON](docs/experiments/README_FLOW_BENCHMARK_20260910.json)。



## 快速开始

需要 **Windows 10 / 11 x64、兼容的 NVIDIA RTX 显卡及驱动**，以及 [Microsoft Visual C++ x64 运行库](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist)。显卡适配方式见第 2 步。

### 1. 下载并完整解压

前往 [Releases 下载免安装版](https://github.com/banbanzhige/DLSS5Tool/releases/latest)，不要选择页面底部的 `Source code` 源码包。

| 你的需求 | 选择哪个包 |
| --- | --- |
| 普通增强、2× / 4× 超分 | **轻量版（推荐）**：`DLSS5Tool-v版本号-win64.zip` |
| 还需要光流模型 | **完整版**：`DLSS5Tool-v版本号-win64-full.zip.001` 起的所有分卷 |
| 已有轻量版，只补充模型功能 | **推理附加包**：`DLSS5Tool-v版本号-win64-addon.zip.001` 起的所有分卷 |

- 完整版已包含附加包，不必重复下载。分卷请下载同一版本的全部文件，放在同一目录，用 7-Zip 打开 `.001` 解压。
- 附加包请在关闭程序后解压到 `DLSS5Tool.exe` 所在目录，不要套成 `mods/mods`。
- **完整解压到可写目录后再运行**，保持 `DLSS5Tool.exe` 与 `_internal` 目录在一起。无需另装 Python 或 FFmpeg。

### 2. 选择显卡运行库

| 显卡 | 使用方式 |
| --- | --- |
| RTX 40 系 | 直接使用包内默认运行库 |
| RTX 30 系 | 下载同一 Release 的 `30系.zip`，按下方说明放置 DLL |
| RTX 50 系 | 下载同一 Release 的 `50系.zip`，按下方说明放置 DLL |

RTX 30 / 50 系请先关闭程序，将对应附件中的 `nvngx_dlssnr.dll` 放入程序同级的 `mods` 文件夹：

```text
mods\nvngx_dlssnr.dll
```

无需覆盖 `_internal`。如果曾手动配置 DLL 路径，请一并检查设置。RTX 30 系使用社区适配运行库，兼容性取决于显卡与驱动组合，并非 NVIDIA 官方支持承诺。

### 3. 导入、对比、导出

1. 打开 `DLSS5Tool.exe`，拖入图片或视频，或点击「选择文件」。
2. 切换到「DLSS」或「对比」，在「画面效果」页选择风格、调整强度。按 `3` 可快速进入分界对比。
3. 在「设置」页选择输出格式和画质；需要放大时再开启 2× / 4× 超分。
4. 导出当前素材，或「加入队列」后批量处理。

首次使用可先保持默认设置，从短视频或单张图片试起。详细操作见[使用指南](docs/USER_GUIDE.md)。

## 使用须知

- **效果与速度因素材和硬件而异**：交互对比不代表模型能实时处理。高分辨率、4× 超分和光流会增加耗时及显存占用。
- **光流按需开启**：安装完整版或附加包后，在「推理模型」中选择模式；重启会记住上次选择，环境检查通过后自动恢复，检查失败才关闭并提示原因。首次使用检查默认 RAFT-Large，通过后启用；已保存的关闭模式保持关闭。支持 SDR 和 HDR 独立分析副本，不支持分块时序引导；静态图片自动跳过光流，不影响大图分块，也不改动视频偏好。
- **HDR 并非完整元数据透传**：界面预览会映射为 SDR；导出保留基础 HDR10 / HLG 色彩标签，不保留 Dolby Vision / HDR10+ 动态元数据及部分源 HDR 元数据。暂不支持静态 HDR 图片，详见[输出与画质说明](docs/USER_GUIDE.md#输出与画质说明)。

## 常见问题

**启动失败，或提示缺少 DLL？**

确认已完整解压，EXE 与 `_internal` 在一起，并安装 x64 Visual C++ 运行库；不要混用不同版本的程序文件。

如果运行的是 `Source code` 源码包，安装 Python 依赖并不会补齐原生 DLL，请按下方[从源码构建与启动](#从源码构建与启动)准备运行环境。`missing dlssnr_host.dll` 也可能是找不到 v2 宿主后尝试旧版宿主的结果，不代表必须下载旧版 DLL。

**预览卡顿，或高倍率超分失败？**

先降低播放质量，尝试较小素材或 2× 超分，并暂时关闭可选光流。增大缓存不能加快尚未计算的首遍处理。

**如何更新？**

通过「更多 → 检查更新」，或直接下载最新 Release。新更新器在存在匹配包时只下载当前安装形态的变化文件，校验后再次确认退出，由独立助手安装。找不到匹配包时，轻量版可下载整包，完整版引导到发布页选择完整版或同版本轻量版＋附加包。旧版用户首次仍需手动完整解压新版到新目录；此功能需包含更新助手的新发行包和发布端更新附件配合。详见[更新说明](docs/USER_GUIDE.md)。

**仍然无法使用？**

通过「更多 → 一键诊断」生成报告，在 [Issues](https://github.com/banbanzhige/DLSS5Tool/issues) 附上软件版本、显卡、驱动、复现步骤及诊断日志。公开前请检查日志中的本地路径等隐私信息。

## 从源码构建与启动

以下步骤适用于**当前源码布局**。普通使用请选择免安装版；GitHub 的 `Source code` 包不包含编译后的 DLL 和 NVIDIA SDK。`setup.bat` **只安装 Python 依赖，不编译宿主，也不安装 NVIDIA 运行库**；即使界面能打开，缺少这些组件仍无法处理素材。

### 1. 准备构建环境

- Windows 10 / 11 x64、Python 3.10+（含 Tkinter 和 `py` 启动器）、Git。
- Visual Studio 2022 Build Tools，安装「使用 C++ 的桌面开发」工作负载及 Windows SDK。仅安装 Visual C++ 运行库不足以编译。
- 实际处理需要兼容的 NVIDIA 显卡、驱动，以及有权使用且匹配显卡的 NVIDIA 运行库。

在源码根目录执行以下命令，每一步成功后再继续。

### 2. 安装 Python 依赖并编译 DLSS 宿主

```powershell
.\setup.bat
git clone --depth 1 https://github.com/NVIDIA/DLSS.git third_party/NVIDIA-DLSS
# 阅读并接受 SDK 许可证后执行：
.\native\host_v2\build.bat
```

若 SDK 已存在，无需重复 clone。编译成功会生成 `runtime\dlssnr_host_v2.dll`。默认自动选择 v2 宿主，不需要另行寻找或将它重命名为旧版 `dlssnr_host.dll`。

### 3. 准备 DLSS 运行库

将有权使用、匹配显卡的 `nvngx_dlssnr.dll` 放到源码根目录下的 `runtime` 文件夹。已有同版本免安装包时，可从其 `_internal` 提取适用的运行库；RTX 30 / 50 系需按对应附件选择，参见[显卡运行库说明](#2-选择显卡运行库)。

`dlssnr_host_v2.dll` 是本项目编译的宿主，`nvngx_dlssnr.dll` 是另行提供的 NVIDIA 运行库；编译宿主不会生成后者。已有 `mods` 替换库或自定义运行库路径时，请在「运行库与模型路径」确认实际选中的文件。

### 4. 可选：构建 2× / 4× 超分组件

需要超分时，另行准备 RTX Video SDK 1.1，阅读并接受其许可证，解压到 `third_party\RTX_Video_SDK`，或设置环境变量 `NV_RTX_VIDEO_SDK` 指向 SDK 根目录，然后执行：

```powershell
.\native\vsr_host\build.bat
```

脚本会生成 `runtime\vsr_host.dll`，并从 SDK 复制 `nvngx_vsr.dll` 到 `runtime`。不需要超分时可跳过此步。

### 5. 检查文件并启动

```text
源码根目录/
├── run.bat
├── gui.py
└── runtime/
    ├── dlssnr_host_v2.dll
    ├── nvngx_dlssnr.dll
    ├── vsr_host.dll          # 仅超分需要
    └── nvngx_vsr.dll         # 仅超分需要
```

```powershell
.\run.bat
```

源码开发统一通过 `run.bat` 启动，优先使用 `.venv`；设置、队列和开发日志保存在 `var`。上述步骤用于基础增强及可选超分，模型推理组件需[独立构建](mods/README.md#维护者深度与构建)。测试及生成免安装 EXE 的步骤见[开发指南](docs/development/BUILDING.md#测试与打包)。

**旧版路径提醒：** v2.1.1 使用 `native_host_v2\build.bat`、`native_vsr_host\build.bat`，DLL 放在源码根目录；当前版本使用 `native\host_v2`、`native\vsr_host` 和 `runtime`。请按所用版本的说明操作，不要混用目录或宿主二进制。

## 更多文档

- [使用指南](docs/USER_GUIDE.md)：快捷键、模型设置、导出说明与详细排障
- [更新日志](CHANGELOG.md)
- [开发指南](docs/development/BUILDING.md) · [参与贡献](CONTRIBUTING.md) · [技术文档索引](docs/README.md)
- [安全问题反馈](SECURITY.md)

## 许可证

项目自有源码按 [MIT License](LICENSE) 发布。免安装版附带的 `nvngx_dlssnr.dll`、NVIDIA SDK、FFmpeg 和 Python 依赖仍受各自上游许可约束，不属于本仓库 MIT 授权范围。本候选的完整/附加包策略仅附 RAFT 权重。详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 及包内 `mods/enhancement/licenses`。
