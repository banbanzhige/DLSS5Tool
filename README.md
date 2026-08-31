# DLSS5Tool

给任意视频或图片做 **DLSS 5 Neural Rendering** 的本地小工具：导入即实时预览，调参即时生效，再按同样参数导出。

游戏里的 Feature 18 通常要引擎提供完整 G-Buffer。本工具走 **零引导、仅颜色** 路径，因此不依赖材质、法线、运动矢量或深度，就能把神经渲染当成后处理接到成片上——补真实感、压掉生成图常见的 AI 感与油腻感，同时比重渲或再跑一遍生成模型快得多。

> [!WARNING]
> 本项目处于实验阶段，不是 NVIDIA 官方产品，也未获得 NVIDIA 的赞助或背书。
> NVIDIA、GeForce RTX、DLSS 和 NGX 是其各自权利人的商标或技术。请在公开分发
> 构建产物前自行核对 NVIDIA RTX SDK、神经渲染运行时、FFmpeg/编解码器及相关
> 模型的许可证；本项目许可证不授予这些第三方组件的权利。

## 能做什么

- **任意成片都能喂**：MP4 / AVI / MOV / MKV / M4V / WebM，以及 PNG、JPEG、WebP、BMP、TIFF 等图片。不需要游戏、也不需要 G-Buffer。
- **实时预览**：原图、处理后、可拖动分界线对比；风格、强度、本地色调、本地结构、皮肤结构即时生效。
- **质量取向**：用 Feature 18 神经渲染抬高材质观感与结构细节，减轻塑料感、过度磨皮和油腻高光，而不是再叠一层生成模型。
- **时间成本**：GPU 上一帧神经后处理即可，不必回到引擎重渲，也不必再排队跑一遍扩散/放大模型。
- **导出**：严格时序单会话，或带预热帧的并行分段；优先 NVIDIA NVENC，没有 NVENC 时回退 `libx264`。
- **无需 PyTorch**：隔离的 D3D12 / NGX 宿主进程完成推理，Python 侧只负责解码、预览和写出。

## 工作原理：关掉游戏合同，只吃颜色

游戏内的 DLSS 5 Neural Rendering（NGX **Feature 18**）按完整渲染合同工作，评估时会去读材质、矢量、深度等引导。本工具固定：

| 策略 | 行为 |
| --- | --- |
| `guidance_mode = 0` | 引导关闭。神经渲染忽略光流与深度。 |
| `DLSSNR.Upscaling = 0` | 同分辨率后处理，不是 DLSS 超分辨率。 |
| 零引导快路径 | 整段会话复用一张全零 `DLSSNR.MVec` 和一张全零 `DLSSNR.Depth`，不再每帧上传。 |

创建 / 评估时 **不绑定** 下列 NGX 参数（游戏路径里它们才是必选项；这里故意留空，让运行时在无 G-Buffer 时仍能评估）：

**材质 / G-Buffer**

- `Albedo`
- `GBuffer.Albedo` / `GBuffer.DiffuseAlbedo` / `GBuffer.SpecularAlbedo` / `GBuffer.IndirectAlbedo`
- `GBuffer.Normals` / `GBuffer.Roughness`
- `GBuffer.Metallic` / `GBuffer.Specular` / `GBuffer.Subsurface`
- `GBuffer.MaterialId` / `GBuffer.ShadingModelId`
- `GBuffer.DisocclusionMask`
- `NormalRoughness` / `DLSSD.NormalRoughness`
- `DiffuseAlbedo` / `SpecularAlbedo` / `SpecularHitDistance`

**矢量**

- `MotionVectors`（本工具只提交全零的 `DLSSNR.MVec`）
- `GBuffer.SpecularMvec` / `SpecularMotionVectors`
- `MotionVectors3D` / `MotionVectorsReflection`

**深度与其它游戏侧辅助**

- `Depth` / `DepthHighRes`（本工具只提交全零的 `DLSSNR.Depth`）
- `Jitter.Offset.X` / `Jitter.Offset.Y`
- `ExposureTexture` / `TransparencyMask` / `AnimatedTextureMask` / `IsParticleMask`
- `RayTracingHitDistance`

同分辨率下 `preset`、`ui_correction`、`depth_convention` 也不参与结果。真正会改画面的是风格、强度、本地色调、本地结构、自动皮肤蒙版与皮肤结构。

因此：任意解码出来的 RGB 帧都能进 Feature 18，输出仍是同尺寸的神经增强帧。这不是通用 DLSS 超分集成，也不是光线重建；它是把游戏神经渲染接到离线成片上的实验路径。

## 功能一览

- 拖放导入视频和常见图片。
- `1` / `2` / `3` 切换原图、DLSS、对比；对比视图可拖动分界线，双击复位到 50%，按住 `Alt` 看纯原图。
- 隔离 NGX 工作进程；v2 / 旧版宿主可热切换，失败自动回滚。
- 严格时序单会话导出，或带预热帧的并行分段导出。
- 设置校验、原子写入、损坏后自动恢复。

## 路线图

- [ ] **ComfyUI 节点**：把当前宿主封装成节点，方便接到现有图生视频 / 图生图工作流里，预览与导出共用同一套 Feature 18 参数。

## 当前状态与限制

- 仅 Windows 10/11 与兼容的 NVIDIA GPU；建议使用仍受支持的最新驱动。
- 只有严格单会话模式能保证完整连续的 DLSS 时间历史；并行分段即使预热，也可能在边界处有细微差异。
- 视频解码和颜色转换仍由 Python / OpenCV 执行，整体速度不只取决于 GPU。
- 仓库刻意不包含 NVIDIA SDK、`nvngx_dlssnr.dll`、编译后的宿主 DLL、用户设置、日志或测试媒体。请勿通过 Git LFS 绕过这一许可证边界。

## 系统要求

- Windows 10/11 x64。
- Python 3.10 或更高版本。
- NVIDIA GPU 与兼容驱动。
- Microsoft Visual C++ 2015–2022 Redistributable（运行宿主所需）。
- Visual Studio 2022 Build Tools 与「使用 C++ 的桌面开发」工作负载（仅重新编译宿主时需要）。
- 从合法来源取得并有权使用的 NVIDIA DLSS Neural Rendering 运行时。

## 快速开始

### 1. 安装 Python 依赖

双击 `setup.bat`。脚本会在项目目录创建 `.venv`，并安装 `requirements.txt`：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. 准备原生依赖

从 NVIDIA 的官方仓库克隆 SDK。阅读并接受其许可证后，将它放在构建脚本预期的位置：

```powershell
git clone --depth 1 https://github.com/NVIDIA/DLSS.git third_party/NVIDIA-DLSS
```

然后编译 v2 宿主：

```powershell
.\native_host_v2\build.bat
```

构建成功后，项目根目录应有 `dlssnr_host_v2.dll`。再将你有权使用的
`nvngx_dlssnr.dll` 放到项目根目录。可选的旧版 `dlssnr_host.dll` 只用于兼容回退，
不属于源码发行版。

### 3. 启动

双击 `run.bat`，或运行：

```powershell
.\.venv\Scripts\python.exe gui.py
```

如果 FFmpeg 不在 `PATH` 中，`imageio-ffmpeg` 会提供基础回退版本；若希望使用 NVENC，
建议自行安装包含 `h264_nvenc` 的完整 FFmpeg，并可通过 `FFMPEG_EXE` 指定路径。

## 基本操作

- 点击「导入」或将视频/图片拖入窗口。
- 使用 `1` / `2` / `3` 切换原图、DLSS、对比视图。
- 对比视图可拖动分界线，双击复位至 50%，按住 `Alt` 临时查看纯原图。
- `Space` 播放/暂停，方向键或滚轮逐帧，`Shift` + 方向键跳 1 秒。
- `F11` 或双击画面切换全屏，`Esc` 退出全屏。
- 导出时可在「严格时序」和「视觉无损（并行分段）」之间选择。

程序会在根目录生成 `dlss5_settings.json` 和 `dlss_run.log`。二者都已加入
`.gitignore`；提交 issue 前请检查日志中是否含有不希望公开的信息。

## 开发与测试

安装依赖后运行：

```powershell
.\.venv\Scripts\python.exe -m compileall -q -x third_party .
.\.venv\Scripts\python.exe -m unittest discover -v
```

单元测试使用模拟宿主，不需要 NVIDIA GPU 或专有 DLL。硬件探针位于 `scripts/`，只应在
已正确安装运行时的兼容机器上手动执行。

## 目录结构

| 路径 | 用途 |
| --- | --- |
| `gui.py` | Tk GUI、预览与交互 |
| `app_settings.py` | 设置校验和原子持久化 |
| `dlss_engine.py` | 原生宿主的 `ctypes` 封装 |
| `dlss_host_process.py` | 隔离进程、共享内存与热切换 |
| `video_export.py` | FFmpeg/NVENC 视频写出 |
| `parallel_export*.py` | 分段并行导出 |
| `preview_audio.py` | 本地音频预览 |
| `native_host_v2/` | D3D12/NGX v2 宿主源码与构建脚本 |
| `scripts/` | 需要真实硬件的诊断探针 |
| `tests/` | 不依赖 GPU 的单元测试 |
| `third_party/` | 本地 SDK/参考仓库；不会提交 |

## 参与贡献

请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。提交不得包含 NVIDIA SDK、运行时 DLL、
构建产物、私人媒体或嵌套 Git 仓库。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

## 第三方组件与许可证

项目自有源码按 [MIT License](LICENSE) 发布。该许可证不覆盖 NVIDIA SDK/运行时、
FFmpeg、Python 依赖或参考项目；详情见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
