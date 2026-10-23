# 队列输出目录交互修正（2026-10-23）

## 行为

- 「新任务输出」位于添加按钮上方，与添加操作归为同一区域；使用单行下拉控件，默认显示「源文件所在目录」，可选择自定义文件夹。只影响之后加入的任务。
- 通过右键菜单「修改所选任务输出目录…」支持多选和全选；面板不显示独立按钮。混选时跳过已完成任务；运行中不可修改。
- 改目录只更新任务记录，保留原文件名、画面参数、导出参数及任务状态；不会移动或生成媒体文件。与磁盘文件、其他任务或本批任务重名时自动编号；选择原目录不会额外编号。
- 完整输入、输出路径通过任务行和所选任务详情的悬浮提示展示；面板不显示独立「实际输出」栏。改目录后悬浮提示同步更新。
- 未选中任务时隐藏选择数量与详情，并收起整块区域的留白；单选或多选后恢复显示，移除后的撤销提示独立按需展示。
- 「应用参数」保留任务现有目录和文件名；容器变化时只调整扩展名并处理重名。
- 原有队列 JSON 格式保持兼容，改后的路径会保存，并供恢复、重试和实际导出调度使用。
- 小窗口优先保留详情和底部操作按钮，由任务列表缩短并滚动。

使用说明已更新到 `docs/USER_GUIDE.md`。此次修改仅涉及源码，未构建或发布安装包。

## 定向验证

环境：已有 Miniconda Python 3.13.9、pytest 9.0.2、Tk 8.6，没有复制环境或模型。

初次实现时，两个独立进程分别通过 **30 项 + 35 项，共 65 项**，无失败、错误或跳过：

1. `tests/test_queue_output.py`、`tests/test_export_queue.py`、`tests/test_export_gui.py`，以及本地化的目录键和 GUI 引用校验。
2. `tests/test_queue_list.py`：保留原选择、快捷键、右键、撤销和列宽覆盖，并验证新增菜单项及忙碌状态。

新增目录用例覆盖参数/文件名/状态保留、混选已完成项、磁盘和队列重名、取消、原目录不变、忙碌和模态对话框后的复查、默认目录只影响新任务、应用参数保留目录、保存恢复及图片/视频导出调度。

初次界面验证使用真实 Tk 控件，覆盖中文 320×620、英文 420×620 队列面板；当时包含只读路径栏及其文字选择验证，后按维护者要求移除该栏，见后续精简复核。导出使用写入少量固定字节的替身核对最终路径，没有运行 GPU 或实际视频编码。

本机合并运行两个界面测试模块时，连续创建/销毁 Tk 解释器曾出现 `init.tcl` / `auto.tcl` 初始化错误；最终按模块分进程运行，未修改产品运行依赖。未运行全量回归。

复现时先按仓库卫生守则重建并登记任务目录，再在单个 PowerShell 会话中设置：

```powershell
$taskRoot = 'F:\project\DLSS5Tool\tmp\queue-output-directory-20261023'
$env:TEMP = $env:TMP = $taskRoot
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:DLSS5TOOL_SETTINGS_PATH = Join-Path $taskRoot 'isolated-settings.json'
$env:DLSS5TOOL_QUEUE_PATH = Join-Path $taskRoot 'isolated-queue.json'
$env:TCL_LIBRARY = 'C:\Users\PC\miniconda3\Library\lib\tcl8.6'
$env:TK_LIBRARY = 'C:\Users\PC\miniconda3\Library\lib\tk8.6'
python -B -m pytest tests/test_queue_output.py tests/test_export_queue.py tests/test_export_gui.py tests/test_i18n.py::LocalizationTests::test_language_catalogs_have_identical_nonempty_keys tests/test_i18n.py::LocalizationTests::test_gui_translation_keys_exist_in_both_catalogs --basetemp (Join-Path $taskRoot 'output-tests') -p no:cacheprovider -q
python -B -m pytest tests/test_queue_list.py --basetemp (Join-Path $taskRoot 'list-tests') -p no:cacheprovider -q
```

## 隐藏面板按钮的复核

同日按维护者要求移除独立改目录按钮及其状态更新，开始队列区域上移，右键菜单继续保留此功能。中英文默认目录提示和使用说明均明确引导用户通过右键操作。

此处只调整入口与布局，定向复核 **5 项全部通过**：`test_queue_panel_shows_actual_output_and_context_menu_change` 的中文/英文两项、两项本地化校验，以及 `test_menu_enablement_and_busy_dispatch`。两组界面测试仍分进程运行。验证了面板没有独立改目录按钮、右键操作能修改任务路径、混选和忙碌状态正确。

## 移除独立输出路径栏的复核

同日按维护者要求继续移除「实际输出」只读栏、对应状态更新及两条不再使用的本地化文案。完整路径沿用现有悬浮提示展示，右键改目录入口保留；使用说明已同步。

定向复核 **5 项全部通过**：`test_queue_panel_uses_path_tooltips_and_context_menu` 的中文/英文两项、两项本地化校验，以及 `test_actual_menu_and_tooltip_cleanup`。确认面板不显示路径栏和改目录按钮、开始按钮可见、右键改目录后详情悬浮提示更新；真实任务行悬浮提示包含完整输入及输出路径，并能正常关闭。两个界面测试模块仍分进程执行，没有扩大回归范围。

## 导入区一体化落地

维护者确认「导入区一体化」方案后，将输出目录移到添加按钮上方，并把标签缩短为「新任务输出」。标签与下拉控件在同一行；移除独立空输入框和浏览按钮，导入区与任务工具栏之间使用自然留白，工具栏直接接任务列表并统一左侧对齐。根据后续界面反馈取消了分隔线。

下拉菜单提供「源文件所在目录」「选择文件夹…」以及当前自定义路径。选择文件夹使用原生目录对话框；取消时恢复原显示和值，运行中不可更改。完整路径与适用范围通过悬浮提示展示。

界面显示与持久化路径分开处理：`queue_output_dir` 继续使用空字符串表示跟随源文件夹，自定义路径保持原格式，兼容已有设置。显示文案不会写入路径设置。切换默认目录、取消或恢复默认均不会改动已排队任务；已有任务仍由右键菜单修改输出目录。

两个进程定向验证 **28 + 2 = 30 项通过**，无失败、错误或跳过：

- `tests/test_queue_output.py`、`tests/test_export_queue.py` 及两项本地化键校验：包括既有目录处理、重名和调度覆盖；真实 Tk 交互覆盖中文/英文、浅色/深色、320/360/420×620 面板、长路径旧设置恢复、选择自定义目录、取消、恢复默认、忙碌禁用及重新启用。确认选择器位于添加按钮上方，标签和左右边缘对齐。
- `tests/test_queue_list.py::test_menu_enablement_and_busy_dispatch`、`tests/test_queue_list.py::test_actual_menu_and_tooltip_cleanup`：验证右键入口、忙碌状态和完整路径提示继续工作。

此次未运行全量回归，未实际编码视频或打包；使用说明和中英文文案同步更新。

## 空闲时收起选择信息

同日按维护者截图反馈，未选中任务时不再展示「已选择 0 项」及选择任务提示，整块详情区域自动收起，空间交还任务列表，底部操作按钮保持原位置。选中任务后恢复选择数量和详情，多选继续显示汇总。清除选择时同步关闭已打开的详情悬浮提示，避免残留窗口。

撤销通知与选择信息分别控制：移除全部任务、没有选中项时仍能显示并操作撤销；恢复任务后重新选中恢复项、展示详情，撤销通知收起。

两个独立 Python 进程定向验证 **4 + 2 = 6 项通过**，无失败、错误或跳过：

- `tests/test_queue_output.py::test_queue_panel_integrates_output_picker_and_context_menu`：中文/英文、深色/浅色和 320/360/420×620 面板，验证初始无选择、单选、多选、取消选择、留白收起、底部按钮位置稳定、详情提示关闭、移除全部与按钮撤销，同时保留默认目录选择器及右键修改目录的覆盖。
- `tests/test_queue_list.py::test_keyboard_remove_undo_and_input_focus_isolation`、`tests/test_queue_list.py::test_actual_menu_and_tooltip_cleanup`：验证移除/撤销快捷键、输入焦点隔离、实际菜单与任务行路径提示。

未运行全量回归，没有构建或打包；使用说明已同步。

## 临时空间收尾

任务目录为 `F:\project\DLSS5Tool\tmp\queue-output-directory-20261023`，原计划新增峰值低于 10 MiB，开始前 F 盘可用约 93 GiB。没有构建、下载或复制大文件。

两次测试进程均以 0 退出；清理前确认目录归属、绝对路径边界、祖先及子项无重解析点、源码无运行依赖，所有测试文件可独占打开。仅使用原生 PowerShell `Remove-Item -LiteralPath` 删除本任务目录。

已清理 28 个文件，逻辑体积 61,027 字节；删除前后磁盘可用空间分别为 99,827,597,312 / 99,827,703,808 字节，观测释放 **106,496 字节**。本任务剩余临时占用为 **0**。

按钮精简复核另登记 `F:\project\DLSS5Tool\tmp\queue-output-menu-only-20261023`，预计峰值低于 1 MiB，开始前 F 盘可用 99,827,691,520 字节。两次测试均以 0 退出；确认边界、归属、无重解析点、文件可独占打开后，按相同清理规则删除本次 3 个测试文件（逻辑体积 7,000 字节）。磁盘可用空间从 99,827,679,232 增至 99,827,691,520 字节，观测释放 **12,288 字节**，剩余临时占用 **0**。

路径栏精简复核登记 `F:\project\DLSS5Tool\tmp\queue-path-tooltip-only-20261023`，预计峰值低于 1 MiB，F 盘开始前可用约 93 GiB。两次测试均以 0 退出；按相同边界、归属、重解析点及独占打开检查后，删除本次 3 个文件（逻辑体积 6,937 字节）。磁盘可用空间从 99,827,679,232 增至 99,827,691,520 字节，观测释放 **12,288 字节**，剩余临时占用 **0**。

导入区一体化验证登记 `F:\project\DLSS5Tool\tmp\queue-import-output-picker-20261023`，预计峰值低于 2 MiB，F 盘开始前可用约 93 GiB。两次测试均以 0 退出；按相同边界、归属、无重解析点及独占打开检查后，仅删除本次 16 个文件（逻辑体积 28,413 字节）。磁盘可用空间从 99,827,642,368 增至 99,827,695,616 字节，观测释放 **53,248 字节**，剩余临时占用 **0**。

空闲详情收起验证登记 `F:\project\DLSS5Tool\tmp\queue-idle-details-20261023`，预计峰值低于 2 MiB，开始前 F 盘可用 99,827,687,424 字节。两个测试进程均以 0 退出；确认无源码运行引用、精确路径与归属、祖先和子项无重解析点、文件可独占打开后，仅删除本次 7 个文件（逻辑体积 13,225 字节）。删除前后磁盘可用空间为 99,827,658,752 / 99,827,683,328 字节，观测释放 **24,576 字节**；剩余临时占用 **0**。
