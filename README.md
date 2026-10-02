# video-to-camera-mov

把**普通电脑视频**转成**相机能直接播放的 MOV**。

首个验证机型：**Nikon Z5II**（1080p59.94 / H.265 / PCM24）。
方法本身与机型相关，但思路可迁移到其它消费级相机。

---

## ⚠️ 先读这一段

- **性能问题很大**：i5-12450H 上，**4 分钟的视频需要转换约 30 分钟**。
  预计过几天优化好并发布打包程序。
- 本项目是 **vibe coding 的产物**。
- **本项目内容由 AI 编写。**
- **不要用 GPU / 硬件编码器**，视频编码交给 **x265**（原因见下）。

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
    --preset fast
```

### 校验（强烈建议，必须 31/31）

```powershell
python tools/verify-output.py DSC_0001.MOV --template D:\card\DCIM\100NZ5_2\DSC_0001.MOV
```

校验项覆盖：容器布局、轨道结构、**时长自洽**、`NCDT` 帧数与时间、
缩略图（含**解码验证**）、**参数集与切片头 33 个字段**、整体解码。

> 参数集检查会 **trace 到 P 帧**。只检查首帧（IDR）会漏掉大部分差异——
> 本项目就因此放行过一个相机不认的文件。

### 拷卡

```powershell
python tools/copy-to-card.py DSC_0001.MOV      # 自动 SHA256 校验、编号冲突检测
python tools/copy-to-card.py --list
```

---

## 常用参数

- `--template`：相机原片模板，**必填**
- `--preset fast`：x265 预设。`fast` 比 `medium` 约快 2.6 倍
- `--bitrate 40M`：码率。源是 720p 上采样时 15M 就够
- `--time now`：拍摄时间。默认当前时间；
  也可给 `"YYYY:MM:DD HH:MM:SS"`（本地时间）或 `keep`
- `--duration 30` / `--start 10`：只取一段
- `--no-thumbs`：不重建缩略图
- `--size 1920x1080` / `--fps 60000/1001`

---

## 工具清单

主链：

- `tools/video-to-camera-mov.py` —— 端到端转换
- `tools/verify-output.py` —— 校验门禁
- `tools/make-camera-mov.py` —— 只做打包（码流 + PCM → MOV）
- `tools/hevc-ps.py` —— 位级改写 HEVC 参数集
- `tools/camera-jpeg.py` —— 相机风格 JPEG 编码
- `tools/boxio.py` —— MOV 盒子解析
- `tools/binpath.py` —— ffmpeg / ffprobe 定位

辅链与诊断：

- `tools/camera-concat.py` —— 拼接相机原片（不重编码）
- `tools/copy-to-card.py` / `tools/clear-card.py` —— 拷卡 / 清卡
- `tools/make-variants.py` —— 批量生成参数集对齐程度不同的候选
- `tools/psdiff.py` —— 逐字段对比两个 MOV（含 P 帧切片头）
- `tools/ncdt-diff.py` / `tools/parse-nctg.py` —— `NCDT` 解析与对比
- `tools/box-tree.py` / `tools/container-compare.py` / `tools/boxdiff.py`
- `tools/sample-diff.py` / `tools/sample-anatomy.py` / `tools/chunk-order.py`
- `tools/hvcc-diff.py` / `tools/trace-diff.py` / `tools/sps-prefix.py` / `tools/sps-sweep.py`
- `tools/check-candidates.py`

---

## 已知局限

1. **必须重编码**，无法无损转封装（参数集须与相机一致）
2. **无法使用硬件编码器**：实测 NVENC 与相机差 16 个字段，
   其中 14 个改不了（CTU、最小编码块、TU 深度、SAO、AMP、PPS 开关、HRD）
3. **依赖相机原片作为模板**
4. **仅验证 1080p59.94**；4K 与其他帧率未实测
5. **存在若干未对齐项**（显式量化表、SPS 内参考图像集、一个 PPS 开关），
   经验证不影响播放，属已知差异而非已解决
6. **结论为逆向推导**，非厂商文档，**固件更新后可能失效**

---

## 许可

代码采用 **MIT**。见 [LICENSE](LICENSE)。

**依赖说明**：运行时用到的 **x265 是 GPL-2.0**。
仅通过子进程调用时不影响本项目许可；但若以打包形式**内置并分发** x265，
该分发物需按 GPL-2.0 处理。
