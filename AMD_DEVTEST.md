# AMD 开发验证包 · amd-dev1

这是给无法远程连接的测试者使用的独立验证包，不是正式 AMD 后端，也不会改动现有
DLSS5Tool 安装、设置或 NVIDIA DLL。主界面的 AMD 开关和正式处理接入仍待实机验证。

## 给测试朋友的操作说明

1. 完整解压 ZIP 到自己可写的短路径，例如 `D:\AMD-Test`。不要直接从压缩包运行，
   不要放进游戏目录或 `Program Files`。不需要安装 Python、Visual Studio 或 FSR SDK。
2. 从 [上游发布页](https://github.com/danielblnc/DLSS-NR-on-AMD/releases) 获取
   `dlssnr_on_amd_setup.exe`，放入 `amd_backend`。本包核验的安装器为 **v0.2.15**。
3. 将自己从合法来源取得且有权使用的 `nvngx_dlssnr.dll`（310.8.0.0）放到同一目录。
   本包不附带 NVIDIA DLL、模型权重或上游安装器，也不自动下载它们。
4. 打开 `AMD-DevTest.exe`，查看上游许可证并确认个人非商业测试，然后点击
   「运行官方安装器」。安装器是独立程序，请手动遵循其提示。已自行安装的也可直接使用。
   不要修改安全软件设置或使用管理员权限绕过拦截。安装器需生成 `version.dll` 和
   `dlssnr_on_amd_weights.bin`；只有 EXE 不足以开始推理测试。
5. 先点击「检测环境」确认选中 AMD 独显。保存工作、关闭游戏和其他 GPU 任务。
   保持默认共享纹理、内置测试图，不勾选 1080p，点击「开始 AMD 一轮测试」。
6. 测试完成后点击「导出上次回传包」，只把 `feedback-*.zip` 发给维护者。成功、失败、
   超时和取消都值得回传。不要发送 `amd_backend`、原始 DLL、权重或整个 results 目录。
7. 可选第二轮：用无隐私的人像/材质/游戏截图替换测试图。默认不把自选图生成的预览放进
   回传 ZIP；若愿意让维护者看效果，需要主动勾选「允许回传…对比预览」。原图不会回传。

如提示缺少 `amdhip64_7.dll` 或依赖：先回传日志，核对 AMD 驱动及官方 HIP 运行环境。
不要从随机 DLL 网站下载文件。上游已支持 RX 9060 XT；RDNA3/4 的具体效果需要实机验证。

## 失败时

- 子进程通常会被 120 秒超时或「取消」停止；不会自动循环重试。
- **GPU 测试存在驱动重置甚至整机卡死风险，进程隔离不能消除它。** 如曾发生卡死/重启，
  请再次打开测试器，仅点击「恢复测试设置」和「导出上次回传包」，不要立即再次压力测试。
- 若系统安全软件拦截，请停止并说明拦截现象；不要求排除文件、关闭防护或提权。
- 未知版本的安装器不会被测试器代为执行；可在自行核对官方来源后手动安装。

## 测试究竟验证什么

- 原生 `amd_probe.exe` 使用 AMD **公开** FidelityFX SDK v1.1.4 API 和官方签名运行库。
  它创建真实 D3D12 设备、隐藏交换链、FSR 3.1 NativeAA 上下文，进行正常的 FSR 调度。
- NR 案例显式加载用户安装的原版 `version.dll`，让它按自身正常机制捕获 FSR 调度。
  测试器不修改、解包或反汇编上游二进制，不调用推断出的私有函数。
- 每轮先验证 GPU 上传/回读逐字节一致，再运行完全不加载该模组的 FSR 基线，最后
  在新进程运行 NR。仅图像有变化不足以证明 NR：还要求完成全部帧、与 FSR 基线有差异、
  数值有限、帧标记对应，以及本轮模组日志里有网络任务证据且没有已知错误。
- 输入是 SDR sRGB RGBA16F，640×360、24 帧；每帧都有红绿标记，包含运动和一次镜头切换。
  运动矢量/深度为零，FSR 在首帧和切换点重置。**模组是否遵循重置未获公开 API 保证。**
- 可选 1080p/6 帧只在前一档观察到增强后运行。这个测试不等价于实际长视频导出。
- 对比图左：FSR 基线；中：加载 AMD 模组；右：相对 FSR 的差异×10。不是 NVIDIA NGX
  画质基准；有差异也可能是伪影。`ENHANCEMENT_OBSERVED` 仍需人工观察，不叫 PASS。
- 同步路径、隐藏 Present 和 1.5 秒启动等待是实验条件，不能证明模组已就绪；如果上游
  必须可见窗口、其他模块加载顺序或特定 FSR 版本，本轮会留下失败阶段供下一轮调整。
- 不测 HDR、超大图片、视频编码、NVENC/AMF、VSR、全部参数映射或长期时序稳定性。

## 回传内容与隐私

回传 ZIP 严格白名单只包含：报告、限定大小的本次子进程日志、脱敏模组日志和允许回传
的对比预览。不扫描磁盘，不采集主机名/序列号/账号/网络地址，不包含内存或崩溃转储。
本地 results 会留下输入和输出原始帧便于复核；不应整个打包上传。

报告按阶段刷新，子进程启动前已经写入初始报告。若程序被强制退出，重新打开后可以
重新打包最近一轮中途日志。测试前的 INI 以 `.before-devtest` 保存，正常结束原样恢复。

## 构建与开发

依赖：Visual Studio C++ x64 Build Tools、项目 Python 环境、PyInstaller、NumPy、Pillow，
以及 `third_party/FidelityFX-1.1.4` 下的 SDK v1.1.4 头文件和官方运行库。

```powershell
cmd /c native_amd_probe\build.bat
.venv\Scripts\python.exe -m unittest tests.test_amd_devtest -v
.venv\Scripts\python.exe amd_devtest.py --inventory --root tmp\amd-devtest-local
# NVIDIA 机器仅验证原生/FSR 基线，不加载 AMD 模组：
.venv\Scripts\python.exe amd_devtest.py --baseline-only --adapter 0 --root tmp\amd-devtest-baseline
powershell -ExecutionPolicy Bypass -File build_amd_devtest.ps1
```

SDK 来源固定为 https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/tree/v1.1.4 。
上游个人非商业许可见 https://github.com/danielblnc/DLSS-NR-on-AMD/blob/master/LICENSE 。
用户自行提供组件不等于项目取得修改、再分发或商业集成授权。本包只供指定朋友的个人
开发测试；不上传到公开 Release，不视作正式支持。后续正式集成仍需确认使用范围。
