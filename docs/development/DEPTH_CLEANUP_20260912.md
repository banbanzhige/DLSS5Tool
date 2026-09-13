# 下线深度路径开销优化（2026-09-12）

## 改动与边界

- `guidance_worker.py`：深度未启用时不计算深度分析尺寸、不额外缩放深度输入，
  `depth_ms` 明确为 0；光流输入、RAFT 更新次数、归一化、输出与 reset 规则不变。
- 纯光流可以协商 `flow_only_v2`：worker 不分配/清零深度输出，pipe 不发送深度，
  client 不接收/扫描深度；共享内存从 RGBA8 + XY float32 + depth float32 的
  16 字节/像素降为 12 字节/像素。4K 稳态共享内存少 31.64 MiB（25%），
  pipe 输出数据少 1/3。此数字不是整个进程或显存的降幅。
- 新原生主机使用能力位 8，未启用深度时绑定初始化一次的 GPU 零深度纹理，
  不再每帧 `PrepareDepth` / `SubmitDepthImmediate` / `RecordUpload`。
  同步兼容、合并临时缓冲和异步槽位路径均覆盖；不把 DLSS 的深度纹理设为空。
- Python 主机对支持能力位 8 的新 DLL 允许缺失深度（传入 null，由 DLL 绑定零纹理）；
  老 DLL 保持非空零数组输入。光流可视化只消费运动向量，不依赖深度。
- 深度维护者开关及模式 2/3 保留原数据路径；并未删除模型、权重、依赖或历史备份。

## 协议兼容与生命周期

首次握手保留 protocol=1 和 shared_memory_v1。客户端增加输出格式请求，
worker 明确返回 `output_layout=flow_only_v2` 后，客户端才发送第二次紧凑映射配置。
worker 成功映射并确认 `shared_memory_flow_v2` 后，双方释放旧 v1 映射。
不支持协商的老 worker 忽略新增字段，客户端继续使用 v1/shared 或原有 pipe 回退；
老客户端不发送请求，新 worker 就仍输出完整 v1。

共享内存升级瞬间存在两块映射：16+12=28 字节/像素，4K 约 221.48 MiB，
随后只剩约 94.92 MiB 的紧凑映射。未把稳态内存减少误称为启动峰值减少。
失败、错误确认、worker 退出与超时均走关闭/解除映射流程。

`GuidanceSession.process()` 默认仍返回深度数组，保证外部 API 兼容；只有内部显式
`allow_missing_depth=True` 才返回 None。旧原生端需要的零数组以只读方式缓存一次，
不在每帧重新清零。明确要求独立副本的外部调用仍承担输出数组分配费用。

原生端为支持已有的在途模式切换，保留各槽位的深度资源，新增一张公共零深度纹理。
因此本次主要减少每帧 CPU/传输/上传，不宣称原生显存减少；首次初始化仍有一次清零上传。

## 验证

- 定向 Python 回归：245 项，244 通过、1 跳过。
- 复用 `tmp/guidance-cuda-env` 补跑模型缓存、深度分支、执行策略、参数和图片开关测试：
  28 项全部通过（包含基础环境中因无 Torch 而跳过的模型缓存测试）。
- NVOFA 与光流输入回归在同一 CUDA 环境中 10 项全部通过；未单独测 NVOFA 硬件吞吐。
- 原生 DLL 编译通过。RTX 4070 SUPER 上运行 9 组真实 Feature18 A/B：
  zero_fast、zero_upload、flow、flow_async、mixed、async、odd、transient、compatibility；
  每组 16 帧，全部逐帧哈希相同。覆盖模式 0/1/2/3 切换、空 motion/depth 输入、
  reset、非对齐尺寸、持久/临时缓冲以及三个在途槽位。
- 真实 RAFT：256×256 输入、128 分析边长、6 次更新、6 帧（含 reset）。
  full_v1/shared、flow_only_v2/shared、flow_only_v2/pipe 的运动向量逐字节相同，
  全部 `depth_model_calls=0`。
- 真实 RAFT → Feature18：旧 DLL/full_v1 与新 DLL/flow_only_v2 的 6 帧输出哈希相同。

原生上传探针的单轮纯光流总耗时为 0.2905→0.2841 秒，异步纯光流为
0.1720→0.1645 秒（各 16 帧、1440×1440，计时包含哈希，不包含初始化）。
样本短、非交替重复基准，其他用例也有正负波动，不能据此承诺稳定的导出提速百分比。
源码 RAFT 传输验证以正确性为主，首次 CUDA 编译/热身耗时不可用于加速比较。

一次端到端测试在旧 NGX 显式 shutdown 时超过 120 秒；探针改为与既有
`native_upload_probe.py` 一致的隔离进程退出回收方式，旧/新路径均完成。
应用退出逻辑未因此改动。

## 重建与生效范围

```powershell
cmd /c native\host_v2\build.bat tmp\depth-cleanup-20260912\native tmp\depth-cleanup-20260912\build
.venv/Scripts/python.exe -B -m unittest tests.test_guidance_transport tests.test_mods_guidance tests.test_still_flow_setting
.venv/Scripts/python.exe -B scripts/native_upload_probe.py --old runtime/dlssnr_host_v2.dll --new tmp/depth-cleanup-20260912/native/dlssnr_host_v2.dll --source tmp/Maxine-VFX-SDK/samples/input/input_0_100_frames.mp4 --output tmp/depth-cleanup-20260912/native-ab
tmp/guidance-cuda-env/Scripts/python.exe -B scripts/depth_cleanup_probe.py --old runtime/dlssnr_host_v2.dll --new tmp/depth-cleanup-20260912/native/dlssnr_host_v2.dll --output tmp/depth-cleanup-20260912/raft-ab
```

先按仓库卫生守则登记临时目录，并把 TEMP/TMP 指向该任务目录，设置
`PYTHONDONTWRITEBYTECODE=1`。探针输出目录必须尚不存在。

基线 DLL SHA256：`F6302D39F5505C2BC71F54147EB4F0756F9CFAED1D9BA7858D85EA7712CB9996`。
验证候选 DLL SHA256：`858A95E442830A830DAE2B47EE2FB349AB46561B55C689238504C94B71451A97`。

本次交付源码、测试和复现脚本，不发版、不覆盖 runtime/ 或 mods/ 中现有二进制。
全部优化在应用源码/构建、guidance_worker 与原生 v2 DLL 同步更新后生效；
仅搭配旧 worker 时会正确回退，但不能获得新的深度传输优化。

## 临时产物收尾

已清理 `tmp/depth-cleanup-20260912/` 中本任务生成的候选 DLL、OBJ/LIB、
临时日志和测试 JSON，共 711,865 字节文件内容；删除前后 F 盘可用空间增加
839,680 字节，本任务剩余临时占用为 0。复现脚本、验证结论和 DLL 校验值保留在仓库。
删除前核对了绝对路径、无链接/目录联接、无相关运行进程，仅存在文档重建引用。
所有产物可按上述命令重建，没有删除旧依赖、模型或备份。
