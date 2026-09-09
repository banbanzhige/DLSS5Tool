# 引导优化抽帧画面对照 · 2026-09-08

## 结果

> 历史结果说明：后续在实拍素材连续播放中观察到 RAFT 精简导致的严重时序抖动，
> 该优化已从生产路径撤回。本文仅证明当时有限窗口中的编码前 RGB 像素一致，
> 也说明静态抽帧不足以验收时序稳定性，不能作为继续启用 RAFT 精简的依据。

用同一份原片输入，分别重新运行原始FP32、当前SDPA＋深度FP16、
SDPA＋RAFT精简、SDPA＋双Stream、三者组合，以及FP32重复运行。
每组33帧，共198次DLSS渲染；六组相同源帧的最终RGB输出逐像素一致，
最大8位像素差为0。FP32重复运行也一致。

本轮补充的是实际原生DLSS输出验证，不再只比较光流／深度数组。
原生宿主为已部署的上传缓冲区优化版；RAFT精简／双Stream仅在测试子进程绑定，
没有加入生产组件，也没有更改用户设置。

## 如何看图

- [六帧总览](F:/project/DLSS5Tool/output/guidance-visual-20260908/overview.png)：每组左为FP32基线，右为组合方案。
- [帧80细节](F:/project/DLSS5Tool/output/guidance-visual-20260908/details-frame080.png)：四列依次为当前SDPA、RAFT精简、双Stream、组合；
  第一行缩小展示，后三行是眼部、刘海、手指／发卷的1:1原像素裁剪，不锐化、不降噪。
  应在图片查看器以100%显示，应用缩放可能改变屏幕上的实际比例。
- [差异×32](F:/project/DLSS5Tool/output/guidance-visual-20260908/difference-x32.png)：固定倍率绝对RGB差，不自动拉伸。黑色区域表示像素相同。
- [源视频帧80](F:/project/DLSS5Tool/output/guidance-visual-20260908/source-080.png)、
  [FP32完整帧80](F:/project/DLSS5Tool/output/guidance-visual-20260908/fp32/frame-080.png)、
  [组合完整帧80](F:/project/DLSS5Tool/output/guidance-visual-20260908/combined/frame-080.png)。

所有保存的对照图都来自编码前的真实渲染输出，使用无损PNG；没有从有损MP4反读比较。
总览缩放只为排版，完整1440×1440 PNG保留在各方案目录。

## 输入和限制

- 原片为此前的 `9月1日.mp4`，1440×1440。
- 连续窗口：源帧0–10、77–87、154–164；每个窗口只在起始帧reset，窗口内部连续推理。
- 展示帧：3、10、80、87、157、164（0起始帧号，与界面编号一致）。
- 没有把每个展示帧单独reset渲染，以免抹掉时间历史。
- 深度Large、引导长边720、光流FP32、6次迭代；固定style=0、intensity/local_tone/local_struct=1、skin_struct=0.5、auto_mask=0。
  参数为测试统一值，并非改写用户的保存参数。
- 不同模型精度下深度图仍可能不同；最终画面一致不意味着深度模型数值无损。
- 人工目检六帧总览及帧80的眼部、刘海、手指／发卷裁剪：未见新增模糊、轮廓变化或边缘差异。
  数值一致性比这次肉眼观察更强，但只覆盖这些帧和参数。
- 尚不能用静态抽帧判断全片播放的闪烁、拖影，亦未覆盖其他视频的快速切镜、强遮挡或大运动。
  之前BF16先导异常没有因这次结果而被撤销；本轮未测试BF16。

复现脚本：`scripts/guidance_visual_compare.py`。依赖此前保存的原片窗口数组，输出必须是新目录：

```powershell
tmp\guidance-cuda-env\Scripts\python.exe scripts\guidance_visual_compare.py --output output\guidance-visual-new
```

完整逐帧输出／光流／深度SHA-256及差异指标在各方案的 `result.json`；
[汇总](F:/project/DLSS5Tool/output/guidance-visual-20260908/summary.json)。
