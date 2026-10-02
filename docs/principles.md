# 普通视频 → Nikon Z5II 可播 MOV：原理详解

本文只讲**原理**：相机为什么认/不认一个文件，每一层的判据是什么，以及为什么必须那样做。
所有数字都来自本项目实测（对照文件：相机原片 `DSC_8955.MOV`，1080p59.94 H.265）。

> 结论先放这里：**相机不是在"兼容播放"，它是在要求"和它自己录的文件同构"。**
> 任何一个层面（容器 / 私有元数据 / 码流参数集）与它的编码器产出不一致到某个程度，
> 它就拒绝。而且三层的失败表现不同，可以据此定位是哪一层出问题。

---

## 〇、三层判据总览

一个 MOV 文件要被 Z5II 认，必须同时过三关：

| 层 | 内容 | 判据严格程度 | 失败表现 |
|---|---|---|---|
| **1. 容器** | box 树、`ftyp`、`mdat` 位置、样本表、交错顺序 | 结构必须自洽且与相机布局同构 | 能识别但"读取无限加载" |
| **2. 私有元数据** | `moov/udta/NCDT` | **必须有**，且内部 JPEG 必须是相机格式 | 缺了 → **"无法显示此文件"** |
| **3. 码流** | HEVC 参数集 + 切片头（编码器配置） | 关键字段必须与相机编码器一致 | 能识别但"读取无限加载" |

**诊断口诀**：
- 报"**无法显示此文件**" → 第 2 层（`NCDT` 缺失或结构坏）
- 能进列表、能识别、但播放**一直转圈** → 第 1 层或第 3 层
- 想区分第 1 层和第 3 层：把**相机自己的码流**装进你的容器里试播。能播 → 容器没问题，问题在码流。

---

## 一、容器层原理

### 1.1 为什么 `mdat` 的位置必须精确

相机文件的顶层布局是固定的：

```
ftyp(24)  moov  free  mdat@655,360
```

1080p 的 `mdat` 必须落在 **655,360**（= 0xA0000，640 KiB），4K 是 **1,966,080**。

**原理**：相机的读取器按固定偏移找媒体数据（这是嵌入式设备的典型做法——省掉一次全文件扫描）。
`moov` 比预留空间小的时候，用 `free` 盒**填充到精确的 655,360**，一行不能多、一行不能少。

所以容器装配的最后一步是断言：

```
assert len(out) == 655360        # ftyp + moov + free
```

### 1.2 `ftyp` 的品牌

```
1080p MOV: "qt  " + "niko"，minor_version = 538,315,008
```

`niko` 是 Nikon 的私有品牌。相机据此把文件归入自己的播放列表。

### 1.3 样本表：把"码流"和"容器"对上

MOV 用一组表描述"第 n 个样本在文件的哪个字节、多大、显示多久"：

| 盒 | 作用 | 相机特征 |
|---|---|---|
| `stsd` | 编码格式描述 | 视频 `hvc1`（内含 `colr` + `hvcC`）；音频 `lpcm` |
| `stts` | 每个样本的时长 | 视频 `(N, 1001)`，时基 60000 → 59.94 fps；音频 `(N, 1)`，时基 48000 |
| `stsc` | 每个 chunk 有几个样本 | 视频 **每 chunk 30 个样本**（约 0.5 秒） |
| `stsz` | 每个样本的字节数 | 全等时可用紧凑格式 `sample_size + count` |
| `co64` | 每个 chunk 的文件偏移 | 64 位 |
| `stss` | 哪些样本是关键帧 | 每 30 帧一个 |

### 1.4 交错（interleave）——最容易错的一层

**原理**：相机播放时要顺序读文件，所以音视频 chunk 必须按时间交错排列，
而不是"先放完所有视频再放所有音频"。

相机的规律：

```
video chunk k  = 第 30k .. 30k+29 个视频样本
audio chunk k  = 覆盖到 video chunk k 结束时刻为止的所有音频样本
                 起点 = floor(累计视频 tick / 60000 × 48000)   (PCM 48 kHz)
                      （AAC 的话再 ÷ 1024）
排列：v0, a0, v1, a1, v2, a2, ...
```

**注意**：音频 chunk 长度**不是均匀的**（实测 `23,23,24,23,24,…`），
因为它由视频时间轴换算而来。按固定长度切会得到"音频与画面对不上"。

### 1.5 `elst` 的时基陷阱

`edts/elst` 的 `segment_duration` 用的是 **movie timescale（`mvhd.timescale`）**，
**不是** track timescale。相机两者恰好都是 60000，所以这个坑很容易埋着不炸——
一旦模板换成 movie timescale 不同的文件，就会"视频只播开头几秒"。

### 1.6 `mvhd` 时长必须覆盖最长轨道

`mvhd.duration` 是"影片总时长"，必须 **≥ 每一轨的时长**。

实测踩过：B 站这类 DASH 源，**视频轨 254.699 s、音频轨 254.862 s**（音频更长）。
如果 `mvhd` 写成视频时长，音频尾部就超出行程被切掉。
正确做法：

```
mvhd.duration = max(video_duration, audio_duration)     # 换算到 movie timescale
```

同时，**抽音频也要按视频轨时长去截**，否则音视频长度天然不一致。

---

## 二、私有元数据层：`NCDT`

### 2.1 位置与作用

```
moov / udta / NCDT
```

**没有它，相机直接报"无法显示此文件"。**
它是相机的"资产描述"：拍摄信息 + 预览图。

### 2.2 子盒结构（实测尺寸）

| 子盒 | 内容 |
|---|---|
| `NCHD` | 18 B 固定头（`"Nikon\0"` + 版本），**所有文件完全相同** |
| `NCTG` | 元数据记录表（5,561 B，定长） |
| `NCTH` | **JPEG 160×120**（列表小图，4–6 KB） |
| `NCVW` | **JPEG 全分辨率**（1920×1080 约 484 KB；4K 约 1.49 MB） |
| `NCM1` | **JPEG 640×360**（约 43 KB） |
| `NCM2` | **JPEG 1920×1080**（1080p 文件里是 8 B 空盒；4K 文件里约 334 KB） |
| `NCDB` | 8 B 空盒 |

> 子盒尺寸会随缩略图内容变化。**改了缩略图就必须重建外层 `udta` 自己的 size 字段**，
> 否则整棵 `moov` 解析错位（现象：连 `NCDT` 都找不到）。帧数/时间是等长改写，不会暴露这个问题。

### 2.3 `NCTG` 记录：相机怎么算"时长"

记录格式：`tag(4) | fmt(2) | cnt(2) | 值`

**关键结论：`NCDT` 里根本没有"时长"字段。**
对比三段不同时长的相机原片（228 帧/3.80 s、384 帧/6.41 s、144 帧/2.40 s），
`NCHD` 逐字节相同，`NCTG` 里与内容相关的只有：

| tag | 含义 |
|---|---|
| `0x13` | `[帧数, 0]` ← **相机的时长 = 帧数 ÷ 帧率** |
| `0x16`/`0x17` | 帧率（`60000/1001`） |
| `0x11`/`0x12`/`0x1002` | 拍摄时间（ASCII 本地时间） |
| `0x19` | 时区（`+08:00`） |
| `0x101`/`0x102` | 缩略图尺寸描述 |
| EXIF 类 | `0x110829d` 光圈、`0x1108832` ISO 等 |

也就是说：**只改 `0x13` 就能改相机显示的时长**；容器里的 `mvhd`/`mdhd`/`elst`
只是给通用播放器看的，相机自己用 `0x13 ÷ 0x16`。

### 2.4 时间要改两套

| 位置 | 内容 |
|---|---|
| `mvhd` / `tkhd` / `mdhd` 的 creation+modification | QuickTime 纪元秒（**UTC**，1904-01-01 起算） |
| `NCTG 0x11` / `0x12` | ASCII **本地时间**（`YYYY:MM:DD HH:MM:SS`） |

两者相差 `NCTG 0x19` 里的时区。**只改一套就自相矛盾。**

实测：只改时间时，文件与"已验证能播"的版本**只差 42 字节，全在 `moov` 里**。

### 2.5 ★ 缩略图必须是"相机格式"的 JPEG

这是最阴的一个坑：**缩略图格式不对，相机会拒绝整个文件**（不是"缩略图显示不出来"，
而是整个视频播不了）。

实测相机 JPEG 与 ffmpeg 默认输出的差别：

| 项 | 相机 | ffmpeg 默认 |
|---|---|---|
| Y 采样 | **2:1（4:2:2）** | 2:2（4:2:0） |
| 量化表 | **2 张**（亮/色各一，DQT 段长 132） | 1 张（段长 67） |
| 标记序列 | `DQT DHT SOF0 SOS` | `APP0(JFIF) COM DQT DHT SOF0 SOS` |

**原理**：相机那块缩略图解码器是定死的硬解，只认它自己编码器产出的那套结构。

**正确做法**：用 Pillow（libjpeg）编码，`subsampling=1` 出来的正好是 `2:1/1:1/1:1`；
量化表直接用相机模板里那张；再把 DQT 合成一段、DHT 合成一段、**重排成相机的顺序**；
最后去掉 `APP0`/`COM`。

**踩过的坑（重要）**：一开始想省事——用 ffmpeg 的 `-pix_fmt yuvj422p` 编码，
然后把 SOF 里的采样率改成相机的值。**这是错的**：
ffmpeg 的 mjpeg 即使给 yuvj422p，写出来的采样率其实是 `2:2/1:2/1:2`；
只改 SOF 的**声明**、不改熵编码数据，JPEG 直接损坏（实测每张 2 个解码错误，`error y=0 x=0`），
相机于是拒绝整个文件。

> **教训**：光对比头部字段不算验证，**必须真的解码一次**（`ffmpeg -v error` 必须 0 行）。

---

## 三、码流层：为什么必须和相机的编码器一致

这一层是整套东西里最反直觉的。

### 3.1 核心原理

H.265 的**参数集（VPS/SPS/PPS）不只是"声明"，它们是解码切片数据的语法依赖**。

解码器解析一个 CU（编码单元）时，要先从 SPS/PPS 里读：
- CTU 多大（`log2_diff_max_min_luma_coding_block_size`）
- 最小编码块多大（`log2_min_luma_coding_block_size_minus3`）
- 变换块能不能递归（`max_transform_hierarchy_depth_*`）
- SAO 开不开（`sample_adaptive_offset_enabled_flag`）
- AMP 开不开（`amp_enabled_flag`）
- CABAC 初始化的上下文（`cabac_init_present_flag`）
- 去块滤波参数在不在切片头里（`deblocking_filter_control_present_flag`）

**这些字段一旦被改，切片数据的解析就全错位。**
所以它们"只能在编码时决定，不能事后改"。

相机等于要求：**用它的编码器配置重新编一遍**，而不是"编一个合法的 H.265 塞给它"。

### 3.2 实测必须一致的字段

对照相机 `DSC_8955.MOV`：

| 字段 | 相机值 | 说明 |
|---|---|---|
| `pic_height_in_luma_samples` | **1088** | ★ 见 3.3 |
| `conformance_window_flag` | **1** | ★ |
| `conf_win_bottom_offset` | **4** | 裁 4×2 = 8 行 → 显示 1080 |
| `log2_min_luma_coding_block_size_minus3` | 0 | 最小编码块 8×8 |
| `log2_diff_max_min_luma_coding_block_size` | 3 | **CTU 64** |
| `max_transform_hierarchy_depth_inter/intra` | 0 / 0 | 变换块不递归 |
| `sample_adaptive_offset_enabled_flag` | 0 | SAO 关 |
| `amp_enabled_flag` | 0 | AMP 关 |
| `cabac_init_present_flag` | 0 | |
| `deblocking_filter_control_present_flag` | 0 | |
| `sign_data_hiding_enabled_flag` | 0 | |
| `weighted_pred_flag` | 0 | |
| `entropy_coding_sync_enabled_flag` | 0 | **不开 WPP** |
| `num_entry_point_offsets` | 无 | WPP 关掉后切片头不再带这个 |
| `five_minus_max_num_merge_cand` | 3 | merge 候选 2 个 |
| `nal_hrd_parameters_present_flag` | 无 | 相机不写 HRD |
| `vps_timing_info_present_flag` | **1** | 帧率时序也写在 VPS 里 |
| `vps_num_units_in_tick / vps_time_scale` | 1001 / 60000 | |
| `sps/vps_max_dec_pic_buffering_minus1` | 1 | DPB = 2 |
| `sps/vps_max_latency_increase_plus1` | 0 | |
| `colour_primaries / transfer_characteristics` | 1 / 1 | BT.709 |
| `video_full_range_flag` | 1 | 全范围 |
| `aspect_ratio_info_present_flag` | 0 | 相机不写 SAR |
| `general_frame_only_constraint_flag` | 0 | |
| `general_progressive_source_flag` | 0 | |
| VPS/SPS 的 constraint flags | 0 | 且必须与 `hvcC` 里的记录头一致 |

### 3.3 ★ 最关键的一条：编 1088 再裁成 1080

```
相机 SPS：pic_height_in_luma_samples = 1088
          conformance_window_flag    = 1
          conf_win_bottom_offset     = 4      （4 × 2 = 8 行）
容器 tkhd / 显示尺寸            = 1920 × 1080
```

**1080 不是 16 的倍数**，硬件编码器习惯编到 16 的倍数（1088 = 68×16），
再用 `conformance_window` 裁掉多出来的 8 行。

如果按 1080 直接编、没有裁剪窗口，相机就"**能识别但读取无限加载**"。

**这条是最后一道门**：前面 10 项都对齐了、只差这个，照样不播；
补上这个之后立刻能播。

**通用规则**：编码高度 = 显示高度向上取到 16 的倍数，
`conf_win_bottom_offset = (编码高度 − 显示高度) / 2`。
1080 → 1088、offset 4，**正好就是相机的取值**。

### 3.4 关键帧类型必须全是 `IDR_W_RADL`

NAL 单元类型实测（相机整条片子）：

```
AUD(35) + IDR_W_RADL(19) + TRAIL_R(1)      只有这三种
```

**没有 CRA(21)，没有 IDR_N_LP(20)。**

x265 默认是**开放 GOP**，关键帧用 **CRA(21)** —— 相机的硬件解码器不认，表现为卡在加载。
必须 `open-gop=0`。

### 3.5 AUD 的 `pic_type` 位

AUD 载荷只有 1 字节：`pic_type u(3) + alignment_bit u(1) + 0000`

```
I 帧 = 000 1 0000 = 0x10
P 帧 = 001 1 0000 = 0x30
```

**对齐位在第 4 位（bit 4），不是第 3 位。**
写错（比如 `0x08`/`0x18`）会把 P 帧标成 I 帧。

### 3.6 WPP 会给切片头塞 entry point

x265 默认开 WPP（`entropy_coding_sync_enabled_flag=1`），
于是**每个切片头里多出十几个 `entry_point_offset`**。相机不认。
必须 `wpp=0`。

### 3.7 `hvcC` 必须和码流里的参数集自洽

`hvcC` 是 `stsd` 里的"参数集副本"，供播放器不去扫码流就能初始化。
它内部有一个 **23 字节记录头**（profile/tier/level/constraint flags 等）。

实测相机值：

```
01 21 60 00 00 00 00 00 00 00 00 00 96 f0 00 fc fd f8 f8 00 00 4f 03
```

**踩过的坑**：
- `profile_idc` 写成 0 是**非法值**（Main profile 应该是 1）
- `hvcC` 的记录头里 constraint flags 清零了，**码流里的 VPS/SPS 却还是 `0xF0`** →
  文件自相矛盾。必须两处一起改。

---

## 四、为什么 GPU（NVENC）不行

这是个很容易想当然的地方："只要编出合法 H.265 就行"。**不是。**

实测 NVENC 出的文件与相机差 **16 个字段，其中 14 个改不了**：

| 类别 | 字段 | 相机 | NVENC | 能否修补 |
|---|---|---|---|---|
| **编码块结构** | CTU | 64 | **32** | ❌ 改了切片数据就废 |
| | 最小编码块 | 8×8 | **16×16** | ❌ |
| | TU 深度 inter/intra | 0 | **3** | ❌ |
| | SAO | 关 | **开** | ❌（切片数据里有 SAO 语法） |
| | AMP | 关 | **开** | ❌ |
| | scaling list | 开 | **关** | 部分 |
| **PPS 开关** | `cabac_init_present_flag` | 0 | **1** | ❌ 切片头会多 cabac_init_flag |
| | `deblocking_filter_control_present_flag` | 0 | **1** | ❌ 切片头多一段 |
| | `num_ref_idx_active_override_flag` | 0 | **1** | ❌ 切片头多一段 |
| | merge 候选数 | 2 | **5** | ❌ |
| **HRD** | `nal_hrd_parameters_present_flag` | 无 | **有** | ❌ 整套参数 |
| 约束标志 | frame_only / progressive | 0 / 0 | **1 / 1** | ✅ 可改 |

**原理**：这些字段决定的是**切片数据怎么解析**。解码器按 SPS/PPS 的声明去读比特，
你把声明改了、数据没改，读出来的就是垃圾。所以它们**只能在编码时决定**。

**结论**：相机实际上要求"**用它自己那套编码器配置**"。目前只有 **x265** 能精确复现
（`ctu=64:min-cu-size=8:sao=0:signhide=0:weightp=0:wpp=0:open-gop=0:max-merge=2:ref=1`），
NVENC 给不出这个组合。

**想快只能用 x265 的快速预设**：`medium` → `fast` 大约快 2.6 倍
（4 分 15 秒的片子：40 分钟 → 20 分钟），且关键 SPS 字段实测仍与相机一致。

---

## 五、位级改写参数集的原理与陷阱

有些字段 x265 没有命令行开关（VPS 时序信息、DPB、latency 等），
只能在参数集的 **RBSP 位串**上做位级改写。这一层有几个必踩的坑：

### 5.1 基本概念

- NAL = 2 字节头 + **RBSP**
- 字节流里为了防止出现起始码，插入了**防竞争字节** `0x03`（`00 00 03 xx`）
  → 解析前必须 `unescape`，改完再 `escape`
- RBSP 末尾是 `rbsp_trailing_bits`：一个 `1` + 若干 `0` 补齐到字节

### 5.2 陷阱一：`vps_reserved_0xffff_16bits`

VPS 在 `vps_temporal_id_nesting_flag` 之后还有 **16 位保留字段**（`0xFFFF`）。
漏读这 16 位，后面全部错位。

### 5.3 陷阱二：`profile_tier_level()` 的位序

```
space(2) + tier(1) + profile_idc(5) + compatibility(32) + constraint(48)
+ general_level_idc(8)      ← 在这
+ 子层 present 标志
+ （若 max_sub_layers > 1）reserved_zero_2bits + 各子层 PTL
```

`general_level_idc` 在 **constraint flags 之后、子层标志之前**。漏读会错位。

### 5.4 陷阱三：插入 vs 替换

要加 VPS 时序信息时，必须**替换**原来那一位 `vps_timing_info_present_flag`，
**不能插在它前面**。

插在前面会留下一个多余的 `0` 位：解码器在 `vps_extension_flag` 之后本应直接读到
`rbsp_stop_one_bit`，那个残留的 `0` 会让**整个 VPS 解析失败**——
现象是 **VPS 之后的 SPS/PPS 全都不再被解析**（`trace_headers` 里连
`Sequence Parameter Set` 段都不出现）。这个现象极具误导性，会让人以为 SPS 有问题。

### 5.5 陷阱四：Annex-B 尾部的多余 `00` 字节

x265 输出的 NAL 尾部会带 `trailing_zero_8bits`（不属于 RBSP）。
改写中间字段后如果还按原来的字节长度补零，停止位的位置就乱了。

**正确做法**：先按"**最后一个 `1` 位**"规范化尾部（保留到最后一个 1，再补齐到字节边界），
再做字段改写。

### 5.6 陷阱五：`st_ref_pic_set()` 必须会跳

SPS 里的 `num_short_term_ref_pic_sets` 后面跟 N 个 `st_ref_pic_set()`。
x265 默认给 **0 个**（循环不执行），但 **NVENC 给 1 个**——
不会跳过它就会卡在 SPS 中间，VUI / `aspect_ratio` 全都读不到，
整个参数集对齐直接失败。

### 5.7 陷阱六：某些字段"不能单独改"

实测：把 PPS 的 `lists_modification_present_flag` 从 0 翻成 1（相机就是 1），
**会让切片头解析错位**（12 帧出现 10 个 `alignment_bit_equal_to_one=0`）。

**原理**：这个开关是"允许"语义，它必须与切片头里的 `ref_pic_lists_modification()`
**同时存在**。单翻开关 = 解码器去找一段不存在的语法。

> **教训**：位级改写后**必须真的解码验证**（`ffmpeg -v error` 0 行），
> 不能只看"我自己重新解析出来是对的"——自己写的解析器会跟着自己的错误一起错。

---

## 六、校验原理：门禁为什么这么设

因为这套东西的失败模式是"**能识别但播不了**"，没有任何通用工具能提前告诉你，
所以需要一份**针对性门禁**。

### 6.1 必须查什么

| 检查项 | 为什么 |
|---|---|
| 顶层布局 + `mdat` 位置 | 第一层判据 |
| 轨道数 / `stbl` 子盒序列 | 结构同构 |
| **时长自洽**（`mvhd` ≥ 每轨；等于最长轨） | 防音频尾部被切 |
| `NCDT` 存在 + 子盒同序 | 缺了报"无法显示" |
| `NCDT` 帧数 == 实际帧数 | 相机时长来源 |
| `mvhd`/`mdhd` 的 UTC 时间 == `NCDT` 本地时间 + `0x19` 时区 | 两套时间必须自洽 |
| 缩略图：标记序列 / 采样 / 量化表段数同相机 + **各自解码 0 错误** | JPEG 格式错会拒绝整个文件 |
| 参数集与切片头 **33 个字段** | 第三层判据 |
| 整体解码 0 错误 | 兜底 |

### 6.2 ★ 切片头检查必须 trace 到 **P 帧**

这是本项目踩的最大一个校验疏漏：

**只 trace 第 0 帧（IDR）会漏掉 P 帧切片头与 PPS 级开关的差异。**
GPU 版有 16 处差异，其中大半在 P 帧切片头 / PPS 上，
而当时的门禁（20 个 SPS/PPS 字段 + 只看 IDR）**让它通过了**。

正确做法：
- `trace_headers` 取 **3 帧**，同名字段**取最后一次出现**（这样拿到的是 P 帧的值）
- 字段清单必须包含 PPS 级开关：`cabac_init_present_flag`、
  `deblocking_filter_control_present_flag`、`num_ref_idx_active_override_flag`、
  `nal_hrd_parameters_present_flag`

### 6.3 ★ 门禁不能"为了放行而放宽"

当时我给自己加了一个"GPU 豁免"（把 NVENC 改不了的字段排除在检查外），
结果**坏文件通过了门禁、相机不认**。

**原理**：门禁的判据应该是"**和已真机验证能播的文件同配方**"，
而不是"我认为这些差异不要紧"。任何豁免都必须有真机验证背书。

### 6.4 也不能过头：哪些字段不该进"必须一致"清单

反例：`num_negative_pics`（切片级参考图像集）——
**能播的 x265 文件是 2、相机是 1**，它随编码器实现自然波动，不是相机的判据。
把它放进必须一致清单会造成假警报。

**判据来源**：只有"**已验证能播的文件也一致**"的字段，才能进必须一致清单。

---

## 七、失败模式对照表

| 相机表现 | 可能原因 | 定位方法 |
|---|---|---|
| **"无法显示此文件"** | `NCDT` 缺失 / `udta` size 没重建 / `hvcC` 记录头非法 | 查 `moov/udta/NCDT` 是否存在且可解析 |
| **能识别，读取无限加载** | 码流结构不符（CTU/SAO/CRA/WPP/entry points/砍掉一半的字段…） | 逐字段与相机对比，**必须 trace 到 P 帧** |
| 能播放但**只播开头几秒** | `elst` 的时基用错（用了 track timescale） | 查 `elst.segment_duration` 是否按 movie timescale |
| 能播放但**音频尾部被切** | `mvhd.duration` 小于音频轨时长 | 查 `mvhd` 是否等于最长轨 |
| 能播放但**音画不同步** | 音频 chunk 没按视频时间轴切 | 按 `floor(累计视频tick/60000×48000)` 重算 |
| 能播放但**时长显示不对** | `NCTD 0x13` 帧数没改对 | 相机时长 = `0x13 ÷ 0x16` |
| 能播放但**缩略图是别人的画面** | 模板的 `NCTH`/`NCVW`/`NCM1` 没替换 | 用首帧重建，注意 JPEG 必须是相机格式 |
| 能播放但**拍摄时间是别的片子** | `NCTG 0x11/0x12` 和容器头没一起改 | 两套一起改，时区取自 `0x19` |
| 文件被拒但**解码 0 错误** | 码流合法但编码器配置与相机不一致（如 NVENC） | 查 CTU/SAO/minCU/TU 深度等结构性字段 |

---

## 八、一句话总结每一层

```
容器层：让相机"能顺序读到"——mdat 落在固定偏移，样本表自洽，音视频按时间交错。
元数据层：让相机"认得并愿意展示"——必须有 NCDT，缩略图必须是相机格式的 JPEG。
码流层：让相机"解得开"——参数集与切片头必须等于它自己编码器的配置，
        尤其是"编 1088 再裁成 1080"这一条。
```

**三层都过，才播。**
而"解码 0 错误"只是第三层的**必要条件**，远不是充分条件——
相机要的是**同构**，不是**合法**。
