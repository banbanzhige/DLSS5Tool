# v2.2.0 · 文件级增量更新与 DLSS 渲染 GPU 选择

## 新增

- v2 宿主不再使用系统的未指定默认适配器：自动模式按高性能顺序选择可用的 NVIDIA D3D12 GPU，即使显示器由核显输出，也不会因此把 DLSS 创建在核显上。
- 「设置 → 高级宿主」新增「DLSS 渲染 GPU」。多张 NVIDIA 显卡可手动指定；选择按物理 GPU 与 PCI 位置持久化，切换时安全重建隔离会话，所选设备不可用时明确报错且不静默改用核显。
- 一键诊断和导出日志记录实际 DLSS 适配器、DXGI LUID、CUDA 序号、PCI 总线、显存与 D3D12 特性级别，便于区分显示输出 GPU 和真正的渲染 GPU。
- 引入版本对文件级更新包，只下载新增或变化的文件，复用未变化的运行依赖和模型。
- 自动匹配轻量版或完整安装；轻量版叠加附加包按完整安装处理。没有匹配差异包时提供整包升级入口，完整安装不再默认下载轻量包。
- 独立 `DLSS5Update.exe` 更新助手：下载和退出安装分别确认；程序退出后校验、备份和替换，失败时尝试回滚，中断时提供恢复入口。
- 保护设置、队列和未受管的自定义文件。官方受管文件被修改、组件残缺、路径冲突或使用自定义外置组件目录时，停止自动更新并提示手动升级。

## 首次升级

**v2.1.x 等旧程序需手动完整安装一次 v2.2.0。** 旧程序没有新助手，不能直接使用文件更新包。关闭旧版，将 v2.2.0 完整解压到新目录：需要推理组件的用户选择完整版，或轻量版叠加同版本附加包；不要只替换 EXE。

v2.2.0 是新更新器的首次正式基线，之后仍需发布者提供对应旧版本到新版本的差异附件才能增量升级。本次不提供虚假的 v2.1.x 自动增量包，不自动上传或发布。

完整包包含主程序、更新助手、已验证推理组件及原始 RAFT-Large 权重，不附深度权重。轻量包含主程序和更新助手；附加包仅包含 mods，不含主程序或更新助手。保留现有 Torch/CUDA 依赖，不降精度或裁剪功能。

## 保留限制

- 继承 v2.1.3 的 HDR 光流和大图支持；仍不支持静态 HDR 图片、视频时序分块及单边超过 16384 的处理。
- 光流预览暂请使用不超过 1280 的分析档位；长 HDR 视频跳帧分析可能等待较久，取消需等当前读取结束；HDR＋超分建议 GPU 队列为 1。
- 文件更新匹配明确的版本对；没有对应更新包时使用完整包。备份不会静默删除，下次更新前询问；回滚未完成时保留恢复数据。
- 本机验证不等于干净机器、跨显卡认证或第三方分发许可审核通过。

## English

The optimized v2 host now selects an explicit NVIDIA D3D12 adapter instead of the unspecified system default, so an integrated GPU can remain connected to the display without receiving the DLSS device. A new **DLSS render GPU** selector supports automatic high-performance selection and explicit multi-NVIDIA-GPU selection. Changes rebuild the isolated session safely, and diagnostics report the adapter actually used.

File-level updates transfer changed/new files only, selecting Lite or Full according to installed components. A standalone helper verifies, backs up and replaces files after the app exits, with rollback and interrupted-update recovery. Modified managed files or custom component paths require manual upgrade.

Existing v2.1.x users must first manually extract v2.2.0 into a new directory. This release is the first updater-enabled baseline; future releases must publish matching version-pair payloads. No upload is performed by the local packaging process. Existing HDR/large-image limitations remain unchanged.
