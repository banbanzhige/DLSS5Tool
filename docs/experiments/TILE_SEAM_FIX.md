# 大图接缝修复：第一阶段

## 实现范围

- `native/host_v2/tile_blend.h`：独立实现轴向分块规划、上下文裁切、平滑余弦权重、归一化及滚动行缓存。没有复制第三方节点代码，也没有新增 Torch/ComfyUI 依赖。
- 原生 Feature 18 仍使用完整输入纹理和子区域推理；输入始终不变，每块独立 reset。每次 Evaluate 后仅回读当前子区域，在下一块覆盖之前融合。RGBA8 和 RGBA16F 均保留原输出类型。
- 默认上下文每侧 128、融合重叠 128 像素；小块按比例缩小。末块向内移动保持固定尺寸，融合按实际重叠区计算，四角权重归一化。
- 累加仅保留一条 tile-height 高的 FP32 横向条带及一个普通内存块。仍占用完整 GPU 输入／输出纹理，不是无限尺寸或全流程 VSR 分块。
- VSR 在共享内存分配前检查目标单边 16384 上限，导出入口也预检。支持范围内的大尺寸仍可能因显存不足或模型约束失败。
- 不兼容的引导配置在宿主进程创建前校验；未放开 HDR／分块引导，也未静默关闭用户设置。

## 实图验证（RTX 4070 SUPER）

两张用户原图只读，所有输出在 `output/`。测试使用引导关闭、默认风格、强度／本地色调／本地结构为 1。

- 11637×5120 原尺寸：分块成功，连续两次输出逐像素一致。
- 4608×4608 → 9216×9216：成功；之前木门／沙发区域的横向明暗断层消失，连续两次输出逐像素一致。
- 下表度量为边界两侧“DLSS 输出减去 VSR 输入”的差值变化之平均绝对值，单位 8-bit 码值。它不是完整感知质量评分，结合局部图和整图检查使用。

| 边界 | 修复前 | 修复后 |
|---|---:|---:|
| x=6000 | 4.595 | 0.599 |
| y=3000 | 7.776 | 0.530 |
| y=6000 | 5.632 | 0.448 |
| y=9000 | 4.234 | 0.555 |

- 扫描所有行列的残差跳变，而非只检查旧网格；修复后最大横向跳变不再集中于分块边界。图像最外沿仍有模型边缘伪影，未将其声称为本次修复范围。
- 640×640 普通非分块图：新旧 DLL 输出逐像素一致。
- 138 项相关 Python 单测及原生分块几何／融合测试通过。
- 合成 scRGB RGBA16F、强制 384 分块、非持久缓冲：处理成功、输出全为有限值、重复一致；这不是实拍 PQ／HLG HDR 画质验收。

## 复现命令

```powershell
cmd /c native\host_v2\build.bat output\tile-fix-build
cmd /c tests\build_tile_blend.bat
.venv\Scripts\python.exe -m unittest tests.test_super_resolution tests.test_hdr_pipeline tests.test_mods_guidance tests.test_i18n tests.test_dlss_host_process tests.test_gui_player -q
.venv\Scripts\python.exe scripts\tile_seam_probe.py --source "原图路径" --dll output\tile-fix-build\dlssnr_host_v2.dll --output output\新的测试目录 --scale 2 --repeat
```

测试工具在独立进程退出时回收 NGX；部分运行库显式 Shutdown 会长时间占用 CPU，生产应用已有可终止的宿主子进程隔离。该既有行为不作为融合耗时。

## 后续范围

HDR 分析副本和整帧一致引导、超过纹理上限的 VSR／DLSS 全流程流式分块、视频时序分块，均未在第一阶段开放。不能仅删除当前格式校验来宣称支持。

## 本地部署状态

新 DLL 已编译到 `output/tile-fix-build/dlssnr_host_v2.dll`。旧 DLL 备份为同目录下的 `dlssnr_host_v2.before.dll`。
用户关闭应用后，已替换项目根目录的 `dlssnr_host_v2.dll`，并核对新文件 SHA256 与验证构建一致：`7046B5402EA28EDFB7F4D1F771BCFA5EDF20B1EC21FFC0A6D81F2104B8FD3931`。重新启动项目版本即可加载；本次没有重新打包或替换 `dist` 内的文件。
