# 深度／光流引导实验 · 2026-09-08

## 结论

本机实验支持给项目增加**可选的光流引导**，但尚不足以默认启用或宣称对所有视频都有固定幅度的提升。

- 同一段输入、相同参数下，legacy 与 v2 都响应实际运动矢量；项目原有“忽略 depth/flow”的注释至少对光流不成立。
- 二开自带 RAFT-Large 权重确实产生了有用的引导。本例中原算法和直接反向估计都使时序残差指标约减半。
- 原算法在这段小幅连续运动中略优，**没有证据说方向修正一定改善实际输出**。前向场取负并非一般意义上的反向场，但在局部缓慢运动中可形成有效近似，估计误差也会影响两者排序。
- 两种宿主中，测试深度图与零深度输出逐像素相同；深度＋精确光流与仅精确光流也逐像素相同。这只证明本次深度探针未产生影响，不证明所有输入、参数、运行时版本都忽略深度。
- 未修改生产引擎、GUI、默认设置、依赖清单、任何 DLL 或 AMD 工作区改动。

## 实验范围

- GPU：NVIDIA GeForce RTX 4070 SUPER，12GB；驱动 616.64。
- 正式输入：项目 `img/01.png`，缩放为 512×640，生成 24 帧（12fps，2秒）旋转、缩放和平移。
- 这是一张项目示例图构成的**合成镜头运动**，不是实际拍摄视频，也没有独立人物运动、遮挡变化或切镜。
- 每帧仿射矩阵已知，用 `前一帧矩阵 × 当前帧矩阵的逆` 得到当前坐标上的精确反向光流。
- 指标屏蔽画面边缘以及仿射变换产生的反射填充区域。
- 参数固定：preset=1、style=0、intensity/local_tone/local_struct=1、skin_struct=0.5、auto_mask=0、UI correction=0、motion_scale=(1,1)、depth convention=2。风格正对照仅将 style 改为1。
- 默认只在首帧 reset；另有逐帧 reset 的对照。
- 每组独立进程、同一套输入；不经过 GUI 输出混合、不从有损编码视频反读计算指标。
- 正式实验共22组（11种条件 × legacy/v2），每组24帧，共528帧输出。另有12帧先导实验及超时定位记录，不混入正式统计。

## 指标定义与结果

先计算每帧增强残差 `R_t = 输出_t - 输入_t`，再用**精确反向光流**把上一帧残差映射到当前帧，统计有效区域的 `mean(abs(R_t - warp(R_(t-1))))`。

单位为8位RGB灰度级，越低表示增强效果沿已知运动变化越小。这个指标不是人眼稳定性评分；输出退化为原图也能得到低分，因此不能只凭它判断画质。风格正对照也说明改变渲染外观会改变该指标。

| 条件 | legacy 时序残差 | v2 时序残差 | v2 相对零引导变化 |
| --- | ---: | ---: | ---: |
| 零引导 | 1.874606 | 1.874606 | 基线 |
| 精确反向光流 | 0.870147 | 0.871246 | 下降53.52% |
| 二开算法：`-RAFT(prev, current)` | 0.920284 | 0.924897 | 下降50.66% |
| 反向估计：`RAFT(current, prev)` | 0.945436 | 0.935968 | 下降50.07% |
| 故意反转精确光流方向 | 2.073988 | 2.039810 | 上升8.81% |
| 合成梯度深度 | 1.874606 | 1.874606 | 无变化 |
| 合成深度＋精确光流 | 0.870147 | 0.871246 | 与仅光流完全一致 |
| 每帧 reset、零引导 | 2.182787 | 2.182787 | 上升16.44% |
| 风格正对照 | 1.403186 | 1.403186 | 外观参数确实有效，不作同风格质量比较 |

补充验证：

- 两种宿主的零引导重复运行，全部24帧逐字节一致。
- 原有 zero-fast 路径与实际上传零纹理路径的输出一致。
- 测试深度与零深度、深度＋精确光流与仅精确光流的比较均为逐字节一致。
- 排除最初3个帧间比较后，v2零引导/精确光流/原RAFT/反向RAFT分别为1.794101 / 0.847961 / 0.896939 / 0.906468，结论仍成立。
- RAFT对有效区域精确光流的平均端点误差：原算法0.065685像素，反向估计0.075277像素。本例两者误差都小，原算法稍低。
- 图像检查确认输出与输入确实不同，保留了可见的神经渲染效果；没有进行盲测或系统性主观画质评分。

## RAFT与性能条件

- 使用二开自带的 `raft_large_C_T_SKHT_V2-ff5fadd5.pth`，没有下载或替换模型权重。
- torchvision RAFT-Large，6次更新，与二开一致；输入尺寸512×640（小于其720短边限制，不降采样）。
- 本轮RAFT运行于CPU float32、4线程，torch 2.8.0+cpu / torchvision 0.23.0+cpu。**不是二开的CUDA混合精度运行方式**。
- 两个方向、23对帧的模型调用合计约59.37秒；不含模型加载和部分预处理。不能用这次CPU结果推断GPU推理速度。
- v2纯DLSS调用：原有zero-fast约3.44ms/帧，关闭fast但上传零图约4.47ms/帧，精确光流约4.75ms/帧，RAFT反向缓存约5.11ms/帧。均排除首帧，不含光流估计、编解码、进程启动和模型加载；只是小分辨率单次试验，不代表端到端播放速度。
- 两种宿主在非零光流时并非所有像素完全一致，指标趋势一致；未对其数值差异进一步归因。

## 原生清理问题

先导实验观察到：v2完成全部帧后调用 `dlssnr_shutdown` 不返回。最初版本在shutdown之后才保存结果，因此超时没有留下帧数组；增加逐帧日志后确认渲染已经完成。

正式实验将结果先保存，再对v2子进程采用进程退出清理（操作系统回收资源），不调用该原生shutdown；legacy仍正常调用shutdown。每个结果目录的 `shutdown.json` 记录清理方式。这个处理只存在于实验脚本，**没有修复或改变生产生命周期**；正式集成前应单独定位该现象。

## 产物与复现

- [实验脚本](F:/project/DLSS5Tool/scripts/guidance_probe.py)
- [CPU测试](F:/project/DLSS5Tool/tests/test_guidance_probe.py)
- [四宫格对照视频](F:/project/DLSS5Tool/output/guidance-experiment-20260908/comparison.mp4)：左上输入，右上零引导，左下原RAFT，右下反向RAFT；2秒输入重复3遍，便于观察。视频仅供查看，指标取无损帧数组。
- [静态对照](F:/project/DLSS5Tool/output/guidance-experiment-20260908/comparison.png)
- [完整统计与逐帧哈希](F:/project/DLSS5Tool/output/guidance-experiment-20260908/summary.json)
- [RAFT运行信息](F:/project/DLSS5Tool/output/guidance-experiment-20260908/raft.json)
- 同目录 `inputs.npz` / `raft.npz` 保存输入和两种光流；每个宿主/条件目录保存 `frames.npy`、`result.json`、`ngx.log` 和 `shutdown.json`。

使用**新的输出目录**复现，脚本拒绝覆盖既有实验：

```powershell
.venv\Scripts\python.exe scripts\guidance_probe.py prepare --image img\01.png --output output\guidance-new --width 512 --height 640 --frames 24
.venv\Scripts\python.exe scripts\guidance_probe.py suite --output output\guidance-new --backends legacy v2 --cases zero_fast zero repeat flow_exact flow_wrong depth both reset_each style_control --timeout 45
```

独立RAFT测试环境在 `tmp/guidance-probe-env`。它安装了CPU版torch及依赖，未写入主项目依赖文件；本次通过临时追加主环境包路径复用OpenCV：

```powershell
tmp\guidance-probe-env\Scripts\python.exe -c "import sys,runpy; sys.path.append(r'F:\project\DLSS5Tool\.venv\Lib\site-packages'); sys.argv=['scripts/guidance_probe.py','raft','--output','output/guidance-new']; runpy.run_path('scripts/guidance_probe.py',run_name='__main__')"
.venv\Scripts\python.exe scripts\guidance_probe.py suite --output output\guidance-new --backends legacy v2 --cases raft_original raft_corrected --timeout 45
.venv\Scripts\python.exe scripts\guidance_probe.py summarize --output output\guidance-new
.venv\Scripts\python.exe scripts\guidance_probe.py preview --output output\guidance-new
.venv\Scripts\python.exe -m unittest tests.test_guidance_probe tests.test_dlss_host_process -v
```

最后一条命令本轮8项测试通过。对照MP4也已重新解码验证：72帧、1024×1344。

## 下一步建议

先增加默认关闭的实验性“光流时序引导”，深度模型暂不接入。正式开发需补：形状/有限值检查、启用引导时禁用zero-fast、共享内存或工作进程内估计、seek与切镜reset、异步帧对应和分段预热；第一版先限制非分块连续视频。

上线前应使用用户真实视频（人物转头、手部遮挡、快速横移、切镜、细纹理、压缩噪声），比较光流质量、拖影、画质、显存和端到端速度。本实验不覆盖HDR、4K、并行分段接缝、分块历史和CUDA RAFT性能。
