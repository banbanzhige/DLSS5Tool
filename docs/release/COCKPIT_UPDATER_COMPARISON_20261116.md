# Cockpit Tools 更新体验核对

日期：2026-11-16。范围：公开源码和发行元数据只读分析，未下载/运行安装器，未复制实现代码。
源码固定为提交 `22eb4df0b05f6395559b41a648da39e566e9e66b`，公开正式版元数据为 v1.4.0。

## 实际机制

- 使用 Tauri 2 及 `tauri-plugin-updater`。配置包含内置验签公钥、按平台的 latest JSON 入口和通用入口。
  [固定提交配置](https://github.com/jlcodes99/cockpit-tools/blob/22eb4df0b05f6395559b41a648da39e566e9e66b/src-tauri/tauri.conf.json)
- 线上 Windows NSIS 清单指向完整 `Cockpit.Tools_1.4.0_x64-setup.exe`；API 附件大小为
  38,017,185 字节，清单含 signature。该通道是完整安装器更新，没有按源版本列出的差分包。
  [正式清单](https://github.com/jlcodes99/cockpit-tools/releases/download/v1.4.0/latest-windows-x86_64-nsis.json)、[Release](https://github.com/jlcodes99/cockpit-tools/releases/tag/v1.4.0)
- 清单生成器按目标平台选择安装器及签名，不含逐文件旧版 before 清单。
  [生成器](https://github.com/jlcodes99/cockpit-tools/blob/22eb4df0b05f6395559b41a648da39e566e9e66b/scripts/release/build_target_latest_json.cjs)
- Tauri 官方文档确认 Windows 更新可直接复用 NSIS/MSI 完整安装器，并要求校验更新签名。
  [官方说明](https://v2.tauri.app/plugin/updater/)

因此，本次观察到的 Windows 更新路径不依赖某个指定旧版本的文件差分基线。
这不能证明全部历史安装形态都无条件兼容，也不能把更新流畅等同于用了块级增量。

## 交互和容错

界面区分可更新、下载中、安装中及就绪状态，提供取消下载、稍后、跳过版本、重试及手动下载入口。
[更新弹窗](https://github.com/jlcodes99/cockpit-tools/blob/22eb4df0b05f6395559b41a648da39e566e9e66b/src/components/UpdateNotification.tsx)

开启自动更新后，代码支持后台下载，待用户触发重启时再安装；默认自动安装设置为关闭。
安装入口先处理其自身服务收尾，再调用插件安装和重启。
[流程](https://github.com/jlcodes99/cockpit-tools/blob/22eb4df0b05f6395559b41a648da39e566e9e66b/src/App.tsx)、[默认设置](https://github.com/jlcodes99/cockpit-tools/blob/22eb4df0b05f6395559b41a648da39e566e9e66b/src-tauri/src/modules/update_checker.rs)

网络类失败按退避重试；签名/校验失败、权限及空间问题不盲目重试。
提示策略尊重本地提醒开关，并避免同版本反复自动弹窗。
[重试](https://github.com/jlcodes99/cockpit-tools/blob/22eb4df0b05f6395559b41a648da39e566e9e66b/src/utils/updaterRetry.ts)、[提醒策略](https://github.com/jlcodes99/cockpit-tools/blob/22eb4df0b05f6395559b41a648da39e566e9e66b/src/utils/updatePromptPolicy.ts)

上述结论来自代码和资产核对；未执行其 GUI，也未证明下载支持跨进程断点续传。

## 对 DLSS5Tool 的建议

优先落实“后台检查/可选下载 → 下载就绪 → 用户选择退出安装或重启更新”的完整体验，
保留精确增量快速通道，增加无需旧版基线的标准安装通道，避免将普通用户送到网页手工解压。

我们的 full 有数 GB 的依赖和模型，应拆分应用核心、稳定依赖、推理组件和模型，
只更新目标需要变化的单元；完整应用包始终作为跨版本兜底。用户自定义 DLL/模型保持独立。
性能和下载收益必须用实际发行包测量，不根据 Cockpit 的约 38 MB 包体推算我们的更新大小。

框架选择：Tauri updater 与其宿主集成，不能作为 Python 应用的直接替换依赖。
若采用成熟安装更新框架，可先验证 Velopack 的 Python/PyInstaller 接入及便携布局、模型保留、
full 兼容、首次迁移和旧更新器兼容性；通过验证后再决定是否替换现有引擎。
[Velopack Python 接入](https://docs.velopack.io/getting-started/python)

本次仅调整设计优先级，不迁移 UI、不更换实际更新框架、不修改现有发行资产。
