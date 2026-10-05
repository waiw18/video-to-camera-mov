"""相机视频转换器 —— 图形界面入口（tkinter，卡片式）。

可以两种方式跑：
  1) 源码运行：python app/main.py
  2) 打包后：相机视频转换器.exe（PyInstaller，见 app/build-exe.ps1）

打包后**不需要额外的 Python**：工具模块被 PyInstaller 一起冻结，
这里用 importlib 在进程内加载它们（工具本身只用 subprocess 调 ffmpeg）。

界面按 design/前端设计稿-v1.html 的卡片式排版实现。三条硬约束（见
交接-发布测试版.md）在界面上是**锁死并写明原因**的，不是"默认值"：
    · 并行段数 --jobs 1（>1 实测相机播到中途跳出）
    · 输出帧率 60000/1001（--fps source 走 29.97 那条路没打通）
    · 码流容器参数一律由工具按相机实测值写（tkhd.duration 保持模板原值）

流程：转换 → 自动跑 43 项门禁 → 门禁全过才允许"拷到卡上"。
进度靠工具 stdout 上的协议行（见 tools/video-to-camera-mov.py 的 _emit）：
    ##CAMMOV {"k":"prog","pct":42.3,"eta":1003,...}
这些行不进日志区，只驱动进度卡片。
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

FROZEN = getattr(sys, "frozen", False)


def res_dir():
    """资源目录：打包后在 _MEIPASS，源码运行时在项目根。"""
    if FROZEN:
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


RES = res_dir()
TOOLS = RES / "tools"
# 工作目录（中间文件）：放在 exe 旁边，别写进 _MEIPASS
WORK = Path(sys.executable).parent / ".camwork" if FROZEN else RES / ".probe"


def default_outdir():
    """默认输出目录。

    打包后**不能**默认写进程序自己的目录：单文件版解包在临时目录（_MEIPASS），
    程序一退出连转换好的视频一起没了。放系统"视频"文件夹下最不容易找不到。
    """
    if not FROZEN:
        return RES / "output"
    home = Path.home()
    base = home / "Videos"
    if not base.is_dir():
        base = home
    return base / "相机转换输出"

APP_VERSION = "v0.1.0-beta.1"
MAXW = 1010                                  # 内容列最宽，再宽就居中留白
CANCEL_FLAG = WORK / "cancel.flag"          # 取消转换用（工具 run_prog 会轮询）

# ---- 设计稿里的色板（design/前端设计稿-v1.html）----
BG = "#eceef1"
CARD = "#ffffff"
INK = "#1c2024"
INK2 = "#5b6570"
INK3 = "#8b959f"
LINE = "#e3e7eb"
LINE2 = "#eef1f4"
ACCENT = "#2563c9"
ACCENT_D = "#1d4fa8"
ACCENT_SOFT = "#eaf1fd"
OK = "#1a7f4b"
OK_SOFT = "#e8f5ee"
WARN = "#9a6700"
WARN_SOFT = "#fdf6e3"
WARN_LINE = "#f0e0b8"
DANGER = "#b42318"
LOG_BG = "#1b1f24"
LOG_INK = "#d7dee6"
LOG_DIM = "#8b959f"
LOG_OK = "#7fd18b"
LOG_BAD = "#ff9a90"
LOG_WARN = "#ffd479"
BOX = "#dfe4ea"

F_BODY = ("Microsoft YaHei UI", 10)
F_TITLE = ("Microsoft YaHei UI", 10, "bold")
F_SMALL = ("Microsoft YaHei UI", 9)
F_BIG = ("Microsoft YaHei UI", 12, "bold")
F_MONO = ("Consolas", 9)

# 普通模式的预设（都是真机验证过的组合）
# 8 Mbps 档实测相机不认（文件能转出来，但相机不播），所以本版不给选；
# 高级模式的码率输入框里也写了同样的提醒。
PRESETS = [
    ("推荐 · 15 Mbps / fast（真机验证）", "15M", "fast"),
    ("高画质 · 40 Mbps / medium（更慢更大）", "40M", "medium"),
]
# 编码以外的阶段占总耗时约 3%，界面的总进度条按这个权重摊
STAGE_W = [1, 92, 3, 2, 2, 0]
STAGE_CUM = [sum(STAGE_W[:i]) for i in range(len(STAGE_W))]


def prep_env():
    """把 ffmpeg 与工作目录告诉工具（它们支持这两个环境变量）。"""
    bindir = RES / "bin"
    for name in ("ffmpeg", "ffprobe"):
        p = bindir / f"{name}.exe"
        if p.exists():
            os.environ[f"CAMMOV_{name.upper()}"] = str(p)
    WORK.mkdir(parents=True, exist_ok=True)
    os.environ["CAMMOV_WORK"] = str(WORK)


def load_tool(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, TOOLS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ffbin(name):
    """ffmpeg / ffprobe 的可执行路径（跟工具用同一套环境变量约定）。"""
    v = os.environ.get("CAMMOV_" + name.upper())
    if v and Path(v).exists():
        return v
    return name


def fmt_secs(s):
    if s is None:
        return "—"
    s = int(max(0, s))
    return f"{s // 60:02d}:{s % 60:02d}"


def fmt_size(n):
    if not n:
        return "—"
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


class App:
    # ---------- 构造 ----------
    def __init__(self, root):
        self.root = root
        root.title("相机视频转换器")
        # 屏幕小或系统缩放在 125%/150% 时，固定的 1000x840 会超出屏幕（Tk 的坐标
        # 是逻辑像素，Windows 会按缩放比放大），所以按屏幕尺寸收一下。
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        # 本进程没做 DPI 感知，Windows 会把窗口整体放大（本机 150%：Tk 看到
        # 1707x1067 的虚拟桌面，1 单位 = 1.5 物理像素），所以尺寸要取屏幕再收一截，
        # 留出任务栏和标题栏；sh-56 是被压到的极限，再小就放不下内容列了。
        w, h = min(MAXW + 40, sw - 80), min(950, sh - 56)
        w, h = max(w, 780), max(h, 560)
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+20")
        root.minsize(min(880, w), min(560, h))
        root.configure(bg=BG)

        self.busy = False
        self.t_start = 0.0
        self.cancel_asked = False
        self.gate_ok = False
        self.done_file = None
        self._photo = None

        prep_env()
        self.tools = {}
        try:
            self.tools["conv"] = load_tool("video-to-camera-mov.py", "convmod")
            self.tools["verify"] = load_tool("verify-output.py", "verifymod")
        except Exception:
            messagebox.showerror("加载失败", traceback.format_exc())

        self.styles()
        self.vars()
        self.build_ui()
        self.apply_mode()
        self.set_state("idle")

    def vars(self):
        v = tk.StringVar
        self.var_mode = v(value="basic")
        self.var_src = v()
        self.var_outdir = v(value=str(default_outdir()))
        self.var_outname = v(value="DSC_4600.MOV")
        self.var_tpl = v(value=str(RES / "templates" / "DSC_8955.MOV"))
        self.var_preset = v(value=PRESETS[0][0])
        self.var_bitrate = v(value=PRESETS[0][1])
        self.var_xpreset = v(value=PRESETS[0][2])
        self.var_size = v(value="1920x1080")
        self.var_fps = v(value="60000/1001 · 59.94（相机原生）")
        self.var_start = v()
        self.var_dur = v()
        self.var_noaudio = tk.BooleanVar(value=False)
        self.var_time = v(value="now")
        self.var_nothumbs = tk.BooleanVar(value=False)
        self.var_keep = tk.BooleanVar(value=False)
        self.var_thumb_mode = v(value="first")
        self.var_thumb_sec = tk.DoubleVar(value=0.0)
        self.var_thumb_img = v()
        self.var_srcinfo = v()

    # ---------- ttk 样式：把 clam 调成设计稿的"白卡片 + 细线" ----------
    def styles(self):
        S = ttk.Style()
        try:
            S.theme_use("clam")
        except tk.TclError:
            pass
        S.configure("TEntry", fieldbackground=CARD, foreground=INK, bordercolor=LINE,
                    lightcolor=LINE, darkcolor=LINE, insertcolor=INK, padding=4)
        S.map("TEntry", bordercolor=[("focus", ACCENT)])
        S.configure("TCombobox", fieldbackground=CARD, background=CARD, bordercolor=LINE,
                    lightcolor=LINE, darkcolor=LINE, arrowcolor=INK2, padding=4,
                    selectbackground=CARD, selectforeground=INK)
        S.map("TCombobox", bordercolor=[("focus", ACCENT)],
              fieldbackground=[("readonly", CARD)])
        S.configure("TCheckbutton", background=CARD, foreground=INK, focuscolor=CARD,
                    padding=2)
        S.map("TCheckbutton", background=[("active", CARD)],
              foreground=[("disabled", INK3)])
        S.configure("TButton", background="#f4f6f8", foreground=INK, bordercolor=LINE,
                    lightcolor="#f4f6f8", darkcolor="#f4f6f8", relief="flat",
                    padding=(12, 6), focusthickness=0, font=F_BODY, arrowcolor=INK2,
                    width=0)   # clam 默认 -width -11：不归零的话所有按钮一样宽
        S.map("TButton",
              background=[("active", "#e9edf1"), ("disabled", "#f2f4f6")],
              foreground=[("disabled", INK3)],
              bordercolor=[("disabled", LINE)])
        S.configure("Primary.TButton", background=ACCENT, foreground="#ffffff",
                    bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
                    padding=(18, 8), font=F_TITLE)
        S.map("Primary.TButton",
              background=[("active", ACCENT_D), ("disabled", "#a9c2e6")],
              foreground=[("disabled", "#eef3fb")],
              bordercolor=[("active", ACCENT_D), ("disabled", "#a9c2e6")])
        S.configure("Cam.Horizontal.TProgressbar", troughcolor=LINE2, background=ACCENT,
                    bordercolor=LINE2, lightcolor=ACCENT, darkcolor=ACCENT, thickness=8)

    # ---------- 小工具 ----------
    @staticmethod
    def card(parent, title, note=""):
        """白底细线卡片：返回 (外壳, 内容区)。"""
        outer = tk.Frame(parent, bg=CARD, highlightbackground=LINE,
                         highlightthickness=1)
        head = tk.Frame(outer, bg=CARD)
        head.pack(fill="x", padx=14, pady=(9, 5))
        tk.Label(head, text=title, bg=CARD, fg=INK, font=F_TITLE).pack(side="left")
        if note:
            tk.Label(head, text=note, bg=CARD, fg=INK3, font=F_SMALL).pack(side="right")
        body = tk.Frame(outer, bg=CARD)
        body.pack(fill="x", padx=14, pady=(0, 10))
        return outer, body

    @staticmethod
    def lab(parent, text, fg=INK, font=F_BODY, width=None, bg=CARD, anchor="w"):
        return tk.Label(parent, text=text, bg=bg, fg=fg, font=font,
                        width=width, anchor=anchor)

    def note(self, parent, text, fg=INK3, font=F_SMALL, pady=(2, 0)):
        """整行说明：跟随父容器宽度自动折行，不然窄窗口里右边会被切掉。"""
        lb = tk.Label(parent, text=text, bg=CARD, fg=fg, font=font, anchor="w",
                      justify="left")
        lb.pack(fill="x", pady=pady)
        parent.bind("<Configure>",
                    lambda e, l=lb: l.configure(wraplength=max(240, e.width - 6)),
                    add="+")
        return lb

    def locked(self, parent, text, note=""):
        """琥珀色"已锁定"格子：看得见、改不了、写明原因。"""
        box = tk.Frame(parent, bg=WARN_SOFT, highlightbackground=WARN_LINE,
                       highlightthickness=1)
        tk.Label(box, text="🔒", bg=WARN_SOFT, fg=WARN, font=F_SMALL).pack(
            side="left", padx=(6, 2), pady=3)
        tk.Label(box, text=text, bg=WARN_SOFT, fg=WARN, font=F_BODY).pack(
            side="left", padx=(0, 8), pady=3)
        if note:
            tk.Label(parent, text=note, bg=CARD, fg=WARN, font=F_SMALL, anchor="w",
                     wraplength=460, justify="left").pack(fill="x")
        return box

    @staticmethod
    def rule(parent):
        tk.Frame(parent, bg=LINE2, height=1).pack(fill="x", pady=(4, 6))

    # ---------- 界面 ----------
    def build_ui(self):
        # 顶部：模式切换（普通/高级）
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=14, pady=(10, 0))
        seg = tk.Frame(top, bg=BG)
        seg.pack(side="left")
        for text, val in (("普通模式", "basic"), ("高级模式", "adv")):
            tk.Radiobutton(seg, text=text, value=val, variable=self.var_mode,
                           indicatoron=0, command=self.apply_mode, bg=LINE2, fg=INK,
                           selectcolor=ACCENT_SOFT, activebackground=ACCENT_SOFT,
                           activeforeground=INK, relief="flat", bd=0, padx=16, pady=6,
                           font=F_BODY, highlightthickness=1,
                           highlightbackground=LINE, cursor="hand2").pack(side="left")
        self.lab(top, "Nikon Z5II · 1080p59.94 / H.265", fg=INK3, font=F_SMALL).pack(
            side="right", pady=6)

        # 固定的底部区（操作 + 进度）：窗口小的时候"开始转换/取消"必须一直在手边，
        # 所以它们不跟着设置区一起滚。先 pack 的就先占位，顺序不能调。
        self.statusbar()
        self.bottom = tk.Frame(self.root, bg=BG)
        self.bottom.pack(side="bottom", fill="x")

        # 主体：可滚动（高级模式很高，窗口小的时候要能滚）
        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=BG)
        self.win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self.on_canvas_resize)
        self.root.bind_all("<MouseWheel>", self.on_wheel)

        self.card_io()
        self.card_params()
        self.card_log()
        self.card_actions(self.bottom)
        self.card_progress(self.bottom)

    def on_canvas_resize(self, e):
        """内容列最宽 MAXW 并在窗口里居中：窗口拉宽时不至于一行拉成两米长。"""
        w = min(e.width - 4, MAXW)
        self.canvas.itemconfigure(self.win, width=w)
        self.canvas.coords(self.win, max(0, (e.width - w) // 2), 0)

    def on_wheel(self, e):
        """滚轮滚整页；鼠标在日志框里时不抢（让它自己滚）。"""
        try:
            w = self.root.winfo_containing(e.x_root, e.y_root)
        except Exception:
            w = None
        if isinstance(w, tk.Text):
            return
        self.canvas.yview_scroll(int(-e.delta / 120), "units")

    # ①输入与输出
    def card_io(self):
        outer, b = self.card(self.body, "① 输入与输出",
                             "支持 mp4 / mov / mkv / avi / ts / flv / webm")
        outer.pack(fill="x", padx=14, pady=(10, 0))
        g = tk.Frame(b, bg=CARD)
        g.pack(fill="x")
        g.columnconfigure(1, weight=1)
        rows = (("输入文件", self.var_src, self.pick_src),
                ("输出目录", self.var_outdir, self.pick_outdir))
        for i, (text, var, cb) in enumerate(rows):
            self.lab(g, text, fg=INK2, width=9, anchor="e").grid(
                row=i, column=0, sticky="e", pady=4, padx=(0, 8))
            ttk.Entry(g, textvariable=var).grid(row=i, column=1, sticky="we", pady=4)
            ttk.Button(g, text="浏览…", command=cb).grid(row=i, column=2, padx=(8, 0))
        self.lab(g, "输出文件名", fg=INK2, width=9, anchor="e").grid(
            row=2, column=0, sticky="e", pady=4, padx=(0, 8))
        name = tk.Frame(g, bg=CARD)
        name.grid(row=2, column=1, sticky="we", pady=4)
        ttk.Entry(name, textvariable=self.var_outname, width=24).pack(side="left")
        self.lab(name, "相机按 DSC_#### 编号，别和卡上已有的重名", fg=INK3,
                 font=F_SMALL).pack(side="left", padx=10)
        self.lab(g, "").grid(row=2, column=2)
        self.lbl_srcinfo = self.lab(b, "", fg=INK3, font=F_SMALL)
        self.lbl_srcinfo.pack(fill="x", pady=(6, 0))

    # ②转换参数（普通/高级两套，切换显示）
    def card_params(self):
        outer, b = self.card(self.body, "② 转换参数")
        outer.pack(fill="x", padx=14, pady=(10, 0))
        self.adv_boxes = []
        self.f_basic = tk.Frame(b, bg=CARD)
        self.f_adv = tk.Frame(b, bg=CARD)
        self.build_basic(self.f_basic)
        self.build_adv(self.f_adv)
        self.thumb_group(b)          # 缩略图两种模式共用

    def build_basic(self, f):
        self.note(f, "普通模式只用真机验证过的配方：1920×1080 / 59.94fps / "
                     "H.265(x265) / PCM 24bit，与 Nikon Z5II 原生格式一致。",
                  fg=INK2, pady=(0, 8))
        g = tk.Frame(f, bg=CARD)
        g.pack(fill="x")
        g.columnconfigure(1, weight=1)
        self.lab(g, "转换预设", fg=INK2, width=9, anchor="e").grid(
            row=0, column=0, sticky="e", padx=(0, 8))
        cb = ttk.Combobox(g, textvariable=self.var_preset, state="readonly",
                          values=[p[0] for p in PRESETS])
        cb.grid(row=0, column=1, sticky="we")
        cb.bind("<<ComboboxSelected>>", self.on_preset)
        self.lab(g, "相机模板", fg=INK2, width=9, anchor="e").grid(
            row=1, column=0, sticky="e", padx=(0, 8), pady=(6, 0))
        tpl = tk.Frame(g, bg=CARD)
        tpl.grid(row=1, column=1, sticky="we", pady=(6, 0))
        ttk.Entry(tpl, textvariable=self.var_tpl).pack(side="left", fill="x",
                                                       expand=True)
        ttk.Button(tpl, text="换一个…", command=self.pick_tpl).pack(side="left",
                                                                    padx=(8, 0))
        self.note(f, "✓ 输出固定为 1920×1080 / 59.94fps / H.265(x265) / PCM 24bit，"
                     "与 Nikon Z5II 原生格式一致。", fg=OK, pady=(8, 0))
        self.note(f, "⚠ 源视频是 30fps 时会被补帧到 59.94，转换时间翻倍 —— "
                     "这是相机能播的前提，不要改。", fg=WARN)
        self.note(f, "⚠ 码率别低于 15 Mbps：8 Mbps 档实测相机不认（转得出来、"
                     "相机不播），本版已去掉该档。", fg=WARN)

    def build_adv(self, f):
        self.note(f, "每项可自定义；标「已锁定」的项会破坏相机兼容性，工具不开放。",
                  fg=INK2, pady=(0, 8))

        def group(title):
            self.rule(f)
            self.lab(f, title, fg=INK3, font=F_SMALL).pack(fill="x", pady=(0, 4))
            g = tk.Frame(f, bg=CARD)
            g.pack(fill="x")
            g.columnconfigure(0, minsize=142)   # 用列宽而不是 Label 的 width，
            g.columnconfigure(1, weight=1)      # 否则长标签会被裁掉半截
            g.columnconfigure(2, minsize=146)
            g.columnconfigure(3, weight=1)
            return g

        def row(g, r, c0, c1, c2, c3):
            """一行两个字段：左标签/控件 + 右标签/控件。"""
            self.lab(g, c0, fg=INK2, anchor="e").grid(
                row=r, column=0, sticky="e", padx=(0, 8), pady=4)
            c1.grid(row=r, column=1, sticky="we", pady=4, padx=(0, 18))
            self.lab(g, c2, fg=INK2, anchor="e").grid(
                row=r, column=2, sticky="e", padx=(0, 8), pady=4)
            c3.grid(row=r, column=3, sticky="we", pady=4)

        # 画面
        g = group("画面")
        row(g, 0, "分辨率 --size",
            ttk.Combobox(g, textvariable=self.var_size, state="readonly",
                         values=["1920x1080"]),
            "输出帧率 --fps",
            ttk.Entry(g, textvariable=self.var_fps, state="readonly"))
        self.rule(f)
        self.note(f, "相机 1080p 原生的分辨率与帧率；4K 要配 4K 模板（本版不开放）。")
        g2 = tk.Frame(f, bg=CARD)
        g2.pack(fill="x")
        g2.columnconfigure(0, minsize=142)
        g2.columnconfigure(1, weight=1)
        self.lab(g2, "码率 --bitrate", fg=INK2, anchor="e").grid(
            row=0, column=0, sticky="e", padx=(0, 8), pady=4)
        ttk.Combobox(g2, textvariable=self.var_bitrate,
                     values=["15M", "40M"]).grid(row=0, column=1, sticky="w",
                                                 pady=4)
        self.lab(g2, "CBR 近似；15M 与相机原生同档，40M 画质更好、体积翻倍"
                     "（8M 实测相机不认，别用）",
                 fg=INK3, font=F_SMALL).grid(row=0, column=2, sticky="w", padx=8)

        # 编码
        g = group("编码")
        row(g, 0, "x265 预设 --preset",
            ttk.Combobox(g, textvariable=self.var_xpreset,
                         values=["fast", "medium", "faster", "veryfast"]),
            "编码器 --encoder", self.locked(g, "x265（已锁定）"))
        self.note(f, "GPU(NVENC) 给的是 CTU 32 + TU 深度 3，与相机差得远，"
                     "会产出相机不认的文件。", fg=WARN)
        g = tk.Frame(f, bg=CARD)
        g.pack(fill="x")
        g.columnconfigure(1, weight=1)
        g.columnconfigure(3, weight=1)
        self.lab(g, "并行段数 --jobs", fg=INK2, anchor="e").grid(
            row=0, column=0, sticky="e", padx=(0, 8), pady=4)
        self.locked(g, "1 · 单进程（已锁定）").grid(row=0, column=1, sticky="we",
                                                    pady=4, padx=(0, 18))
        self.lab(g, "mdat 块 --mdat", fg=INK2, anchor="e").grid(
            row=0, column=2, sticky="e", padx=(0, 8), pady=4)
        self.locked(g, "655360（沿用模板）").grid(row=0, column=3, sticky="we", pady=4)
        self.note(f, "并行段数 >1 实测相机播到中途跳出（DSC_4534/4535）；"
                     "mdat 按 0.5 秒分块，是相机的要求。", fg=WARN)

        # 截取
        g = group("截取")
        row(g, 0, "起始 --start",
            ttk.Entry(g, textvariable=self.var_start, width=10),
            "时长 --duration",
            ttk.Entry(g, textvariable=self.var_dur, width=10))
        self.note(f, "⚠ 截取出来的短文件（<1 分钟）在相机上快进越界时不会被夹紧；"
                     "全长文件正常。发布测试版不推荐截取。", fg=WARN)

        # 音频与元数据
        g = group("音频与元数据")
        row(g, 0, "", ttk.Checkbutton(g, text="不带音频 --no-audio",
                                      variable=self.var_noaudio),
            "拍摄时间 --time",
            ttk.Combobox(g, textvariable=self.var_time, state="readonly",
                         values=["now", "keep"]))
        row(g, 1, "", ttk.Checkbutton(g, text="不重建缩略图 --no-thumbs",
                                      variable=self.var_nothumbs),
            "", ttk.Checkbutton(g, text="保留中间文件 --keep",
                                variable=self.var_keep))

        # 模板
        self.rule(f)
        gt = tk.Frame(f, bg=CARD)
        gt.pack(fill="x")
        gt.columnconfigure(0, minsize=142)
        gt.columnconfigure(1, weight=1)
        self.lab(gt, "模板 MOV --template", fg=INK2, anchor="e").grid(
            row=0, column=0, sticky="e", padx=(0, 8))
        ttk.Entry(gt, textvariable=self.var_tpl).grid(row=0, column=1, sticky="we")
        ttk.Button(gt, text="浏览…", command=self.pick_tpl).grid(row=0, column=2,
                                                                 padx=(8, 0))
        ttk.Button(gt, text="恢复默认", command=lambda: self.var_tpl.set(
            str(RES / "templates" / "DSC_8955.MOV"))).grid(row=0, column=3, padx=(6, 0))
        self.note(f, "必须用相机自己录的同规格 MOV 当模板：容器结构、时间基、"
                     "缩略图都照抄它。tkhd 时长为可播放长度，由工具按模板写入，"
                     "不提供修改项（改了这个相机快进越界会卡死）。", pady=(4, 0))

    def thumb_group(self, parent):
        """缩略图：普通/高级模式共用的一个块。"""
        g = tk.Frame(parent, bg=CARD)
        g.pack(fill="x", pady=(10, 0))
        self.thumb_block = g      # 普通/高级参数块要 pack 在它前面
        self.rule(g)
        head = tk.Frame(g, bg=CARD)
        head.pack(fill="x", pady=(0, 6))
        self.lab(head, "缩略图", fg=INK).pack(side="left")
        self.lab(head, "相机播放列表和全屏预览里显示的画面", fg=INK3,
                 font=F_SMALL).pack(side="left", padx=8)
        rowf = tk.Frame(g, bg=CARD)
        rowf.pack(fill="x")
        prev = tk.Frame(rowf, bg=BOX, width=192, height=108)
        prev.pack(side="left")
        prev.pack_propagate(False)
        self.prev_img = tk.Label(prev, text="取帧预览", bg=BOX, fg=INK3, font=F_SMALL)
        self.prev_img.pack(fill="both", expand=True)
        right = tk.Frame(rowf, bg=CARD)
        right.pack(side="left", fill="x", expand=True, padx=(12, 0))
        seg = tk.Frame(right, bg=CARD)
        seg.pack(fill="x")
        for text, val in (("首帧（默认）", "first"), ("指定时间", "time"),
                          ("图片文件", "file")):
            tk.Radiobutton(seg, text=text, value=val, variable=self.var_thumb_mode,
                           indicatoron=0, command=self.thumb_mode, bg=LINE2, fg=INK,
                           selectcolor=ACCENT_SOFT, activebackground=ACCENT_SOFT,
                           relief="flat", bd=0, padx=10, pady=4, font=F_BODY,
                           highlightthickness=1, highlightbackground=LINE,
                           cursor="hand2").pack(side="left", padx=(0, 4))
        self.thumb_rows = {}
        r1 = tk.Frame(right, bg=CARD)
        sc = tk.Scale(r1, variable=self.var_thumb_sec, from_=0, to=600, resolution=0.5,
                      orient="horizontal", showvalue=0, bg=CARD, highlightthickness=0,
                      troughcolor=LINE2, activebackground=ACCENT, sliderrelief="flat",
                      length=220)
        sc.pack(side="left")
        self.var_thumb_sec.trace_add("write", lambda *_: self.thumb_cap())
        ttk.Button(r1, text="取这一帧", command=self.make_preview).pack(side="left",
                                                                        padx=8)
        self.thumb_rows["time"] = r1
        r2 = tk.Frame(right, bg=CARD)
        ttk.Entry(r2, textvariable=self.var_thumb_img).pack(side="left", fill="x",
                                                            expand=True)
        ttk.Button(r2, text="浏览…", command=self.pick_thumb_img).pack(side="left",
                                                                       padx=8)
        self.thumb_rows["file"] = r2
        self.thumb_rows["first"] = tk.Frame(right, bg=CARD)
        self.lab(self.thumb_rows["first"], "取视频第一帧（有截取时取截取起点）—— "
                 "和相机原生行为一致，最稳", fg=INK2, font=F_SMALL).pack(side="left")
        self.prev_cap = self.lab(g, "", fg=INK3, font=F_SMALL)
        self.prev_cap.pack(fill="x", pady=(6, 0))
        self.var_thumb_mode.trace_add("write", lambda *_: self.thumb_cap())

    # 操作
    def card_actions(self, parent=None):
        outer, b = self.card(parent or self.body, "操作")
        outer.pack(fill="x", padx=14, pady=(8, 0))
        self.btn_go = ttk.Button(b, text="① 开始转换", style="Primary.TButton",
                                 command=self.run_convert)
        self.btn_go.pack(side="left")
        self.btn_cancel = ttk.Button(b, text="取消转换", command=self.cancel_job)
        self.btn_ver = ttk.Button(b, text="② 校验门禁", command=self.run_verify)
        self.btn_ver.pack(side="left", padx=6)
        self.btn_copy = ttk.Button(b, text="③ 拷到卡上", command=self.run_copy)
        self.btn_copy.pack(side="left")
        ttk.Button(b, text="打开输出目录", command=self.open_out).pack(side="right")

    # 进度与预计时间
    def card_progress(self, parent=None):
        outer, b = self.card(parent or self.body, "进度与预计时间",
                             "剩余时间按编码速度实时估算")
        outer.pack(fill="x", padx=14, pady=(8, 0))
        head = tk.Frame(b, bg=CARD)
        head.pack(fill="x")
        self.badge = tk.Label(head, text="空闲", bg=LINE2, fg=INK2, font=F_SMALL,
                              padx=8, pady=2)
        self.badge.pack(side="left")
        self.lbl_stage = self.lab(head, "等待开始", fg=INK2)
        self.lbl_stage.pack(side="left", padx=10)
        self.lbl_pct = self.lab(head, "", fg=INK, font=F_BIG, anchor="e")
        self.lbl_pct.pack(side="right")
        self.bar = ttk.Progressbar(b, style="Cam.Horizontal.TProgressbar",
                                   maximum=100.0)
        self.bar.pack(fill="x", pady=(8, 4))
        self.stats = tk.Frame(b, bg=CARD)
        self.stat_lbls = {}
        for i, key in enumerate(("已用时间", "预计剩余", "编码速度", "已处理 / 总时长",
                                 "输出大小")):
            cell = tk.Frame(self.stats, bg=CARD)
            cell.grid(row=0, column=i, sticky="w", padx=(0, 26))
            self.lab(cell, key, fg=INK3, font=F_SMALL).pack(anchor="w")
            v = self.lab(cell, "—", fg=INK)
            v.pack(anchor="w")
            self.stat_lbls[key] = v

    # 日志
    def card_log(self):
        outer, b = self.card(self.body, "日志")
        outer.pack(fill="x", padx=14, pady=(10, 12))
        bar = tk.Frame(b, bg=CARD)
        bar.pack(fill="x")
        for text, cb in (("清空", self.log_clear), ("复制", self.log_copy),
                         ("保存…", self.log_save)):
            ttk.Button(bar, text=text, command=cb).pack(side="right", padx=(6, 0))
        box = tk.Frame(b, bg=LOG_BG)
        box.pack(fill="both", expand=True, pady=(6, 0))
        self.log = tk.Text(box, height=9, wrap="word", bg=LOG_BG, fg=LOG_INK,
                           insertbackground=LOG_INK, font=F_MONO, relief="flat",
                           padx=8, pady=6)
        sb = ttk.Scrollbar(box, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_configure("dim", foreground=LOG_DIM)
        self.log.tag_configure("ok", foreground=LOG_OK)
        self.log.tag_configure("bad", foreground=LOG_BAD)
        self.log.tag_configure("warn", foreground=LOG_WARN)
        self.log_write("[就绪] 选好输入文件与输出目录，点「① 开始转换」。")
        self.log_write("[提示] 4 分钟视频约需 20 分钟（15-12450H），编码占 97%，"
                       "进度不会走得很快。")
        self.log_write("[提示] 转换完成后会自动跑 43 项门禁，43/43 通过才建议拷到卡上。")

    def statusbar(self):
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(side="bottom", fill="x", padx=14, pady=(6, 8))
        self.lbl_status = self.lab(bar, "空闲", fg=INK3, font=F_SMALL, bg=BG)
        self.lbl_status.pack(side="left")
        self.lbl_tpl = self.lab(bar, "", fg=INK3, font=F_SMALL, bg=BG)
        self.lbl_tpl.pack(side="right")
        self.sync_status()
        self.var_tpl.trace_add("write", lambda *_: self.sync_status())

    def sync_status(self):
        """右下那行：门禁状态 + 当前模板 + 版本。门禁过了要真的显示出来。"""
        self.lbl_tpl.configure(
            text=("门禁 43/43 通过 · " if self.gate_ok else "门禁未运行 · ")
            + f"模板 {Path(self.var_tpl.get()).name} · {APP_VERSION}")

    # ---------- 模式 / 缩略图 ----------
    def apply_mode(self):
        adv = self.var_mode.get() == "adv"
        # 缩略图块是两种模式共用的，必须在它前面 pack，否则会被挤到后面去
        if adv:
            self.f_basic.pack_forget()
            self.f_adv.pack(fill="x", before=self.thumb_block)
        else:
            self.f_adv.pack_forget()
            self.f_basic.pack(fill="x", before=self.thumb_block)
            self.sync_preset_label()
        self.thumb_mode()

    def sync_preset_label(self):
        cur = (self.var_bitrate.get().strip(), self.var_xpreset.get().strip())
        for label, br, pr in PRESETS:
            if (br, pr) == cur:
                self.var_preset.set(label)
                return
        self.var_preset.set(f"自定义 · {cur[0]} / {cur[1]}（在高级模式改的）")

    def on_preset(self, _e=None):
        for label, br, pr in PRESETS:
            if label == self.var_preset.get():
                self.var_bitrate.set(br)
                self.var_xpreset.set(pr)

    def thumb_mode(self):
        mode = self.var_thumb_mode.get()
        for key, w in self.thumb_rows.items():
            if key == mode:
                w.pack(fill="x", pady=(6, 0))
            else:
                w.pack_forget()
        self.thumb_cap()

    def thumb_cap(self):
        mode = self.var_thumb_mode.get()
        if mode == "file":
            txt = "用指定图片；工具会按 3 种尺寸缩放后编成相机规格 JPEG"
        else:
            secs = self.var_thumb_sec.get() if mode == "time" else 0.0
            txt = (f"取第 {int(secs) // 60:02d}:{secs % 60:04.1f} 帧 · "
                   "160×120（NCTH）/ 640×360（NCM1）/ 1920×1080（NCVW）"
                   if mode == "time" else
                   "取首帧 · 160×120（NCTH）/ 640×360（NCM1）/ 1920×1080（NCVW）")
        self.prev_cap.configure(text=txt)

    # ---------- 选文件 ----------
    def pick_src(self):
        p = filedialog.askopenfilename(
            title="选择要转换的视频",
            filetypes=[("视频", "*.mp4 *.mov *.mkv *.avi *.m4s *.flv *.webm *.ts"),
                       ("所有文件", "*.*")])
        if not p:
            return
        self.var_src.set(p)
        if not self.var_outdir.get().strip():
            self.var_outdir.set(str(Path(p).parent))
        self.probe_src()
        self.make_preview()

    def pick_outdir(self):
        p = filedialog.askdirectory(title="选择输出目录")
        if p:
            self.var_outdir.set(p)

    def pick_tpl(self):
        p = filedialog.askopenfilename(
            title="选择相机模板 MOV（同分辨率的相机原片）",
            filetypes=[("MOV", "*.MOV *.mov"), ("所有文件", "*.*")])
        if p:
            self.var_tpl.set(p)

    def pick_thumb_img(self):
        p = filedialog.askopenfilename(
            title="选择一张图片当缩略图",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.bmp *.webp"),
                       ("所有文件", "*.*")])
        if p:
            self.var_thumb_img.set(p)

    # ---------- 源信息 / 预览 ----------
    def probe_src(self):
        """跑一次 ffprobe 拿分辨率/帧率/时长/有无音频，填那一行"源 → 输出"。"""
        src = self.var_src.get().strip()

        def job():
            info = {}
            try:
                r = subprocess.run(
                    [ffbin("ffprobe"), "-v", "error", "-show_entries",
                     "format=duration:stream=codec_type,width,height,r_frame_rate",
                     "-of", "default=nw=1", src],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=60)
                for line in (r.stdout or "").splitlines():
                    k, _, v = line.partition("=")
                    info[k] = v
            except Exception:
                return
            self.root.after(0, self.show_srcinfo, info)

        threading.Thread(target=job, daemon=True).start()

    def show_srcinfo(self, i):
        if not i:
            return
        try:
            w, h = i.get("width"), i.get("height")
            fr = i.get("r_frame_rate", "")
            fps = ""
            if "/" in fr:
                n, d = (float(x) for x in fr.split("/"))
                if d:
                    fps = f"{n / d:.2f}fps"
            dur = float(i.get("duration") or 0) or None
            audio = "含音频" if i.get("codec_type") == "audio" or self.has_audio(i) else "无音频"
            est = ""
            if dur:
                f = 6 if self.var_xpreset.get() in ("fast", "faster", "veryfast") else 15
                est = f" · 预计转换约 {max(1, round(dur * f / 60))} 分钟（粗估）"
            self.var_srcinfo.set(
                f"源 {w}×{h} · {fps} · {fmt_secs(dur)} · {audio}   →   "
                f"输出 1920×1080 · 59.94fps · H.265(x265) / PCM 24bit{est}")
        except Exception:
            pass

    @staticmethod
    def has_audio(i):
        return i.get("codec_type") == "audio"

    def make_preview(self):
        """抽一帧给界面看（跟输出无关，纯粹是"我要的是不是这一帧"）。"""
        src = self.var_src.get().strip()
        if not src or not Path(src).exists():
            return
        secs = self.var_thumb_sec.get() if self.var_thumb_mode.get() == "time" else 0.0
        png = WORK / "preview.png"
        cmd = [ffbin("ffmpeg"), "-y", "-v", "error", "-i", src]
        if secs:
            cmd += ["-ss", f"{secs:g}"]
        cmd += ["-map", "0:v:0", "-vf",
                "scale=384:216:force_original_aspect_ratio=decrease,"
                "pad=384:216:(ow-iw)/2:(oh-ih)/2:color=black",
                "-frames:v", "1", "-f", "image2", str(png)]

        def job():
            try:
                subprocess.run(cmd, capture_output=True, timeout=120)
                self.root.after(0, self.show_preview, png)
            except Exception:
                self.root.after(0, lambda: self.prev_img.configure(text="预览不可用"))

        threading.Thread(target=job, daemon=True).start()

    def show_preview(self, png):
        try:
            from PIL import Image, ImageTk
            with Image.open(png) as im:
                self._photo = ImageTk.PhotoImage(im.resize((192, 108),
                                                           Image.LANCZOS))
            self.prev_img.configure(image=self._photo, text="")
        except Exception:
            self.prev_img.configure(text="预览不可用（打包时缺 PIL.ImageTk？）")

    # ---------- 日志 ----------
    def log_write(self, text, tag=None):
        self.log.configure(state="normal")
        if tag is None:
            tag = ("bad" if ("[!!]" in text or "[FAIL]" in text or "失败" in text
                             or "异常" in text) else
                   "ok" if ("[OK]" in text or "通过" in text) else
                   "warn" if ("⚠" in text or "警告" in text) else
                   "dim" if text.startswith("  [用时]") else "")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def log_clear(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def log_copy(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log.get("1.0", "end").strip())

    def log_save(self):
        p = filedialog.asksaveasfilename(defaultextension=".txt",
                                         initialfile="转换日志.txt",
                                         filetypes=[("文本", "*.txt")])
        if p:
            Path(p).write_text(self.log.get("1.0", "end"), encoding="utf-8")

    # ---------- IO 重定向：把工具的 print 变成日志 + 进度 ----------
    class _Sink(io.TextIOBase):
        def __init__(self, app):
            self.app = app
            self.buf = ""

        def write(self, s):
            if not s:
                return 0
            self.buf += s
            while "\n" in self.buf:
                line, self.buf = self.buf.split("\n", 1)
                self.app.root.after(0, self.app.on_line, line)
            return len(s)

        def flush(self):
            if self.buf:
                line, self.buf = self.buf, ""
                self.app.root.after(0, self.app.on_line, line)

    def on_line(self, line):
        """工具的一行输出：进度协议行进进度卡片，其余进日志。"""
        if line.startswith("##CAMMOV "):
            try:
                self.on_event(json.loads(line[len("##CAMMOV "):]))
            except Exception:
                pass
            return
        if line.strip():
            self.log_write(line)

    def on_event(self, ev):
        k = ev.get("k")
        if k == "prog":
            if ev.get("pct") is not None:
                self.enc_pct = ev["pct"]
            total, done, sp = ev.get("total"), ev.get("done"), ev.get("speed")
            self.bar.configure(value=self.overall())
            if self.enc_pct is not None:
                self.lbl_pct.configure(text=f"{self.enc_pct:.1f}%")
            self.stat_lbls["预计剩余"].configure(text=fmt_secs(ev.get("eta")))
            if sp:
                self.stat_lbls["编码速度"].configure(
                    text=f"{59.94 * sp:.1f} fps（{sp:.2f}×）")
            if total:
                self.stat_lbls["已处理 / 总时长"].configure(
                    text=f"{fmt_secs(done)} / {fmt_secs(total)}")
        elif k == "stage":
            self.cur = ev.get("step", 0)
            run = ev.get("running")
            total = ev.get("total", 6)
            if run:
                self.lbl_stage.configure(text=f"第 {self.cur + 1}/{total} 步 · {run}")
            self.bar.configure(value=self.overall())
        elif k == "gate":
            ok, bad = ev.get("ok", 0), ev.get("bad", 0)
            self.gate_ok = (bad == 0 and (ok + bad) > 0)
            self.log_write(f"[门禁] {ok}/{ok + bad} 项通过"
                           + ("" if self.gate_ok else f"，未通过：{ev.get('failed')}"))
            if self.gate_ok:
                self.lbl_status.configure(text=f"门禁通过 {ok}/{ok + bad}")
                self.lbl_stage.configure(text=f"门禁 {ok}/{ok + bad} 通过，可以拷到卡上")
                self.sync_status()
                self.set_state("done")
        elif k == "done":
            self.done_file = ev.get("out")
            if ev.get("size"):
                self.stat_lbls["输出大小"].configure(text=fmt_size(ev["size"]))
        elif k == "cancelled":
            self.log_write(f"[已取消] 编到第 {ev.get('at', '?')} 秒画面被停掉，"
                           "中间文件已清理。")

    def overall(self):
        """总进度 = 已完成阶段的权重 + 当前阶段内部进度（编码用真实百分比）。"""
        i = min(self.cur, len(STAGE_W) - 1)
        frac = (self.enc_pct or 0) / 100.0 if i == 1 else 0.0
        v = STAGE_CUM[i] + STAGE_W[i] * frac
        return 100.0 if self.done_file else v

    # ---------- 状态 ----------
    def set_state(self, st):
        self.state = st
        if st == "idle":
            self.badge.configure(text="空闲", bg=LINE2, fg=INK2)
            # 空闲时把进度条和 5 格统计收掉：底部固定区矮一截，普通模式的
            # 输入/参数两张卡在 150% 缩放的笔记本屏上也能一屏看全
            self.bar.pack_forget()
            self.stats.pack_forget()
        elif st == "run":
            self.badge.configure(text="转换中", bg=ACCENT_SOFT, fg=ACCENT)
            self.bar.pack(fill="x", pady=(8, 4))
            self.stats.pack(fill="x", pady=(4, 0))
            self.lbl_pct.configure(text="")
            self.bar.configure(value=0.0)
        elif st == "done":
            self.badge.configure(text="已完成", bg=OK_SOFT, fg=OK)
            self.bar.pack(fill="x", pady=(8, 4))
            self.stats.pack(fill="x", pady=(4, 0))
            self.lbl_pct.configure(text="100%")
            self.bar.configure(value=100.0)
            self.canvas.yview_moveto(1.0)     # 完成后停在日志尾部（43/43 那几行）
        busy = st == "run"
        self.btn_go.configure(state="disabled" if busy else "normal")
        self.btn_ver.configure(state="normal" if self.done_file else "disabled")
        self.btn_copy.configure(
            state="normal" if (self.gate_ok and self.done_file) else "disabled")
        if busy:
            self.btn_cancel.pack(side="left", padx=6)
            self.canvas.yview_moveto(1.0)     # 转换时把日志区滚进视野
        else:
            self.btn_cancel.pack_forget()

    # ---------- 运行工具 ----------
    def out_path(self):
        name = self.var_outname.get().strip() or "DSC_4600.MOV"
        if not name.upper().endswith(".MOV"):
            name += ".MOV"
        return Path(self.var_outdir.get().strip() or ".") / name

    def run_convert(self):
        if self.tools.get("conv") is None:
            return messagebox.showerror("错误", "转换模块没加载成功")
        src = self.var_src.get().strip()
        if not src or not Path(src).exists():
            return messagebox.showerror("错误", "请先选择存在的输入视频")
        tpl = self.var_tpl.get().strip()
        if not Path(tpl).exists():
            return messagebox.showerror("错误", f"模板不存在：{tpl}")
        out = self.out_path()
        if out.exists() and not messagebox.askyesno(
                "覆盖？", f"{out}\n已经存在，要覆盖它吗？"):
            return
        out.parent.mkdir(parents=True, exist_ok=True)
        argv = [src, "--out", str(out), "--template", tpl,
                "--bitrate", self.var_bitrate.get().strip() or "15M",
                "--preset", self.var_xpreset.get().strip() or "fast"]
        # 缩略图（默认首帧 = 什么都不传，也就是相机原生行为）
        mode = self.var_thumb_mode.get()
        if self.var_nothumbs.get():
            argv.append("--no-thumbs")
        elif mode == "time":
            argv += ["--thumb-at", f"{self.var_thumb_sec.get():g}"]
        elif mode == "file":
            img = self.var_thumb_img.get().strip()
            if not img or not Path(img).exists():
                return messagebox.showerror("错误", "选一张存在的图片当缩略图")
            argv += ["--thumb-image", img]
        # 高级模式的可选项；锁死的项（encoder/jobs/fps/size）一律不传，用工具默认值
        if self.var_mode.get() == "adv":
            if self.var_noaudio.get():
                argv.append("--no-audio")
            if self.var_keep.get():
                argv.append("--keep")
            if self.var_time.get().strip() == "keep":
                argv += ["--time", "keep"]
            st, du = self.var_start.get().strip(), self.var_dur.get().strip()
            if st:
                argv += ["--start", st]
            if du:
                argv += ["--duration", du]

        self.done_file = None
        self.gate_ok = False
        self.start_job()
        steps = [(self.tools["conv"].main, "① 转换：" + Path(src).name)]
        # 转换完自动跑门禁（门禁全过才允许拷卡）
        steps.append((self.tools["verify"].main,
                      "② 校验门禁：" + str(out)))
        self.launch(steps, argv=[argv, [str(out), "--template", tpl]],
                    scripts=["video-to-camera-mov.py", "verify-output.py"])

    def run_verify(self):
        if self.tools.get("verify") is None:
            return messagebox.showerror("错误", "校验模块没加载成功")
        out = self.done_file or self.out_path()
        if not Path(out).exists():
            return messagebox.showerror("错误", f"找不到 {out}")
        tpl = self.var_tpl.get().strip()
        self.gate_ok = False
        self.start_job()
        self.launch([(self.tools["verify"].main, "② 校验门禁：" + Path(out).name)],
                    argv=[[str(out), "--template", tpl]],
                    scripts=["verify-output.py"])

    def run_copy(self):
        out = self.done_file or self.out_path()
        if not Path(out).exists():
            return messagebox.showerror("错误", f"找不到 {out}")
        copy = load_tool("copy-to-card.py", "copycard")
        self.start_job()
        self.launch([(copy.main, "③ 拷到卡上：" + Path(out).name)],
                    argv=[[str(out)]], scripts=["copy-to-card.py"])

    def start_job(self):
        CANCEL_FLAG.unlink(missing_ok=True)
        self.cancel_asked = False
        self.busy = True
        self.cur = 0
        self.enc_pct = None
        self.t_start = time.time()
        self.lbl_stage.configure(text="准备中…")
        self.set_state("run")
        self.lbl_status.configure(text="转换中")
        self.bar.configure(value=0.0)
        self.root.after(1000, self.tick)
        # 窗口小的时候"进度/日志"在第一屏之外，直接滚下去
        self.body.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def tick(self):
        """每秒更新已用时间与输出大小（编码中的 .265 中间文件在长）。"""
        if not self.busy:
            return
        self.stat_lbls["已用时间"].configure(
            text=fmt_secs(time.time() - self.t_start))
        try:
            stem = self.out_path().stem
            f = WORK / f"enc-{stem}.265"
            if f.exists():
                self.stat_lbls["输出大小"].configure(text=fmt_size(f.stat().st_size))
        except Exception:
            pass
        self.root.after(1000, self.tick)

    def launch(self, steps, argv, scripts):
        """在一个守护线程里依次跑若干工具 main()，共享日志与进度。"""
        if not self.busy:
            return
        old_argv, old_out = sys.argv, sys.stdout
        sink = self._Sink(self)

        def job():
            code = 0
            try:
                for (fn, title), av, sc in zip(steps, argv, scripts):
                    self.root.after(0, self.log_write, "\n" + "=" * 68)
                    self.root.after(0, self.log_write, title)
                    self.root.after(0, self.log_write, "=" * 68)
                    sys.argv = [sc] + list(av)
                    sys.stdout = sink
                    try:
                        code = fn() or 0
                    except SystemExit as e:
                        c = e.code
                        if isinstance(c, str):
                            self.root.after(0, self.log_write, "[失败] " + c)
                            code = 1
                        else:
                            code = c or 0
                    if code:
                        break
            except Exception:
                self.root.after(0, self.log_write,
                                "[异常]\n" + traceback.format_exc())
                code = 1
            finally:
                sys.argv, sys.stdout = old_argv, old_out
                self.root.after(0, self.job_done, code)

        threading.Thread(target=job, daemon=True).start()

    def job_done(self, code):
        self.busy = False
        CANCEL_FLAG.unlink(missing_ok=True)
        # 取消只在"这一步真的失败了"时才算数：门禁阶段点取消不影响已经转好的文件
        if self.cancel_asked and code != 0:
            self.clean_work()            # 工具只在成功时自己清理中间文件
            self.log_write("[已取消] 本次操作已取消")
            self.lbl_status.configure(text="已取消")
            self.lbl_stage.configure(text="已取消 · 可以重新开始")
            self.set_state("idle")
            return
        if code == 0:
            self.log_write("[成功]")
            if self.done_file:
                self.stat_lbls["输出大小"].configure(
                    text=fmt_size(Path(self.done_file).stat().st_size))
        else:
            self.log_write(f"[失败] 退出码 {code}")
            self.lbl_status.configure(text="失败")
        if self.gate_ok:
            self.set_state("done")
        elif self.done_file:
            self.bar.configure(value=100.0)
            self.set_state("idle")       # 转换成功但门禁没过：不允许拷卡
        else:
            self.set_state("idle")
        self.sync_status()

    def cancel_job(self):
        if not self.busy:
            return
        self.cancel_asked = True
        CANCEL_FLAG.write_text("cancel", encoding="utf-8")
        self.log_write("[取消] 正在停 ffmpeg…")
        self.lbl_stage.configure(text="正在取消…")

    def clean_work(self):
        """收掉这一趟的中间文件。命名跟 tools/video-to-camera-mov.py 里一致
        （enc-/seg-/th- 加输出名），工具只在成功收尾时自己清理，取消时要界面兜底。"""
        tag = self.out_path().stem
        for pat in (f"enc-{tag}.265", f"enc-{tag}.pcm", f"seg-{tag}-*.265",
                    f"th-{tag}-*.png"):
            for f in WORK.glob(pat):
                try:
                    f.unlink()
                except OSError:
                    pass

    def open_out(self):
        d = Path(self.var_outdir.get().strip() or ".")
        if not d.exists():                # 还没转过任何东西时目录可能还不存在
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                messagebox.showwarning("打开输出目录", f"目录不存在，也建不出来：\n{d}\n\n{e}")
                return
        os.startfile(d)          # noqa: S606


def selftest(report=None):
    """无人工介入的界面自检：能不能建起来、有没有异常。
    --windowed 打包后没有控制台，所以结果同时落到 --report 指定的文件。"""
    headless_modals()
    root = tk.Tk()
    app = App(root)
    n = len(root.winfo_children())

    def fin():
        text = f"UI OK：顶层子控件 {n} 个、{len(app.stat_lbls)} 格统计"
        if report:
            Path(report).write_text(text + "\n", encoding="utf-8")
        print(text)
        root.destroy()

    root.after(1200, fin)
    root.mainloop()


def headless_modals():
    """冒烟测试里没人点按钮，也不能被模态框挡住：换成记录。"""
    def rec(*a, **_k):
        NOTES.append("[弹窗] " + " ".join(str(x) for x in a))

    for _n in ("showerror", "showwarning", "showinfo"):
        setattr(messagebox, _n, rec)
    messagebox.askyesno = lambda *a, **_k: True


NOTES = []
AUTO_TIMEOUT = 3600          # 秒；纯保险，正常几十秒就完


def auto_smoke():
    """冻结后的端到端冒烟测试：没人点按钮也把「① 转换 + ② 门禁」跑完，
    结果写进 --report 文件（--windowed 的 exe 没有 stdout 可看）。

        app.exe --auto <源视频> --outdir <目录> --outname DSC_4700.MOV
                --report <结果文件>
    """
    a = sys.argv
    src = a[a.index("--auto") + 1]
    outdir = a[a.index("--outdir") + 1] if "--outdir" in a else str(Path(src).parent)
    name = a[a.index("--outname") + 1] if "--outname" in a else "DSC_4700.MOV"
    report = Path(a[a.index("--report") + 1]) if "--report" in a else WORK / "auto-report.txt"

    headless_modals()
    t0 = time.time()
    root = tk.Tk()
    app = App(root)
    app.var_src.set(str(Path(src).resolve()))
    app.var_outdir.set(outdir)
    app.var_outname.set(name)
    app.probe_src()
    state = {"ok": False, "why": "超时"}

    def write_report(success):
        out = app.done_file or str(app.out_path())
        try:                       # 拷卡模块是懒加载的，顺手确认打包后也能加载
            load_tool("copy-to-card.py", "copycard")
            copy_ok = "能加载"
        except Exception as e:
            copy_ok = f"加载失败：{e}"
        lines = [
            f"结果       : {'成功' if success else '失败'}",
            f"用时       : {time.time() - t0:.0f}s",
            f"输入       : {app.var_src.get()}",
            f"输出       : {out}",
            f"输出存在   : {Path(out).exists()}"
            + (f"（{Path(out).stat().st_size:,} B）" if Path(out).exists() else ""),
            f"界面状态   : {app.state}   门禁通过: {app.gate_ok}",
            f"拷卡模块   : {copy_ok}",
            f"按钮       : ①{app.btn_go.cget('state')} ②{app.btn_ver.cget('state')}"
            f" ③{app.btn_copy.cget('state')}",
            "统计       : " + str({k: v.cget("text") for k, v in app.stat_lbls.items()}),
            "阶段行     : " + app.lbl_stage.cget("text"),
            "左下状态   : " + app.lbl_status.cget("text"),
            "右下状态   : " + app.lbl_tpl.cget("text"),
            "弹窗       : " + ("；".join(NOTES) if NOTES else "无"),
            "--- 日志（末尾 20 行）---",
        ]
        lines += app.log.get("1.0", "end").strip().splitlines()[-20:]
        try:
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass
        print("\n".join(lines))

    def go():
        state["why"] = "转换已启动"
        app.run_convert()
        state["why"] = "转换中"

    def watch():
        if app.busy:
            if time.time() - t0 > AUTO_TIMEOUT:
                write_report(False)
                root.destroy()
                return
            root.after(500, watch)
            return
        state["ok"] = bool(app.gate_ok and app.done_file)
        state["why"] = "结束"
        write_report(state["ok"])
        root.destroy()

    root.after(400, go)
    root.after(1500, watch)
    root.mainloop()
    sys.exit(0 if state["ok"] else 1)


def main():
    root = tk.Tk()
    app = App(root)
    # 拖到 exe / 脚本上的文件（argv[1]）自动填入
    if len(sys.argv) > 1 and Path(sys.argv[1]).exists():
        app.var_src.set(str(Path(sys.argv[1]).resolve()))
        app.probe_src()
        app.make_preview()
    root.mainloop()


if __name__ == "__main__":
    if "--auto" in sys.argv:
        auto_smoke()
    elif "--selftest" in sys.argv:
        selftest(sys.argv[sys.argv.index("--report") + 1] if "--report" in sys.argv else None)
    else:
        main()
