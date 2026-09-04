# Third-party notices

DLSS5Tool 的 MIT 许可证只覆盖本仓库的自有源码。以下组件保持其各自许可证、版权、
商标和分发限制；本文件只是项目清单，不替代原始许可证，也不构成法律意见。

## NVIDIA DLSS / NGX SDK

- 上游：[NVIDIA/DLSS](https://github.com/NVIDIA/DLSS)
- 许可证：NVIDIA RTX SDKs License 及随 SDK 提供的补充条款
- 在本项目中的用途：编译 `native_host_v2` 所需的头文件和静态导入库
- 分发策略：不纳入本仓库；开发者在 `third_party/NVIDIA-DLSS` 单独克隆并接受其条款

NVIDIA SDK 许可证包含专有权利、分发、商标、通知和使用范围要求。公开发布包含 SDK
材料或与之链接的二进制前，请以当时随 SDK 提供的原始条款为准。

## NVIDIA DLSS Neural Rendering runtime

- 文件名：`nvngx_dlssnr.dll`
- 在本项目中的用途：运行时加载的 Neural Rendering 模型/运行时
- 分发策略：不纳入源码仓库，也不作为独立文件再分发；用户必须从合法授权来源取得

## 参考实现

以下仓库用于研究和构建参考，不会作为 vendored 源码提交：

- [jlrouzies-fr/DLSS5-Feeder](https://github.com/jlrouzies-fr/DLSS5-Feeder) — MIT，
  Copyright (c) 2026 Jean-Laurent ROUZIES。
- [NIGos/dlss5-dx11-bridge](https://github.com/NIGos/dlss5-dx11-bridge) — MIT，
  Copyright (c) 2026 NIGos。
- [kibblerz/DLSS5-Reshade-AIO](https://github.com/kibblerz/DLSS5-Reshade-AIO) —
  用于核对 Feature 18 的 SDR、scRGB、HDR10/PQ、HLG 资源与色彩合同。
- [SAOG0721/Magpie](https://github.com/SAOG0721/Magpie) — 用于核对捕获帧 DLSSNR
  集成、运行时身份和 RTX 40/50 社区 DLL。
- [faisalkindi/DLSS5oneclick](https://github.com/faisalkindi/DLSS5oneclick)、
  [kayle2203/dlssnr-signature-repair](https://github.com/kayle2203/dlssnr-signature-repair) 与
  [Dagherbou/OptiScaler_DLSSNR](https://github.com/Dagherbou/OptiScaler_DLSSNR) —
  用于运行时兼容性、签名修复和显卡代际适配调研。

若未来复制或修改了这些项目的实质性代码，必须在相关文件和发行物中保留其完整 MIT
版权与许可声明。

## Python 与媒体组件

运行时依赖由 PyPI 单独安装，当前清单位于 `requirements.txt`：NumPy、OpenCV Python、
imageio-ffmpeg 和 tkinterdnd2。它们不受本项目 MIT 许可证重新许可。

`imageio-ffmpeg` 或系统 FFmpeg 可能包含受 LGPL、GPL 及编解码器专利条款约束的组件，
具体取决于使用的构建。二进制发行者有责任审查实际随包提供的 FFmpeg 构建及目标地区要求。
