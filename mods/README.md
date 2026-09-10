# 光流组件 / Optical flow add-on

普通增强和超分不需要本组件。完整可信附加包解压到 **DLSS5Tool.exe 所在目录**，不要解压成 mods/mods。
保留整个 enhancement 目录，不能只复制 EXE。用户无需安装 Python、PyTorch 或 CUDA Toolkit。

```text
DLSS5Tool.exe
_internal/
mods/
  nvngx_dlssnr.dll                 # 可选替换运行库
  enhancement/
    guidance_worker.exe
    enhancement.json
    _internal/
    licenses/
  models/
    raft_large_C_T_SKHT_V2-ff5fadd5.pth
```

## 使用

1. 在“推理模型”选择光流后端，再选择“仅光流”。重启记住上次模式，环境检查通过后自动恢复，失败才关闭并提示原因；首次使用检查默认 RAFT-Large，仅通过后启用；上次关闭时保持关闭。
2. 可选 RAFT-Large，512 长边、6 次迭代、FP32、当前帧到上一帧。它需要兼容的 RAFT 权重。
3. NVOFA 是当前组件提供的硬件光流选项，无需权重和迭代，仍需支持的 NVIDIA GPU 与驱动。
4. 启用前进行两帧检查。NVOFA 初始化失败时提示并尝试 RAFT（需权重）；处理中失败停止，不中途换算法。
5. “设置 → 模型与组件”查看状态和详情；“替换 DLL / 模型”可选自定义组件目录或权重路径。

默认采用 RAFT-Large，不承诺实时 30fps。开发从仓库根目录 `run.bat` 启动。
仅支持 SDR、非分块；单张图和首帧/reset 为零光流。切镜、seek、换会话重置历史。
自动/CUDA 不静默切到 CPU。CPU 仅供主动选择的 RAFT 调试。资源不足请降低长边或并发后重试。
显示量程只影响分析图，不影响增强结果。分析图导出无音频。

## 更新、路径和安全

退出应用后更新完整组件目录。所选目录中已有不完整组件会报错，不绕过去执行另一份。
权重查找：存在的指定路径优先，然后查所选/default mods 的 models、enhancement/models 与旧 checkpoint 布局。
组件支持声明不是签名或沙箱，只使用可信来源的 EXE、DLL、权重。权重以 weights_only=True 加载。

本候选完整/附加包不附深度权重；不会删除旧安装中的文件，建议新目录安装以获得精简体积。
组件仍带 Torch 和深度架构代码，不是无 Torch 小包；保留所附代码/依赖的原始许可。
主程序轻量包只带本说明，不收集开发机 mods 文件。

## 维护者：深度与构建

现阶段 DLSS 深度参考失效，公开界面暂时屏蔽深度推理。仓库保留 DAV2 推理/上传管道；
维护者设 DLSS5TOOL_ENABLE_DEPTH=1 后重启，并自行提供兼容的合法权重，可验证内部模式。
Small/Base/Large 权重许可不同，需核对上游；公开包未附权重不等于免除架构代码许可。
混合深度 + NVOFA 本轮拒绝，维护者混合测试使用 RAFT。

使用 scripts/build_enhancement.py，在独立匹配的 Torch/CUDA 构建环境中传 --depth-source、
--depth-license 和全新 --output。可选权重需 --weights-notice。
保留 NVIDIA Optical Flow 公开头文件许可，不分发 nvofapi64.dll（使用系统驱动）。

## English quick start

Extract the trusted complete add-on beside DLSS5Tool.exe, not inside mods. Keep the complete enhancement directory.
Select a flow backend, then Flow only. The last mode is restored after a startup environment check; failure turns it off and reports the reason. First use checks RAFT-Large before enabling it. RAFT-Large is the default (512 edge, six updates,
FP32, backward flow) and needs weights. NVOFA needs a supported NVIDIA GPU/driver, but no weights or iterations.
Initialization failure is reported before trying RAFT; runtime failure stops processing with no mid-video switch.
No automatic CPU fallback, downloads or installers. Base enhancement works with guidance off.
Launch the development app with `run.bat` at the repo root.

Current public editions omit depth weights and hide depth inference. Existing installed files are not deleted.
The component still includes Torch and architecture code with their licenses.
