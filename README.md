# video-to-camera-mov

把**普通电脑视频**转成**相机能直接播放的 MOV**。

首个验证机型：**Nikon Z5II**（1080p59.94 / H.265 / PCM24）。
方法本身与机型相关，但思路可迁移到其它消费级相机。

---

## ⚠️ 先读这一段

- **性能问题很大**：i5-12450H 上，**4 分钟的视频需要转换约 20 分钟**（编码占 97%+）。
  目前只能靠"降 `--preset` + 关掉别的程序"缓解。
- 本项目是 **vibe coding 的产物**。
- **本项目内容由 AI 编写。**
- **不要用 GPU / 硬件编码器**，视频编码交给 **x265**（原因见下）。

---

## 下载（测试版 v0.1.0-beta.1）

Windows 10/11 x64，**不需要装 Python 或 ffmpeg**（ffmpeg/x265 已打进包里）：

- [video-to-camera-mov-v0.1.0-beta.1-win64.zip](../../releases/download/v0.1.0-beta.1/video-to-camera-mov-v0.1.0-beta.1-win64.zip)
  —— **推荐**：解压得到「相机视频转换器」文件夹（约 400 MB），启动快、好排错
- [video-to-camera-mov-v0.1.0-beta.1-onefile.exe](../../releases/download/v0.1.0-beta.1/video-to-camera-mov-v0.1.0-beta.1-onefile.exe)
  —— 只有一个文件，第一次启动要解压临时目录，慢一些

发布页：<https://github.com/waiw18/video-to-camera-mov/releases/tag/v0.1.0-beta.1>
测试指引：[docs/测试指引-v0.1.0-beta.1.md](docs/测试指引-v0.1.0-beta.1.md)（包内的 `测试指引.txt` 是同一份）

> **8 Mbps 档不可用**：1280×720 源 + 8 Mbps 能转出来、43 项门禁也全过，
> 但相机里提示"无法显示此文件"。码率请用 **15 Mbps 或以上**；界面里已经没有 8M 档了。

图形界面里是三步：**① 开始转换 → ② 校验门禁（必须 43/43）→ ③ 拷到卡上**，
转换中显示阶段、进度、编码速度和预计剩余时间。请按测试指引走一遍，
回传结果的方式也写在里面。包内还有一个 `自检.cmd`：把一段短视频拖到它上面，
就会自动跑一遍内置自检并把结果写成报告（不需要人盯着点界面）。

---

## 它能解决什么

相机不接受"随便一个合法的 H.265 MP4/MOV"。实测有**三层判据**，
任何一层不符就播不了，而且失败表现不同：

- 提示 **"无法显示此文件"** → 相机私有元数据层（`NCDT`）有问题
- 能识别、进列表，但播放**一直加载** → 容器层或码流层有问题

要区分后两者：把**相机原生码流**装进自建容器后试播。能播则容器层无问题。

三层的原理、字段对照、失败模式，见 **[docs/principles.md](docs/principles.md)**。
面向普通读者的版本见 **[docs/blog-post.md](docs/blog-post.md)**。

---

## 快速开始

### 依赖

- **Python 3**（仓库里的脚本）
- **Pillow**（重建缩略图用）
- **ffmpeg / ffprobe**（需含 `libx265`）
- 一段**相机自己录的同分辨率 MOV**当作模板

ffmpeg 的定位顺序：环境变量 `CAMMOV_FFMPEG` / `CAMMOV_FFPROBE` → `PATH` → 常见安装位置。
找不到时可直接指定：

```powershell
$env:CAMMOV_FFMPEG  = "D:\ffmpeg\bin\ffmpeg.exe"
$env:CAMMOV_FFPROBE = "D:\ffmpeg\bin\ffprobe.exe"
```

### 转换

```powershell
python tools/video-to-camera-mov.py 你的视频.mp4 `
    --out DSC_0001.MOV `
    --template D:\card\DCIM\100NZ5_2\DSC_0001.MOV `
    --preset fast --bitrate 15M
```

**默认值就是真机验证通过的配方，不要额外加参数。** 尤其：

| 不要加 | 原因 |
|---|---|
| `--fps source`（走 29.97）| 外部生成的 29.97 至今**没成功过**，见「已知局限」 |
| `--duration`（截短）| 短文件快进越界时相机不夹紧，见下 |
| `--jobs 2` 以上（分段并行）| 相机播到 20~35 秒会跳出 |

### 校验（强烈建议，必须 43/43）

```powershell
python tools/verify-output.py DSC_0001.MOV --template D:\card\DCIM\100NZ5_2\DSC_0001.MOV
```

校验项覆盖：容器布局、轨道结构、**时长自洽**、**分块尺寸（视频/音频都必须 0.5 秒）**、
`NCDT` 帧数与时间、缩略图（含**解码验证**）、**参数集与切片头逐字段**、整体解码。

> 参数集检查会 **trace 到 P 帧**。只检查首帧（IDR）会漏掉大部分差异——
> 本项目就因此放行过一个相机不认的文件。

### 拷卡

```powershell
python tools/copy-to-card.py DSC_0001.MOV      # 自动 SHA256 校验、编号冲突检测
python tools/copy-to-card.py --list
```

---

## ★ 三条硬约束（每条都有真机反例）

这三条是踩坑最多的地方，**改动前务必读完**：

| 项 | 必须 | 违反后的现象 |
|---|---|---|
| **`tkhd.duration`** | **保持模板原值**（示例模板是 `228228` / `228220`），**绝对不要改成"正确值"** | 快进超过文件末尾时相机**不夹紧 → 卡死** |
| **PTL tier** | **59.94 → `0`（Main）** | 改成 `1` → 相机直接判无效 |
| **分块** | 每 **0.5 秒**一块（59.94 = 30 帧；音频 24,024 样本）| 按"每 30 帧"算 → 29.97 全错，播到一半跳出 |

**关于 `tkhd.duration`**：它看起来"过期"（和真实时长不符），但相机把它当**可播放长度**
来夹紧快进目标。决定性证据：一个能播的文件与一个改过该字段的文件**全文件只差 6 个字节**
（两条轨道的 `tkhd.duration`），行为随之翻转。`verify-output.py` 已把它列为门禁项。

**关于分块**：相机的规律是**每 0.5 秒**，不是"每 30 帧"。
59.94 恰好 30 帧 = 0.5 秒，所以这个坑藏了很久；30fps 该是 15 帧一块。

---

## 常用参数

- `--template`：相机原片模板，**必填**
- `--preset fast`：x265 预设。`fast` 比 `medium` 约快 2.6 倍
- `--bitrate 15M`：码率。源是 720p 上采样时 15M 就够；
  **别低于 15M**（8M 实测相机不认，见「已知局限」）
- `--time now`：拍摄时间。默认当前时间；
  也可给 `"YYYY:MM:DD HH:MM:SS"`（本地时间）或 `keep`
- `--duration 30` / `--start 10`：只取一段（注意上面的短文件限制）
- `--no-thumbs`：不重建缩略图
- `--thumb-at 12.5`：缩略图取第 12.5 秒的画面（默认 `0` = 首帧；
  有 `--start` 时是相对截取起点的偏移）
- `--thumb-image 封面.jpg`：直接拿一张图片当缩略图（会按 3 种尺寸缩放后编成相机规格 JPEG）
- `--size 1920x1080` / `--fps 60000/1001`

---

## 图形界面与打包

- `app/main.py` —— tkinter 图形界面：普通模式（真机验证过的配方）/ 高级模式（每项可调），
  底部有进度、编码速度、预计剩余时间与日志；三步走 **① 转换 → ② 校验门禁 → ③ 拷到卡上**。
  另外带两个不需要人盯的自检入口（`--windowed` 打包后没有控制台，结果写进报告文件）：
  - `--selftest --report <文件>`：只把界面起起来，写一份控件自检报告后退出
  - `--auto <源视频> --outdir <目录> --outname DSC_0001.MOV --report <文件>`：整套转换 + 门禁跑一遍
- `app/build-exe.ps1` —— 用 PyInstaller 打包成 `dist\相机视频转换器\`（onedir）与单文件 exe
- 打包需要：装了 **PyInstaller + Pillow** 的 Python（可用 `CAMMOV_PYTHON` 指定）、
  `ffmpeg`/`ffprobe`、以及**你自己相机录的同规格模板** `templates\DSC_8955.MOV`
  （媒体文件不进仓库，必须自备）
- `design/前端设计稿-v1.html` —— 界面设计稿（双击可看，带三种状态的静态预览）

---

## 工具清单

主链：

- `tools/video-to-camera-mov.py` —— 端到端转换
- `tools/verify-output.py` —— 校验门禁（43 项）
- `tools/make-camera-mov.py` —— 只做打包（码流 + PCM → MOV）
- `tools/hevc-ps.py` —— 位级改写 HEVC 参数集
- `tools/camera-jpeg.py` —— 相机风格 JPEG 编码
- `tools/boxio.py` —— MOV 盒子解析
- `tools/binpath.py` —— ffmpeg / ffprobe 定位

无损编辑与诊断：

- `tools/cut-mov.py` —— **无损截取**（码流原样搬运，只重建容器）
- `tools/camera-concat.py` —— 拼接相机原片（不重编码）
- `tools/copy-to-card.py` / `tools/clear-card.py` —— 拷卡 / 清卡
- `tools/make-variants.py` —— 批量生成参数集对齐程度不同的候选
- `tools/psdiff.py` —— 逐字段对比两个 MOV（含 P 帧切片头）
- `tools/ncdt-diff.py` / `tools/parse-nctg.py` —— `NCDT` 解析与对比
- `tools/byte-dump.py` / `tools/make-byte-doc.py` —— **逐字节展开成 Markdown**
- `tools/make-ref-doc.py` —— 从基准文件生成参数档案
- `tools/timecmp.py` / `tools/tail-diff.py` / `tools/chunk-tile.py` / `tools/sampletable.py`
- `tools/seek-test.py` / `tools/es-analyze.py` / `tools/patch-tkhd.py` / `tools/box-types.py`
- `tools/camera-gop-census.py` / `tools/frame-types.py` / `tools/gop-structure.py`
- `tools/camera-files-info.py` —— 一次列出多个相机文件的概要
- `tools/box-tree.py` / `tools/container-compare.py` / `tools/boxdiff.py`
- `tools/sample-diff.py` / `tools/sample-anatomy.py` / `tools/chunk-order.py`
- `tools/hvcc-diff.py` / `tools/trace-diff.py` / `tools/sps-prefix.py` / `tools/sps-sweep.py`
- `tools/check-candidates.py`

---

## 文档

- **[docs/principles.md](docs/principles.md)** —— 原理详解（三层判据、字段对照、失败模式）
- **[docs/blog-post.md](docs/blog-post.md)** —— 面向普通读者的专栏版
- **[docs/MOV转换指南.md](docs/MOV转换指南.md)** —— 使用指南
- **[docs/生成DSC_4521的参数.md](docs/生成DSC_4521的参数.md)** —— 基准文件完整参数档案
- **[docs/可用文件逐字节展开.md](docs/可用文件逐字节展开.md)** —— 可用文件逐字节展开
- **[docs/码流字段对齐-实测.md](docs/码流字段对齐-实测.md)** —— 参数集字段实测
- **[docs/相机GOP结构-实测更正.md](docs/相机GOP结构-实测更正.md)** —— GOP 结构实测
- **[docs/MOV与MP4结构差异.md](docs/MOV与MP4结构差异.md)** —— 容器格式差异

---

## 已知局限

1. **必须重编码**，无法无损转封装（参数集须与相机一致）
2. **无法使用硬件编码器**：实测 NVENC 与相机差 16 个字段，
   其中 14 个改不了（CTU、最小编码块、TU 深度、SAO、AMP、PPS 开关、HRD）
3. **依赖相机原片作为模板**
4. **仅验证 1080p59.94**；4K 未实测
5. **29.97（1080p30）暂未打通**：相机自己的 30fps 原片能播，
   但外部生成的试过 `tier=0` 和 `tier=1` 都播不了
6. **短文件（约 1 分钟）快进越界时相机不夹紧**；全长文件正常，
   只影响短片段的使用体验
7. **存在若干未对齐项**（显式量化表、SPS 内参考图像集、一个 PPS 开关），
   经验证不影响播放，属已知差异而非已解决
8. **结论为逆向推导**，非厂商文档，**固件更新后可能失效**
9. **码率低于 15 Mbps 会被相机拒播**：8 Mbps 档能转出来、43 项门禁也能过，
   但相机里提示"无法显示此文件"；图形界面已去掉 8M 档，命令行也请用 `--bitrate 15M` 以上

---

## 许可

代码采用 **MIT**。见 [LICENSE](LICENSE)。

**依赖说明**：运行时用到的 **x265 是 GPL-2.0**。
仅通过子进程调用时不影响本项目许可；但若以打包形式**内置并分发** x265，
该分发物需按 GPL-2.0 处理。
