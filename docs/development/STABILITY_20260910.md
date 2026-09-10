# 稳定性修复与 RTX 4060 排查（2026-09-10）

本轮修改源码；没有重打包或替换用户安装目录，也没有更换原生运行库。

## 已修复

- 模型切换完成后，分析工作区显式刷新当前帧。请求仍由 generation / epoch / 参数哈希区分，过期结果不作为新配置画面。
- 已有 SDR 素材的分析工作区使用实际预览尺寸完成启用检查，并保留宿主和模型。首帧/图片先执行临时时序检查再 reset，展示仍为正确的零光流。无素材或非分析工作区保留原独立 preflight。
- 图片预览走后台队列；沿用大图分块配置和全分辨率缓存。后台失败停止缓冲，日志保留错误并显示简短提示。
- 状态文案固定单行，帮助支持鼠标悬浮和可聚焦按钮。画面固定角落显示切换/生成状态。
- 识别 `0xBAD00002` PlatformError，并分别识别 OutOfDate、OutOfGPUMemory、UnableToWriteToAppDataPath。

## 4060 反馈的证据与边界

用户截图的日志是 legacy 宿主诊断，640×360、preset=1；Init_with_ProjectID、Init_Ext 和 AllocateParameters 成功，CreateFeature(18) 返回 `0xBAD00002`，尚未执行图片处理。

NVIDIA SDK 将该码定义为底层平台错误，可能涉及图形 API、系统或 NGX 之外的依赖；不能据此断言显卡不支持或显存不足。
官方定义：https://github.com/NVIDIA/DLSS/blob/main/include/nvsdk_ngx_defs.h

本地保存的 `dist/v2.1.1-editions/DLSS5Tool-v2.1.1-win64-lite/_internal/nvngx_dlssnr.dll` 与项目默认 RTX 40 核心 SHA256 一致：
`CEB6432F6FBDF44D886014BCD47241932BF8B67439FEEF9BBDD0961436662650`。
这只能证明本地副本，不能代表用户下载/覆盖后的文件。

继续排查需要完整一键诊断报告中的实际 DLL 路径、版本/哈希、驱动版本以及 v2 和 legacy 分别的结果。轻量包基础增强不需要 RAFT/光流附加包。不要根据该错误自动换库或直接要求用户安装模型。

## 验证

- 回归：`python -m unittest tests.test_stability tests.test_gui_player tests.test_gui_guidance_tab tests.test_gui_module_reload tests.test_guidance_activation tests.test_diagnostics tests.test_i18n tests.test_mods_guidance tests.test_dlss_host_process tests.test_layout tests.test_package_editions`。
- 真实 GPU：`python scripts/stability_smoke.py --gpu --output output/stability-20260910`。合成素材、临时设置，不读取用户视频。
- 本机 RTX 4070 SUPER / 驱动 616.64：图片约 2.397 秒；光流激活约 7.604 秒；后续预览复用同一宿主；427 次 UI 定时回调，最大间隔约 93.9ms。这是单次冒烟，不是跨硬件性能承诺。
- UI 手工验证窗口：`python scripts/stability_smoke.py --show-ui --output output/stability-ui-20260910`，使用临时设置，关闭或三分钟后退出。

## 未覆盖

尚无反馈用户的 DLL/驱动环境，不能声称修复其 PlatformError。参数热更新、无素材时的跨进程模型保活、独立 DLL 切换小图验证/回滚入口仍需后续实现和测试；本轮不自动修改驱动、选择适配器或切换运行库。
