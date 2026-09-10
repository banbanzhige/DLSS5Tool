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

## 功能概览

DLSS5Tool 使用 **DLSS 5 Neural Rendering** 增强本地视频与图片，无需接入游戏引擎或自行提供材质、法线、深度数据。这是面向已有素材的画面后处理工具，不是游戏插件，也不提供插帧。

- **画面增强**：默认 / 自然 / 电影三种风格，可调整强度、色调、结构与皮肤蒙版。
- **2× / 4× 超分**：先用 RTX Video 放大，再进行增强；也可保持原尺寸处理。
- **交互对比**：滑动分界、左右并排、缩放与逐帧查看，支持全屏和独立预览窗口。
- **批量导出**：图片与视频混合排队，每项独立保存参数；视频支持 MP4 / MKV / MOV，兼容的原音轨优先保留。
- **HDR 视频**：支持 HDR10 / HLG 高精度处理与 10-bit 导出，使用前请查看下方 HDR 注意事项。
- **光流引导（可选）**：估计帧间运动，为连续画面增强提供时序参考。效果因素材而异；现阶段深度参考失效，相关推理暂时下线。

界面支持简体中文 / English、浅色 / 暗色主题。通过「更多 → 语言」切换语言，重启后生效。

当前源码版本为 **v2.1.2，发行包尚未上传**：深度暂时下线；光流后端可选 RAFT 或 NVOFA，默认仍 RAFT。开发入口为根目录 `run.bat`。下方下载入口指向已发布版本，不代表本版已经上传。

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
- **光流按需开启**：安装完整版或附加包后，在「推理模型」中选择模式，环境检查通过后启用；每次启动默认关闭。目前只支持 SDR、非分块处理，单张图片没有帧间光流。
- **HDR 并非完整元数据透传**：界面预览会映射为 SDR；导出保留基础 HDR10 / HLG 色彩标签，不保留 Dolby Vision / HDR10+ 动态元数据及部分源 HDR 元数据。暂不支持静态 HDR 图片，详见[输出与画质说明](docs/USER_GUIDE.md#输出与画质说明)。

## 常见问题

**启动失败，或提示缺少 DLL？**

确认已完整解压，EXE 与 `_internal` 在一起，并安装 x64 Visual C++ 运行库；不要混用不同版本的程序文件。

**预览卡顿，或高倍率超分失败？**

先降低播放质量，尝试较小素材或 2× 超分，并暂时关闭可选光流。增大缓存不能加快尚未计算的首遍处理。

**如何更新？**

通过「更多 → 检查更新」，或直接下载最新 Release。应用内下载的是轻量版；关闭旧版后，将新包解压到新目录，并按显卡配置运行库。需要模型功能时，使用同版本完整版或附加包。

**仍然无法使用？**

通过「更多 → 一键诊断」生成报告，在 [Issues](https://github.com/banbanzhige/DLSS5Tool/issues) 附上软件版本、显卡、驱动、复现步骤及诊断日志。公开前请检查日志中的本地路径等隐私信息。

## 更多文档

- [使用指南](docs/USER_GUIDE.md)：快捷键、模型设置、导出说明与详细排障
- [更新日志](CHANGELOG.md)
- [开发指南](docs/development/BUILDING.md) · [参与贡献](CONTRIBUTING.md) · [技术文档索引](docs/README.md)
- [安全问题反馈](SECURITY.md)

## 许可证

项目自有源码按 [MIT License](LICENSE) 发布。免安装版附带的 `nvngx_dlssnr.dll`、NVIDIA SDK、FFmpeg 和 Python 依赖仍受各自上游许可约束，不属于本仓库 MIT 授权范围。本候选的完整/附加包策略仅附 RAFT 权重，不附深度权重。详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 及包内 `mods/enhancement/licenses`。
