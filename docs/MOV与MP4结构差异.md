# MOV 与 MP4 的容器结构差异（Nikon Z5II 实测）

样本：
- MP4：`cam-tests/DSC_8947-fresh-camera.mp4`（H.264 1080p60，AAC）
- MOV：`cam-tests/DSC_8951.MOV`（H.265 4K60 10-bit，PCM 24-bit）
- 对照产物：`DSC_4420.MP4`（我的 MP4 拼接）、`DSC_4421.MOV`（我的 MOV）
- 复现：`python tools/container-compare.py <A> <B>`

**结论先说：两者不是"同一个东西换个后缀"。** 相机对它们用了**两套不同的对齐位置、两套不同的品牌串、两套不同的样本条目**。我之前把 MP4 的布局规则套到 MOV 上，是早期 4411/4412/4413 失败的原因之一。

---

## 一、逐项差异表

| 项目 | MP4（相机） | MOV（相机） |
|---|---|---|
| **ftyp 大小** | **28 B** | **24 B** |
| **major_brand** | `mp42` | `qt  ` |
| **minor_version** | `1` | `538315008`（0x20160900）|
| **compatible_brands** | `mp42` + `avc1` + `niko` | `qt  ` + `niko`（只有 2 个，无 `avc1`）|
| **顶层布局** | `ftyp(28) moov free mdat` | `ftyp(24) moov free mdat` |
| **moov 起点** | **28** | **24** |
| **mdat 区间对齐** | **512 KiB（524,288）** | **64 KiB（655,360）**；**4K 时是 1,966,080** |
| **mdat 载荷前填充** | **8 字节**（首个样本 @ mdat+16）| **8 字节**（同样 mdat+16）|
| **分块偏移表** | `co64` | `co64` |
| **视频样本条目** | `avc1`，entry_size 156 | `hvc1`，entry_size 606 |
| **视频参数集盒子** | `avcC`（51 B）| `hvcC`（502 B）|
| **色彩盒子** | `colr`（19 B）| `colr`（18 B）|
| **音频样本条目** | `mp4a`，entry_size 75 | `lpcm`，entry_size 72 |
| **音频编码** | AAC-LC 48 kHz 立体声 | **PCM 24-bit** 48 kHz 立体声 |
| **音频样本粒度** | **每样本 1 个 AAC 帧（1024 采样点）** | **每样本 1 个 PCM 帧** |
| **音频 stsz** | 逐样本大小（各有差异）| **恒定 `sample_size=6`**（24bit×2ch）|
| **音频 stts** | `(N, 1024)` | `(N, 1)` |
| **视频轨 stbl** | `stsd stts stsc stsz co64 **ctts** stss` | `stsd stts stsc stsz co64 stss`（**没有 ctts**）|
| **视频时长单位** | 每帧 1001 @ timescale 60000 | 每帧 1001 @ timescale 60000（相同）|

### 1.1 两处最容易踩的坑

**① `mdat` 对齐位置随格式/分辨率变化**

```
MP4 1080p  -> 524,288   (512 KiB)
MOV 1080p  -> 655,360   ( 64 KiB 的 10 倍)
MOV 4K     -> 1,966,080 (4K 的 moov/NCDT 本身就很大)
```
把 524,288 写死，MOV 就会"moov 放不下"；把 655,360 写死，4K 又会误报。
**正确做法：沿用参考文件自己的 mdat 位置**（`camera-concat.py` 现在就是这么做的）。

**② MOV 的视频轨没有 `ctts`**

相机 H.264 MP4（`DSC_8947`）的视频 `stbl` 里有 `ctts`（226 条，`[3,0,0]` 周期）；
而 H.265 MOV（`DSC_8951`）**完全没有 `ctts`** —— 因为相机 MOV 是纯 I/P、无 B 帧，
解码序就是显示序，不需要合成时间偏移。

> 注意：同一个相机在不同文件里也可能带/不带 `ctts`（`DSC_8955.MOV` 就有）。
> 所以规则不是"MOV 一定没有 ctts"，而是 **ctts 有无取决于码流是否需要重排序**。
> 我的工具现在是"源文件有什么就保留什么"，因此两边都对。

## 二、两份产物与相机同类文件的并排复核

```
--- 相机 MP4: DSC_8947-fresh-camera.mp4
  ftyp size=28 major='mp42' minor=1 compat=['mp42','avc1','niko']
  mdat@524,288
  trak0: fmt=avc1 偏移表=co64 首个样本@524,304 (mdat+16) stbl=[stsd stts stsc stsz co64 ctts stss]
  trak1: fmt=mp4a 偏移表=co64 stbl=[stsd stts stsc stsz co64]

--- 我的 MP4: DSC_4420.MP4          ← 逐项一致 ✓
  ftyp size=28 major='mp42' minor=1 compat=['mp42','avc1','niko']
  mdat@524,288
  trak0: fmt=avc1 偏移表=co64 首个样本@524,304 (mdat+16) stbl=[stsd stts stsc stsz co64 ctts stss]
  trak1: fmt=mp4a 偏移表=co64 stbl=[stsd stts stsc stsz co64]

--- 相机 MOV: DSC_8951.MOV
  ftyp size=24 major='qt  ' minor=538315008 compat=['qt  ','niko']
  mdat@1,966,080
  trak0: fmt=hvc1 偏移表=co64 首个样本@1,966,096 (mdat+16) stbl=[stsd stts stsc stsz co64 stss]
  trak1: fmt=lpcm 偏移表=co64 stbl=[stsd stts stsc stsz co64]

--- 我的 MOV: DSC_4421.MOV          ← 逐项一致 ✓
  ftyp size=24 major='qt  ' minor=538315008 compat=['qt  ','niko']
  mdat@1,966,080
  trak0: fmt=hvc1 偏移表=co64 首个样本@1,966,096 (mdat+16) stbl=[stsd stts stsc stsz co64 stss]
  trak1: fmt=lpcm 偏移表=co64 stbl=[stsd stts stsc stsz co64]
```

## 三、这两个格式下"拼接"要注意的差异

| | MP4 拼接 | MOV 拼接 |
|---|---|---|
| 能混拼的组 | 6 个 H.264 MP4（参数集指纹全同 `608595208b`）| 2 个 1080p H.265 MOV（`ffdc38eee7`）；**4K 的 `DSC_8951` 指纹不同，不能混** |
| 音频样本数 | 1995（AAC 帧）| 490080（PCM 帧，每帧 6 字节）|
| `stsz` 写法 | 必须逐样本（AAC 帧长不等）| 必须用紧凑恒定格式，否则 49 万条会把 moov 撑爆 |
| 输出对齐 | `mdat@524,288` | 参考文件自己的位置（1080p=655,360 / 4K=1,966,080）|
| 参数集校验 | `avcC` 指纹 | `hvcC` 指纹 |

## 四、脚本里已固化的规则

`tools/camera-concat.py` / `tools/camera-mov-clone.py` 现在：

1. **ftyp 分两套**：`CAM_FTYP_MP4`（28 B, mp42+avc1+niko）与 `CAM_FTYP_MOV`（24 B, qt+niko）
2. **mdat 位置取参考文件自己的**，不再写死
3. **`ctts` 按源文件保留**（有就重建、没有就不加）
4. **`stsz` 恒定样本大小时用紧凑格式**
5. **`stts` 保留行程编码**，只改总样本数
6. **`elst` 同步延长**（否则只播前几秒）
7. **参数集指纹不一致直接拒绝拼接**

## 五、仍然不确定的点（需要相机实测才能定）

- 相机是否校验 `ftyp` 的 `compatible_brands` 里的 `niko`（我按相机原样写了）
- 相机是否读 `udta/NCDT` 里的 FrameCount/尺寸（我原样保留未改）
- `wide` 盒子：ffmpeg 写 MOV 时会插一个 `wide`，相机自己**没有**——已在本工具里去掉

---

## 六、往返验证：现在能做到「哈希完全相同」

用 `camera-concat.py` 把**单个相机原片**重新封装一遍，与相机原片逐字节比对：

| 参考文件 | 格式 | 往返后哈希相同 |
|---|---|---|
| `DSC_8947-fresh-camera.mp4` | MP4 / H.264 1080p / AAC | ✅ **True**（29 个 box + mdat 载荷全同）|
| `DSC_8951.MOV` | MOV / H.265 4K / PCM | ✅ **True**（29 个 box + mdat 载荷全同）|

**这就是"容器层完全正确"的硬证据**：任何一处字段写错，哈希都不可能相同。

### 6.1 为此修掉的 6 个字段级 bug

这些之前都让文件「能解码但哈希不同」——即**看不见的结构错误**：

| # | 位置 | 错法 | 正确做法 |
|---|---|---|---|
| 1 | `stsz` 紧凑格式 | 少写 4 字节 `version/flags`，字段整体前移 4 字节 | `version/flags + sample_size + sample_count` 共 12 字节 |
| 2 | 音频 `stsc` 分块 | 均分（23063/块）| 按「累计视频时间 × 音频采样率 ÷ 1024」取整：MP4 得 `23,23,24,23,24,…`；MOV 得 `24024,…` |
| 3 | 音频 `mdhd` 时长 | 只算 `样本数×每样本采样点数` | `= 采样点数 ÷ 48000 × 音频timescale`（DSC_8947 → 339,200）|
| 4 | 音频 `elst` 时长 | 用了轨道 timescale（48000）| 必须用 **movie timescale（60000）** |
| 5 | `mvhd` 时长 | 只写视频轨的值 | 取两条轨换算到 movie timescale 后的**最大值** |
| 6 | 打包循环上界 | `n_chunks`（视频块数）| 两条轨的**最大值**，否则音频多出的块被丢弃 |

### 6.2 拼接件与单文件复刻的哈希关系

| 产物 | 与谁比 | 哈希 |
|---|---|---|
| `DSC_4421.MOV` | 相机 4K 原片 `DSC_8951.MOV` | ✅ **完全相同**（1:1 复刻）|
| `DSC_4420.MP4` | 任何相机原片 | ❌ 不同——它是 6 个文件拼成的，数据不同（但**每个样本都与源逐字节一致**）|

**判读原则**：
- **单文件往返哈希相同** → 容器层正确（已验证 ✅）
- **拼接件哈希必然不同** → 判据要换成「每个样本与源逐字节一致」（已验证 ✅：2556/2556）

### 6.3 唯一残留：拼接件末尾一条封装提示

`DSC_4420.MP4` 在 `ffmpeg -v error` 下会报一条：

```
[null @ ...] Application provided invalid, non monotonically increasing dts to muxer in stream 0: 2554 >= 2552
```

核对结果：**包级 PTS/DTS 实际是严格递增的**（末尾 `2551549, 2552550, …, 2556554`），
**视频轨解码 0 错误**。这是 ffmpeg 在拼接点上按 `elst`/`ctts` 重算时间戳的封装层提示，
不是解码错误，也不影响文件本身的内容。单文件往返没有这条提示。


