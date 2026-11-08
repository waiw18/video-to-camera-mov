# 尼康容器解析器子集 — 外部研究参考 + 我方实测

> 建立于 2026-10-17。目的：把「靠真机盲试」升级为「读尼康固件自己那份 ISO BMFF 解析器的判定条件」。
> 本仓库的 43 项门禁是**我们自己**的规则（真机试出来的），这份文档带来**第二把尺子**：外部研究者
> 从固件里逆向出的、固件接受的容器子集。

---

## 一、外部来源

| 来源 | 内容 | 许可 |
|------|------|------|
| `WonderLantern/z9-qemu` → `tools/isobmff_raw_index.py` | 固件侧 ISO BMFF 索引器（host 实现），**专门读固件自己写出的电影** | GPL-2.0-or-later |
| `WonderLantern/z9-qemu` → `tools/recording_files.py` | 从「虚拟录制卡」里列出/导出成片，附证据门禁 | GPL-2.0-or-later |
| `WonderLantern/z9-qemu` → `tools/virtual_recording_card.py` | 生成虚拟 exFAT 卡（不碰真实设备） | GPL-2.0-or-later |
| `WonderLantern/z9-qemu` → `docs/shared221.md` | 那一轮验证的证据清单与反复强调的纪律 | 文档 |

**我们没有把他们的代码复制进本仓库**（GPL 传染性 + 我们的工具是独立实现）。
`tools/nikon-box-audit.py` 只用 `struct` 手撕 MP4，**不 import 仓库里的 boxio** —— 同源实现抓不出同源 bug，
异源可以。

### 1.1 他们那份解析器的拒绝条件（= 固件的口味）

- 顶层盒链必须自洽；**恰好一个 `moov`，且 `moov` ≤ 16 MiB**（`One bounded moov required`）
- **恰好一个 `trak`**（他们的对象是 N-RAW 单轨；我们是 H.265+PCM 双轨，这点不适用）
- `hdlr` 必须是 `vide`；`stsz` 必须是 **version0**
- `stco` / `co64` **二选一**（不能都有、不能都没有）
- `stsc` 首项必须从 chunk 1 开始、`samples-per-chunk != 0`、`sample-description-index == 1`，
  且块号严格递增
- 由 `stsc` 展开的样本必须落在 `mdat` 内、**单调不重叠**、样本范围与时长完整
- **只要出现非零 `ctts`（帧重排）就判不合格**（`Reordered RAW samples outside qualified scope`）

### 1.2 他们反复强调的证据纪律（值得我们照抄）

- 「**Metadata inspection is not codec acceptance**」——元数据能读 ≠ 编码被接受。
- 「**byte identity, not hash equality**」——同一轮结论要有字节级一致，不能只看哈希相等。
- 「**interrupted test retained as interrupted evidence**」——被打断的测试如实记为"被打断"，
  不许当成通过。
- 「导出 API 调用成功」**不算**证据（他们明确拒绝把 API 成功当录像成功的凭据）。
- 有界索引 vs ffprobe 逐包清单对齐：他们 4443 条 packet 条目（含 >4 GB 偏移）全部对上，
  3609 条样本约 0.2 s 完成 —— 我们现在做的正是同一件事。
- 只允许**一处**声明过的 guest RAM 补丁（`ui_language_preference`），出现任何其它 RAM 改动即判失败。

---

## 二、我方新增：`tools/nikon-box-audit.py`

```powershell
# 单独跑
python tools\nikon-box-audit.py 输出.MOV [--no-ffprobe] [--quiet]
# 门禁 + 附加一起跑（推荐）
python tools\verify-output.py 输出.MOV --template cam-tests\DSC_8960.MOV --extra
```

- **20 项**（含 ffprobe 逐包对照）/ 19 项（`--no-ffprobe`）。退出码 0/1，并打印
  `##CAMMOV {"k":"extra","ok":..,"bad":..,"total":..,"failed":[..]}` 供脚本读取。
- 检查内容：顶层盒链、moov 大小上限、trak 数、每轨 `stco/co64` 二选一、`stsc` 形态、
  索引表长度精确自洽、展开样本数 == `stsz` 计数、样本全部落在 `mdat` 数据区、
  单调不重叠、无重排、`mdat` 数据区被样本铺满（除相机同款的 8 字节首部前缀）、
  首个样本位置，以及 ffprobe 逐包偏移/长度对照。
- **`--extra` 不改变 43 项门禁语义**：门禁结论照旧打印 `k:"gate"` 与 43/43；
  附加审查只多印一块，并在 `--extra` 下参与退出码（附加未通过则退出码 1）。

### 2.1 阳性实测（这把尺子不会误报）

| 文件 | 结果 |
|------|------|
| 8 个相机原片 `cam-tests\DSC_8951/8955/8956/8960/9695/9696/9697/9698.MOV` | 20/20 通过 |
| `output\verified\DSC_4521.MOV`（参考基准，380 MB） | 20/20 通过 |
| `output\_mdatraise.MOV`（抬高 mdat 的修复件） | 20/20 通过 |
| `output\_reg.MOV`（回归件） | 20/20 通过 |
| `6427971_da3_合并分片\相机转换输出\DSC_9719.MOV`（698,983,029 B，955 s，28,637 视频样本 / 45,868,075 音频样本） | 19/19 通过，约 1 s |
| `templates\DSC_8955.MOV`、`templates\DSC_8960.MOV` | 19/19 通过 |
| 彻夜之歌 `相机转换输出\DSC_9701.MOV`（949 MB，41,269 帧） | 43/43 + 20/20 通过 |
| 彻夜之歌 `相机转换输出\DSC_9713.MOV`（1,039,060,343 B） | 43/43 + 20/20 通过 |

> 校验时必须用**构建时用的那条模板**：彻夜之歌 13 部都是 29.97，用 `templates\DSC_8960.MOV`；
> 拿 59.94 的 `templates\DSC_8955.MOV` 去校验会挂 3 项（`mdat 位置` 与两条 `tkhd.duration`），
> 那是模板不同，不是产物有问题（2026-10-17 实测踩到，已记在此）。


音频是等长样本（`stsz` 固定 sample_size），工具**绝不逐样本展开**，按块做数学等价的检查，
所以几千万样本也是秒级。

### 2.2 阴性对照（这把尺子不会永远通过）

用 `tools/box-mutant.py` 生成 mutant（盒子都在 `mdat` 之前，原地改字节不移动偏移）：

| mutant | 结果 |
|--------|------|
| 截断 1 字节 | 挂 `A1 顶层盒链自洽` |
| 全部 chunk 偏移 +4 / +8 / +4096 | 挂 `mdat 铺满（无空洞）` + `首个样本紧接 8 字节前缀` |
| 全部 chunk 偏移 -8 | 挂 `mdat 铺满（无空洞）`（跨轨空洞）+ `首个样本位置` |
| 全部 chunk 偏移 -9 | 挂 `样本落在 mdat 数据区内` + 上两条 |
| 某样本长度改成 0 | 挂 `样本落在 mdat 数据区内` |
| 某样本长度改成 2³¹-1 / 2³²-1 | 挂 `样本落在 mdat 数据区内` + `单调不重叠` |

---

## 三、本次新测到的相机事实

1. **`mdat` 数据区首部有 8 字节全 0 前缀**：8/8 相机原片、以及我们**全部**产物都是如此，
   首个样本总在 `mdat 数据区起点 + 8`。（成因未追；我们的工具与相机一致。）
   因此「无空洞」检查把这一处 8 字节前缀作为例外承认，其余任何空洞都判失败。
2. **相机原片与我们的产物都没有 `ctts`**（无 B 帧重排）→ 之前「B 帧重排导致长片快进不夹紧」的
   假设**作废**。我们的编码参数写死 `bframes=0`（`tools/video-to-camera-mov.py:411`），
   `tools/make-camera-mov.py:116` 也依赖这一点（非 IDR 必为 P）。
3. 索引表全部 version0、`stsc` 首项 chunk=1 / spc≠0 / sdi=1 / 块号严格递增 —— 相机与我们的产物一致。
4. 数据区**无空洞**（除上述 8 字节前缀）→「写盘索引漏段」这类损坏在我们产物里不存在。

---

## 四、为什么这还不能直接解开「长片快进不夹紧」

- 他们那份解析器的对象是 **N-RAW 单轨**电影；我们是 H.265+PCM 双轨。它只覆盖
  「容器索引子集」这一层，不覆盖参数集与编码层（那是 43 项里的事）。
- 它能证明的是「我们没写出损坏的索引」，**不能**证明「相机为什么有时不夹紧」——
  那个判定逻辑在固件里。要直接读它，只有走下面的 route B。

## 五、Route B（尚未启动）：在模拟器里让固件自己写 MOV

- 需要：Linux/WSL2 + 构建 QEMU（`z9-qemu` 是 QEMU 10.2.4 的源码 overlay）+ **自备 Z9/ZR 固件**
  （他们不提供固件，仓库明确声明不含尼康二进制）。
- 产出（他们已经做到的）：固件自己的录像路径把成片写进「虚拟录制卡」（虚拟 exFAT 镜像），
  文件名就是相机命名 `DCIM/100NIKON/DSC_0001.MOV`（`docs/recording-qualification221.json` 里
  main10 两个 clip 各 909 帧 / `30.330300` s，`whole_functional_workflow_verified: true`）。
  拿到固件亲笔写的 MOV + 固件自己的索引器行为，才是真正的「真值」。
- 风险与成本：自备固件（版权/法律灰色，**绝不能进产品**）、WSL 环境与构建时间；
  他们自己也标注 `zr-qemu` 的「Movie recording incomplete」、z9 那轮验证范围写的是
  functional recording（`original_algorithms_verified: false`、`color_calibration_verified: false`）。
- 结论：**路线成立但成本高，暂不启动**；成本与产出已如实记在这里，供以后决策。

---

## 六、已知局限与 TODO

- 工具目前整份读入内存（为了顶层盒链与各表都随手可取），**限 4 GiB**；要支持更大文件，
  应改成按需分页读（他们那份实现就用有界读，明确拒绝为了 packet 偏移去读 28 GB 载荷）。
- 只查容器索引层；不查参数集/切片头（那在 `verify-output.py` 的 43 项里）。
- `--extra` 下的附加失败会让退出码为 1，但**不进入** `k:"gate"` 的 43 项计数（界面照旧只看 gate）。

## 七、变更记录

- 2026-10-17：新建本文档；新增 `tools/nikon-box-audit.py`；`tools/verify-output.py` 增 `--extra`；
  顺手清掉两处隐患（都与 43 项门禁无关、不改语义）：
  1. 删掉 `FFPROBE = FFMPEG.replace("ffmpeg.exe", "ffprobe.exe")` 死代码 —— `binpath` 给的是
     `...\ffmpeg.EXE`（大写扩展名），这行只会把 ffmpeg 当 ffprobe 用，输出为空、静默失败。
  2. `a = trs[1]` 原本把 argparse 的名字空间 `a` 覆盖成音频轨，后面再读任何参数就会
     `AttributeError`。已改名为 `atr`。
