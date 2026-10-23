# 本地界面细节打磨（2026-10-23）

## 调整

- 设置 → 输出与编码：HDR 高精度开关移至解码缓存下方，与 CPU 解码兜底开关连续排列、左侧对齐。
- 画面效果：使用现有的折叠标题与长标题换行样式，分为画面调整、输出合成、单次 NR 残差细调、全局残差合成、区域保护。
- 默认只展开画面调整；扩展调节范围归入该组，输出预览归入输出合成，保护区编辑及计数归入保护组。
- 各组独立折叠，展开状态保存到 `ui_appearance_open`。收起保持参数、开关及撤销记录；无效输入提示位于组外。

## 首轮验证（区域保护独立拆组之前）

复用现有 `.venv`，仅执行本次 UI 与参数关联的 42 项检查，全部通过：

```powershell
.venv/Scripts/python.exe -B -m unittest -q tests.test_parameter_unification tests.test_parameter_range.LocalRangeTests tests.test_gui_player.SettingsPanelPersistenceTests tests.test_i18n tests.test_gui_player.WidgetSmokeTests.test_app_and_timeline_construct tests.test_workbench_ui.WorkbenchUiTests.test_both_themes_and_sizes_keep_controls_inside_workbench
```

原有中英文最窄侧栏布局检查改为先展开分组，再检查控件几何；跨容器位置比较使用屏幕坐标。

另以隔离设置和空队列运行源码 UI，未载入媒体，检查以下交互：

1. 每组开合后参数与撤销快照不变，未触发模型或合成回调。
2. 收起含无效输入的分组后保留原始草稿，错误提示仍显示。
3. 浅色、深色全部展开时，数值框与组标题显示正常。
4. 两个设置开关处于同一容器的相邻行，横向位置一致。
5. 重建应用后，展开状态与参数仍与保存内容一致；损坏的展开配置回退至默认。

结果见 [verification.json](local-ui-polish-20261023/verification.json)。首轮四组布局在同一 DPI 下，默认画面效果内容高度为 1058 px，全部展开为 2369 px，减少约 55%。首轮截图使用应用自身窗口句柄捕获，并人工检查布局：

- [画面效果（浅色）](local-ui-polish-20261023/appearance-light.png)
- [画面效果（深色）](local-ui-polish-20261023/appearance-dark.png)
- [设置（浅色）](local-ui-polish-20261023/settings-light.png)

## 区域保护补充

- 依据后续反馈，将区域保护从全局残差合成拆为独立折叠组，默认收起并独立保存展开状态。旧展开配置缺少新键时使用该默认值。
- 组内依次为启用保护区勾选框、羽化、保护区域编辑和数量。保留原有参数标识、预设、区域坐标和合成逻辑；取消勾选仍保留区域及羽化设置。
- `ParameterRow` 增加显式勾选框选项，保护区使用同一套 `CheckToggle`；其他布尔参数的既有呈现保持原逻辑。

29 项关联检查通过：

```powershell
.venv/Scripts/python.exe -B -m unittest -q tests.test_parameter_unification.LocalAdjustmentTests tests.test_parameter_range.RealtimeRangeTests.test_ui_correction_checkbox_aligns_left_and_retains_values_and_undo tests.test_realtime_inspector.RealtimeInspectorTests.test_all_parameters_kept_and_control_types_match_metadata tests.test_gui_player.SettingsPanelPersistenceTests tests.test_i18n tests.test_gui_player.WidgetSmokeTests.test_app_and_timeline_construct
```

另在中英文隔离实例中验证旧配置保留、勾选切换仅更新合成、区域和羽化记忆、撤销及复位、折叠保留、重启持久化和浅深两色几何；回调错误均为零。结果见 [中文](local-ui-polish-20261023/verification-zh_CN.json)、[英文](local-ui-polish-20261023/verification-en_US.json)。当前区域保护预览：

- [浅色](local-ui-polish-20261023/region-protection-light.png)
- [深色](local-ui-polish-20261023/region-protection-dark.png)

## 临时空间

本次任务目录为 `tmp/ui-polish-20261023/`。开始前 F: 可用 44.98 GiB，新增峰值预算小于 20 MiB；复用现有依赖。已将必要截图与结果保留在本维护文档旁，任务目录中的验证脚本和隔离状态为可重建的一次性材料。

已核对精确目录、祖先路径及文件清单，无重解析点，无运行代码引用；本次验证进程已返回成功且未启动后台子进程。全局 CIM 进程列表查询因环境权限不可用。清理本任务的 335555 字节临时文件后，F: 可用空间实际增加 352256 字节（约 344 KiB）；本任务临时占用剩余 0 字节。原始记录见 [cleanup.json](local-ui-polish-20261023/cleanup.json)。截图和结果约 318 KiB 作为本次 UI 维护证据保留，无剩余临时目录待复核。

区域保护补充验证使用 `tmp/region-protection-ui-20261023/`，开始前 F: 可用 44.98 GiB、峰值预算小于 10 MiB。验证命令均成功退出后，核对路径、重解析点、引用、文件清单及截图副本哈希，再清理该目录；剩余临时占用为 0 字节。实际释放空间及保留证据见 [region-protection-cleanup.json](local-ui-polish-20261023/region-protection-cleanup.json)。
