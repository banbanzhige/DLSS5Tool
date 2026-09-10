# Third-party notices

DLSS5Tool 的 MIT 许可证只覆盖本仓库的自有源码。以下组件保持其各自许可证、版权、
商标和分发限制；本文件只是项目清单，不替代原始许可证，也不构成法律意见。

## NVIDIA Optical Flow API headers

- `dlss5tool/nvofa.py` adapts the official NVIDIA Optical Flow API 2.0 CUDA header declarations, originally Copyright (c) 2020 NVIDIA Corporation, under BSD-3-Clause.
- Full terms: [NVIDIA-Optical-Flow-Headers-LICENSE.txt](licenses/NVIDIA-Optical-Flow-Headers-LICENSE.txt). The standalone component also carries this license in its licenses directory.
- Sources: [CUDA declarations](https://github.com/NVIDIA/NVIDIAOpticalFlowSDK/blob/master/nvOpticalFlowCuda.h) and [common declarations](https://github.com/NVIDIA/NVIDIAOpticalFlowSDK/blob/master/nvOpticalFlowCommon.h).
- The driver optical-flow DLL is loaded from Windows System32 and is not distributed by this project. Header licensing does not grant redistribution rights to unrelated SDK/driver binaries.

## torchvision RAFT implementation

- 增强组件直接使用 torchvision 0.23 的原始 RAFT-Large 推理实现；生产路径不再包含自定义 final-only adapter。
- 原始版权与 BSD-3-Clause 条款保留于 `licenses/torchvision-LICENSE.txt`，冻结增强组件同时保留 torchvision 的许可证元数据。

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

## NVIDIA RTX Video SDK / Video Super Resolution

- 上游：[NVIDIA RTX Video SDK](https://developer.nvidia.com/rtx-video-sdk)
- 许可证：随 SDK 提供的 NVIDIA RTX Video SDK License
- 文件名：`nvngx_vsr.dll`
- 在本项目中的用途：在 DLSS 5 Neural Rendering 前执行 2× / 4× SDR 或 10-bit HDR 超分
- 分发策略：SDK、头文件和运行时不纳入源码仓库；发行者必须从 NVIDIA 官方来源取得，
  并在便携包中同时保留原始许可证文件和适用的专有条款

本仓库自有的 `native_vsr_host` 源码不复制 NVIDIA 示例源码，但编译时需要单独安装
RTX Video SDK 1.1 或兼容版本。公开或商业发布前，发行者仍需复核当时有效的 NVIDIA
分发、通知与商标要求。

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
imageio-ffmpeg、Pillow 和 tkinterdnd2。它们不受本项目 MIT 许可证重新许可。

## Lucide icons

- 上游：[lucide](https://github.com/lucide-icons/lucide)
- 许可证：ISC；下列 Feather 衍生图标同时遵循 MIT
- 版权：Copyright (c) 2026 Lucide Icons and Contributors
- 在本项目中的用途：`dlss5tool/ui_icons.py` 内嵌的 24×24 描边图标，由 Pillow 栅格化后用于播放条与队列工具按钮

### Lucide ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any purpose with or
without fee is hereby granted, provided that the above copyright notice and this permission
notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO
THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT
SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR
ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF
CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE
OR PERFORMANCE OF THIS SOFTWARE.

### Feather-derived icons — MIT License

Lucide identifies several icons used here, including chevrons, minus, plus, trash and
more-horizontal, as derived from the Feather project.

Copyright (c) 2013-present Cole Bemis

Permission is hereby granted, free of charge, to any person obtaining a copy of this software
and associated documentation files (the "Software"), to deal in the Software without
restriction, including without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.

`imageio-ffmpeg` 或系统 FFmpeg 可能包含受 LGPL、GPL 及编解码器专利条款约束的组件，
具体取决于使用的构建。二进制发行者有责任审查实际随包提供的 FFmpeg 构建及目标地区要求。
