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
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/release-v2.3.3-0E7490?style=flat&amp;labelColor=475569" alt="源码版本 v2.3.3" height="20"></a>
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
- **视频插帧**：导出可选 2× / 3× / 4×；3× / 4× 为实验模式，可能出现运动偏差。超分与插帧各有独立预览开关，默认关闭。
- **交互对比**：真实原图与增强结果支持滑动分界、左右并排、缩放与逐帧查看，也可全屏或分离预览窗口；画面显示实际超分、插帧倍率，显示操作保持预缓存连续。
- **批量导出**：图片、视频与图片序列混合排队，每项独立保存参数；支持拖动多选、右键批量操作、快捷键与撤销移除，长文件名可悬停查看完整路径。视频支持 MP4 / MKV / MOV，兼容的原音轨优先保留。
- **图片序列转视频**：连续编号、同尺寸的图片可作为一个任务，设置原始帧率后进行增强、超分和插帧，导出无音轨视频；支持 SDR PNG/JPG，当前源码还支持符合要求的 PQ / HLG HDR PNG 序列。
- **HDR / GPU 导出**：支持 HDR10 / HLG 高精度处理与 10-bit 导出；符合条件时使用 GPU 色彩转换，并将增强结果和插帧画面在显存中直接交给编码器，减少 CPU 往返搬运。
- **导出进度与诊断**：长视频导出前显示时间戳检查进度，GPU 导出失败或取消不覆盖已有文件；一键诊断可查看实际生效设置和近期导出信息。
- **光流引导**：模型反推帧间运动，为连续画面增强提供时序参考。更接近真实的画面稳定性和光影准确性。
- **DLSS 渲染 GPU**：默认选用高性能 NVIDIA 显卡；显示器接在核显上时，DLSS 仍运行在 NVIDIA GPU。多卡可在设置中指定。
- **文件级更新**：安装含更新助手的发行包后，可通过「关于 → 检查更新」只下载变化文件。

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
| 普通增强、2× / 4× 超分、插帧 | **轻量版（推荐）**：`DLSS5Tool-v版本号-win64.zip` |
| 还需要光流模型 | **完整版**：`DLSS5Tool-v版本号-win64-full.zip.001` 起的所有分卷 |
| 已有轻量版，只补充模型功能 | **推理附加包**：`DLSS5Tool-v版本号-win64-addon.zip.001` 起的所有分卷 |

- 完整版已包含附加包，不必重复下载。分卷请下载同一版本的全部文件，放在同一目录，用 7-Zip 打开 `.001` 解压。
- 附加包请在关闭程序后解压到 `DLSS5Tool.exe` 所在目录，不要套成 `mods/mods`。
- **完整解压到可写目录后再运行**，保持 `DLSS5Tool.exe` 与 `_internal` 目录在一起。无需另装 Python 或 FFmpeg。

### 2. 选择显卡运行库

提前确认自己的显卡型号，前往 [Releases](https://github.com/banbanzhige/DLSS5Tool/releases/tag/zip) 下载显卡运行库

| 显卡 | 使用方式 |
| --- | --- |
| RTX 40 系 | 直接使用包内默认运行库 |
| RTX 30 系 | 下载同一 Release 的 `30系.zip`，按下方说明放置 DLL |
| RTX 50 系 | 下载同一 Release 的 `50系.zip`，按下方说明放置 DLL |

将匹配显卡的[nvngx_dlssnr.dll](https://github.com/banbanzhige/DLSS5Tool/releases/tag/zip)下载解压，请先关闭程序，将对应附件中的 `nvngx_dlssnr.dll` 放入程序同级的 `mods` 文件夹：

```text
mods\nvngx_dlssnr.dll
```

建议使用 `mods`，不要覆盖 `_internal` 中的内置 DLL，以免修改受管文件后无法使用增量更新。如果曾手动配置 DLL 路径，请一并检查设置。RTX 30 系使用社区适配运行库，兼容性取决于显卡与驱动组合，并非 NVIDIA 官方支持承诺。

### 3. 导入、对比、导出

1. 打开 `DLSS5Tool.exe`，拖入图片或视频，或点击「选择文件」。
2. 切换到「DLSS」或「对比」，在「画面效果」页选择风格、调整强度。按 `3` 可快速进入分界对比。
3. 在「画面效果」的导出区或「设置」页选择输出格式和画质；需要放大或插帧时再开启对应选项。超分 / 插帧旁的预览开关默认关闭，不影响导出。
4. 导出当前素材，或「加入队列」后批量处理。

首次使用可先保持默认设置，从短视频或单张图片试起。详细操作见[使用指南](docs/USER_GUIDE.md)。

### 4. 图片序列转视频（可选）

在「队列 → 添加图片序列」选择任意一帧，例如 `frame_0001.png`。程序会检查同目录、相同前后缀及扩展名、连续编号且同尺寸的图片，再将整组图片加入队列。

HDR 图片序列支持已加入当前源码，但已有本地候选包尚未包含；下载版本请以 Releases 中实际提供的包为准。

- **设置原始帧率**：按素材实际帧率填写，再按需开启超分或插帧；输出为无音轨视频。
- **普通图片序列**：选择「SDR / sRGB」，使用 8 位、三通道 RGB PNG/JPG。
- **HDR 图片序列（当前源码）**：使用已按 PQ 或 HLG 编码的全范围 BT.2020、16 位三通道 RGB PNG，并在「输入图片色彩」选择对应类型。保留 HDR 导出需开启「HDR 高精度处理」。16 位不等于 HDR，不支持线性 EXR、HDR TIFF、灰度或带透明通道的 HDR 序列。

导入后若移动或修改原图，请重新导入。详细输入要求见[图片序列说明](docs/USER_GUIDE.md#图片序列)。

## 使用须知

- **效果与速度因素材和硬件而异**：交互对比不代表模型能实时处理。高分辨率、4× 超分、插帧和光流会增加耗时及显存占用。
- **插帧**：导出可选 2× / 3× / 4×；3× / 4× 为实验模式，可能出现运动偏差。超分与插帧预览默认关闭，不改变导出选择。
- **GPU 导出按条件启用**：是否使用 GPU 色彩转换和直连取决于显卡、组件、分辨率与编码设置；不符合条件的配置继续使用既有编码路径，不保证所有素材都有固定提速。可结合导出日志和一键诊断确认实际路径。
- **光流默认关闭**：新用户先以基础渲染速度使用；需要光流时安装完整版或附加包，在「推理模型」中主动选择模式。重启会记住已有选择，已启用的模式通过环境检查后恢复，检查失败则关闭并提示原因。轻量版未装附加包时无需运行光流检查。同一张 NVIDIA 显卡上使用 RAFT 时，光流可自动直连 DLSS。支持 SDR 和 HDR 独立分析副本，不支持分块时序引导；静态图片自动跳过光流，不影响大图分块，也不改动视频偏好。
- **DLSS 渲染 GPU**：默认按高性能顺序使用 NVIDIA 显卡，显示器接在核显上时仍在独显上运行；多卡可在设置中指定。
- **HDR 导出与预览**：导出写入 HDR10 / HLG 色彩标签（BT.2020、PQ/HLG、limited range）与 10-bit HEVC；界面预览仍映射为 SDR。不复制 Dolby Vision / HDR10+ 动态元数据。暂不支持单张静态 HDR 图片；HDR 图片序列要求见上文。详见[输出与画质说明](docs/USER_GUIDE.md#输出与画质说明)。

## 常见问题

**启动失败，或提示缺少 DLL？**

确认已完整解压，EXE 与 `_internal` 在一起，并安装 x64 Visual C++ 运行库；不要混用不同版本的程序文件。

如果运行的是 `Source code` 源码包，安装 Python 依赖并不会补齐原生 DLL，请按[开发指南](docs/development/BUILDING.md)准备运行环境。`missing dlssnr_host.dll` 也可能是找不到 v2 宿主后尝试旧版宿主的结果，不代表必须下载旧版 DLL。

**预览卡顿，或高倍率超分失败？**

先降低播放质量，尝试较小素材或 2× 超分，并暂时关闭可选光流。增大缓存不能加快尚未计算的首遍处理。

**如何更新？**

通过「关于 → 检查更新」，或直接下载最新 Release。增量包仅适配上一个正式版本：**v2.3.3 对应 v2.3.2 → v2.3.3 的轻量版／完整版更新包**；更早版本不提供直达本版的增量包。

若提示 `_internal/nvngx_dlssnr.dll` 被修改，或希望保留 v2.2.2 / v2.3.0 已安装的官方推理组件以减少下载，请看 [旧版省流量升级说明](docs/release/UPGRADE_v2.3.1.md)。无需一律重新下载完整版；不要只替换 EXE。

有匹配包时，更新器只下载当前安装形态的变化文件，校验后再次确认退出，由独立助手安装。没有匹配包时，轻量版可下载整包，完整版引导到发布页选择完整版或同版本轻量版＋附加包。没有更新助手的旧版请手动将新版完整解压到新目录；增量更新需发布页提供对应附件。详见[更新说明](docs/USER_GUIDE.md)。

**仍然无法使用？**

通过「关于 → 导出诊断」生成报告，在 [Issues](https://github.com/banbanzhige/DLSS5Tool/issues) 附上软件版本、显卡、驱动、复现步骤及诊断日志。公开前请检查日志中的本地路径等隐私信息。


## 更多文档

- [使用指南](docs/USER_GUIDE.md)：快捷键、模型设置、导出说明与详细排障
- [更新日志](CHANGELOG.md)
- [开发指南](docs/development/BUILDING.md) · [参与贡献](CONTRIBUTING.md) · [技术文档索引](docs/README.md)
- [安全问题反馈](SECURITY.md)

## 许可证

项目自有源码按 [MIT License](LICENSE) 发布。免安装版附带的 `nvngx_dlssnr.dll`、NVIDIA SDK、FFmpeg 和 Python 依赖仍受各自上游许可约束，不属于本仓库 MIT 授权范围。完整版 / 附加包仅附 RAFT 权重。详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 及包内 `mods/enhancement/licenses`。
