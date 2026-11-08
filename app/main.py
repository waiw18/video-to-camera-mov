#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""相机视频转换器：把普通视频批量转成 Nikon Z5II 能直接播放的 MOV。

    python app/main.py                 # 界面
    python app/main.py --selftest --report r.txt
    python app/main.py --auto <源> --outdir <目录> --outname DSC_4700.MOV --report r.txt
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
from tkinter import filedialog, messagebox, ttk

import tkinter as tk

FROZEN = getattr(sys, "frozen", False)


def res_dir():
    if FROZEN:
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


RES = res_dir()
TOOLS = RES / "tools"
WORK = Path(sys.executable).parent / ".camwork" if FROZEN else RES / ".probe"


def default_outdir():
    if not FROZEN:
        return RES / "output"
    base = Path.home() / "Videos"
    if not base.is_dir():
        base = Path.home()
    return base / "相机转换输出"


APP_VERSION = "v0.1.0-beta.3"
MAXW = 1010
CANCEL_FLAG = WORK / "cancel.flag"

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
DANGER = "#b42318"
LOG_BG = "#1b1f24"
LOG_INK = "#d7dee6"
LOG_DIM = "#8b959f"
LOG_OK = "#7fd18b"
LOG_BAD = "#ff9a90"
LOG_WARN = "#ffd479"

F_BODY = ("Microsoft YaHei UI", 10)
F_TITLE = ("Microsoft YaHei UI", 10, "bold")
F_SMALL = ("Microsoft YaHei UI", 9)
F_BIG = ("Microsoft YaHei UI", 13, "bold")
F_MONO = ("Consolas", 9)

QUALITY = [
    ("小体积 · 5 Mbps", "5M", "fast", "体积约四成，编码快一倍"),
    ("推荐 · 15 Mbps", "15M", "fast", "与相机原生同档"),
    ("高画质 · 40 Mbps", "40M", "medium", "更清楚，也更慢更大"),
]
BUNDLED_TPLS = {"59.94": "DSC_8955.MOV", "29.97": "DSC_8960.MOV"}
AUDIO_PAIRS = (("-30064", "-30232"), ("-30033", "-30216"))
VIDEO_EXT = "*.mp4 *.mov *.mkv *.avi *.ts *.flv *.webm *.m4s"


def prep_env():
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
    v = os.environ.get("CAMMOV_" + name.upper())
    if v and Path(v).exists():
        return v
    return name


def fmt_secs(s):
    if s is None:
        return "--:--"
    s = max(0, int(round(float(s))))
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def probe_info(src):
    info = {}
    try:
        r = subprocess.run(
            [ffbin("ffprobe"), "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(src)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)
        j = json.loads(r.stdout or "{}")
    except Exception:
        return {}
    streams = j.get("streams") or []
    vid = next((s for s in streams if s.get("codec_type") == "video"), {})
    for k in ("width", "height", "r_frame_rate"):
        if vid.get(k) is not None:
            info[k] = str(vid[k])
    info["duration"] = str((j.get("format") or {}).get("duration") or "")
    info["codec_type"] = "video" if vid else ""
    info["has_audio"] = "1" if any(s.get("codec_type") == "audio"
                                   for s in streams) else ""
    return info


def src_fps(info):
    try:
        n, d = (float(x) for x in (info.get("r_frame_rate") or "").split("/"))
        return n / d if d else 0.0
    except Exception:
        return 0.0


def grab_frame(src, secs, png, width=160):
    cmd = [ffbin("ffmpeg"), "-y", "-v", "error"]
    if secs:
        cmd += ["-ss", f"{secs:g}"]
    cmd += ["-i", str(src), "-map", "0:v:0", "-frames:v", "1",
            "-vf", f"scale={width}:-2", "-f", "image2", str(png)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception:
        return None
    return png if Path(png).exists() else None


def frame_std(png):
    try:
        from PIL import Image
        import numpy as np
        with Image.open(png) as im:
            a = np.asarray(im.convert("L"), dtype="float32")
        return float(a.std())
    except Exception:
        return 0.0


def auto_thumb(src):
    """首帧如果是纯色（白场/黑场），退到第一个有内容的帧。"""
    for i, t in enumerate((0, 1, 2, 3, 5, 8, 12)):
        png = WORK / f"th-auto-{i}.png"
        if grab_frame(src, t, png) and frame_std(png) > 12:
            return png if t else None
    return None


class App:
    def __init__(self, root):
        self.root = root
        root.title("相机视频转换器")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(MAXW + 40, sw - 80), min(900, sh - 56)
        root.geometry(f"{max(w, 800)}x{max(h, 620)}+{max(0, (sw - w) // 2)}+20")
        root.minsize(800, 620)
        root.configure(bg=BG)

        self.busy = False
        self.cancel_asked = False
        self.state = "idle"
        self.items = []
        self.qi = 0
        self.cur_item = None
        self.enc_pct = None
        self.t_start = 0.0
        self.gate_ok = False
        self.done_file = None
        self.used = {}
        self.seq = 0
        self.mode = "pick"

        prep_env()
        self.tools = {}
        try:
            self.tools["conv"] = load_tool("video-to-camera-mov.py", "convmod")
            self.tools["verify"] = load_tool("verify-output.py", "verifymod")
            self.tools["split"] = load_tool("split-m4s.py", "splitmod")
        except Exception:
            messagebox.showerror("加载失败", traceback.format_exc())

        self.styles()
        self.vars()
        self.build_ui()
        self.set_state("idle")

    def vars(self):
        v = tk.StringVar
        self.var_outdir = v(value=str(default_outdir()))
        self.var_quality = tk.IntVar(value=1)
        self.var_verify = tk.BooleanVar(value=True)
        self.var_more = tk.BooleanVar(value=False)
        self.var_log = tk.BooleanVar(value=False)
        self.var_tpl = v(value="自动")
        self.var_thumb = v(value="自动")
        self.var_pieces = tk.IntVar(value=1)
        self.tpl_path = ""
        self.thumb_path = ""

    def styles(self):
        S = ttk.Style()
        try:
            S.theme_use("clam")
        except tk.TclError:
            pass
        S.configure("TEntry", fieldbackground=CARD, foreground=INK, bordercolor=LINE,
                    lightcolor=LINE, darkcolor=LINE, insertcolor=INK, padding=4)
        S.map("TEntry", bordercolor=[("focus", ACCENT)])
        S.configure("TCombobox", fieldbackground=CARD, background=CARD,
                    bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
                    arrowcolor=INK2, padding=4, selectbackground=CARD,
                    selectforeground=INK)
        S.map("TCombobox", bordercolor=[("focus", ACCENT)],
              fieldbackground=[("readonly", CARD)])
        S.configure("TButton", background="#f4f6f8", foreground=INK, bordercolor=LINE,
                    lightcolor="#f4f6f8", darkcolor="#f4f6f8", relief="flat",
                    padding=(12, 6), focusthickness=0, font=F_BODY, width=0)
        S.map("TButton", background=[("active", "#e9edf1"), ("disabled", "#f2f4f6")],
              foreground=[("disabled", INK3)])
        S.configure("Primary.TButton", background=ACCENT, foreground="#ffffff",
                    bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
                    padding=(20, 9), font=F_TITLE)
        S.map("Primary.TButton",
              background=[("active", ACCENT_D), ("disabled", "#a9c2e6")],
              foreground=[("disabled", "#eef3fb")])
        S.configure("Cam.Horizontal.TProgressbar", troughcolor=LINE2,
                    background=ACCENT, bordercolor=LINE2, lightcolor=ACCENT,
                    darkcolor=ACCENT, thickness=8)
        S.configure("Cam.Treeview", font=F_BODY, rowheight=28, background=CARD,
                    fieldbackground=CARD, foreground=INK, borderwidth=0)
        S.configure("Cam.Treeview.Heading", font=F_SMALL, foreground=INK3,
                    background=CARD, relief="flat")
        S.map("Cam.Treeview", background=[("selected", ACCENT_SOFT)],
              foreground=[("selected", INK)])

    # ---------- 界面 ----------
    def build_ui(self):
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=20, pady=(14, 8))
        tk.Label(head, text="相机视频转换器", bg=BG, fg=INK, font=F_BIG).pack(side="left")
        tk.Label(head, text="Nikon Z5II · 1080p / H.265 / PCM", bg=BG, fg=INK3,
                 font=F_SMALL).pack(side="left", padx=(10, 0), pady=(6, 0))
        tk.Label(head, text=APP_VERSION, bg=BG, fg=INK3, font=F_SMALL).pack(
            side="right", pady=(6, 0))

        self.bottom = tk.Frame(self.root, bg=BG)
        self.bottom.pack(side="bottom", fill="x", padx=20, pady=(8, 12))
        self.log_head = tk.Frame(self.root, bg=BG)
        self.log_head.pack(side="bottom", fill="x", padx=20)
        self.card_log()
        self.card_source()
        self.card_quality()
        self.card_bottom()
        self.more_panel = tk.Frame(self.root, bg=CARD, highlightthickness=1,
                                   highlightbackground=LINE)

    def card_source(self):
        outer = self.card("来源")
        outer.pack(fill="both", expand=True, padx=20, pady=(0, 0))
        body = tk.Frame(outer, bg=CARD)
        body.pack(fill="both", expand=True, padx=16, pady=(2, 12))

        tabs = tk.Frame(body, bg=CARD)
        tabs.pack(fill="x", pady=(2, 8))
        self.tab_btns = {}
        for key, text in (("pick", "选择视频"), ("merge", "合并视频")):
            b = tk.Button(tabs, text=text, relief="flat", bd=0, font=F_TITLE,
                          padx=14, pady=4, cursor="hand2",
                          command=lambda k=key: self.set_mode(k))
            b.pack(side="left", padx=(0, 6))
            self.tab_btns[key] = b

        tv = ttk.Treeview(body, columns=("src", "out", "st"), show="headings",
                          height=5, selectmode="extended", style="Cam.Treeview")
        tv.heading("src", text="输入", anchor="w")
        tv.heading("out", text="输出", anchor="w")
        tv.heading("st", text="状态", anchor="w")
        tv.column("src", width=430, anchor="w")
        tv.column("out", width=160, anchor="w")
        tv.column("st", width=150, anchor="w")
        tv.pack(fill="both", expand=True)
        tv.tag_configure("ok", foreground=OK)
        tv.tag_configure("run", foreground=ACCENT)
        tv.tag_configure("bad", foreground=DANGER)
        self.tv = tv

        bar = tk.Frame(body, bg=CARD)
        bar.pack(fill="x", pady=(8, 2))
        self.btn_add = tk.Button(bar, text="添加文件…", command=self.add_files,
                                 bg=LINE2, fg=INK, relief="flat", bd=0, font=F_SMALL,
                                 padx=12, pady=5, cursor="hand2")
        self.btn_add.pack(side="left")
        for text, cb in (("移除", self.remove_sel), ("清空", self.clear_all)):
            tk.Button(bar, text=text, command=cb, bg=CARD, fg=INK2, relief="flat",
                      bd=0, font=F_SMALL, cursor="hand2").pack(side="left", padx=(10, 0))
        self.lbl_count = tk.Label(bar, text="0 个", bg=CARD, fg=INK3, font=F_SMALL)
        self.lbl_count.pack(side="left", padx=(12, 0))

        line = tk.Frame(body, bg=CARD)
        line.pack(fill="x", pady=(4, 0))
        tk.Label(line, text="保存到", bg=CARD, fg=INK3, font=F_SMALL).pack(side="left")
        self.lbl_out = tk.Label(line, textvariable=self.var_outdir, bg=CARD, fg=INK2,
                                font=F_SMALL, anchor="w")
        self.lbl_out.pack(side="left", fill="x", expand=True, padx=(8, 0))
        tk.Button(line, text="更改", command=self.pick_outdir, bg=CARD, fg=ACCENT,
                  relief="flat", bd=0, font=F_SMALL, cursor="hand2").pack(side="left")

    def card_quality(self):
        outer = self.card("画质")
        outer.pack(fill="x", padx=20, pady=(10, 0))
        body = tk.Frame(outer, bg=CARD)
        body.pack(fill="x", padx=16, pady=(2, 12))

        self.lb = tk.Listbox(body, height=3, activestyle="none", bd=0,
                             highlightthickness=1, highlightbackground=LINE,
                             selectbackground=ACCENT_SOFT, selectforeground=INK,
                             bg=CARD, fg=INK, font=F_BODY, exportselection=False)
        for name, _r, _p, tip in QUALITY:
            self.lb.insert("end", f"  {name}    {tip}")
        self.lb.pack(fill="x")
        self.lb.selection_set(1)
        self.lb.bind("<<ListboxSelect>>", self.on_quality)

        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x", pady=(8, 0))
        tk.Checkbutton(row, text="转换完自动校验（43 项门禁）",
                       variable=self.var_verify, bg=CARD, fg=INK2, font=F_SMALL,
                       activebackground=CARD, selectcolor=CARD,
                       highlightthickness=0, cursor="hand2").pack(side="left")
        self.btn_more = tk.Button(row, text="更多选项 ▾", command=self.toggle_more,
                                  bg=CARD, fg=ACCENT, relief="flat", bd=0,
                                  font=F_SMALL, cursor="hand2")
        self.btn_more.pack(side="right")

    def card_log(self):
        self.btn_log = tk.Button(self.log_head, text="日志 ▾", command=self.toggle_log,
                                 bg=BG, fg=INK3, relief="flat", bd=0, font=F_SMALL,
                                 cursor="hand2", anchor="w")
        self.btn_log.pack(side="left", pady=(0, 2))
        self.log_box = tk.Frame(self.root, bg=LOG_BG)
        self.log = tk.Text(self.log_box, height=8, bg=LOG_BG, fg=LOG_INK, bd=0,
                           font=F_MONO, wrap="none", padx=10, pady=8,
                           state="disabled")
        self.log.pack(fill="both", expand=True)
        for tag, fg in (("ok", LOG_OK), ("bad", LOG_BAD), ("warn", LOG_WARN),
                        ("dim", LOG_DIM)):
            self.log.tag_configure(tag, foreground=fg)

    def card_bottom(self):
        self.bar = ttk.Progressbar(self.bottom, mode="determinate", maximum=100,
                                   style="Cam.Horizontal.TProgressbar")
        self.bar.pack(fill="x", pady=(0, 8))
        row = tk.Frame(self.bottom, bg=BG)
        row.pack(fill="x")
        self.lbl_state = tk.Label(row, text="", bg=BG, fg=INK2, font=F_BODY)
        self.lbl_state.pack(side="left")
        self.f_post = tk.Frame(row, bg=BG)
        self.btn_run = ttk.Button(row, text="开始转换", style="Primary.TButton",
                                  command=self.toggle_run)
        self.btn_run.pack(side="right")
        self.btn_cancel = tk.Button(row, text="取消", command=self.cancel_job,
                                    bg=DANGER, fg="white", activebackground=DANGER,
                                    activeforeground="white", relief="flat", bd=0,
                                    font=F_TITLE, padx=16, pady=7, cursor="hand2")

    def card(self, title):
        outer = tk.Frame(self.root, bg=CARD, highlightbackground=LINE,
                         highlightthickness=1)
        head = tk.Frame(outer, bg=CARD)
        head.pack(fill="x", padx=16, pady=(10, 2))
        tk.Label(head, text=title, bg=CARD, fg=INK3, font=F_SMALL).pack(side="left")
        return outer

    # ---------- 更多选项 ----------
    def toggle_more(self):
        if self.var_more.get():
            self.more_panel.pack_forget()
            self.var_more.set(False)
            self.btn_more.configure(text="更多选项 ▾")
            return
        self.var_more.set(True)
        self.btn_more.configure(text="更多选项 ▴")
        for w in self.more_panel.winfo_children():
            w.destroy()
        body = tk.Frame(self.more_panel, bg=CARD)
        body.pack(fill="x", padx=16, pady=12)

        def row(label, factory):
            r = tk.Frame(body, bg=CARD)
            r.pack(fill="x", pady=(0, 8))
            tk.Label(r, text=label, bg=CARD, fg=INK3, font=F_SMALL, width=6,
                     anchor="e").pack(side="left")
            w = factory(r)
            w.pack(side="left", padx=(8, 0))
            return w

        self.cb_tpl = row("模板", lambda f: ttk.Combobox(
            f, textvariable=self.var_tpl, state="readonly", width=30,
            values=["自动"] + [f"随包 {v}" for v in BUNDLED_TPLS.values()]
            + ["选择文件…"]))
        self.cb_tpl.bind("<<ComboboxSelected>>", self.on_tpl_choice)
        self.cb_thumb = row("封面", lambda f: ttk.Combobox(
            f, textvariable=self.var_thumb, state="readonly", width=30,
            values=["自动", "首帧", "末尾一帧", "选择图片…"]))
        self.cb_thumb.bind("<<ComboboxSelected>>", self.on_thumb_choice)
        row("分段", lambda f: ttk.Spinbox(f, from_=1, to=50, width=5,
                                          textvariable=self.var_pieces))
        tk.Label(body, text="分段 = 长片切成几段再转（切点对齐关键帧），1 = 不切",
                 bg=CARD, fg=INK3, font=F_SMALL).pack(anchor="w")
        self.more_panel.pack(fill="x", padx=20, pady=(10, 0), before=self.log_head)

    def on_tpl_choice(self, _e=None):
        if self.var_tpl.get() == "选择文件…":
            p = filedialog.askopenfilename(
                title="选择相机模板 MOV（同分辨率的相机原片）",
                filetypes=[("MOV", "*.MOV *.mov"), ("所有文件", "*.*")])
            if p:
                self.tpl_path = p
                self.var_tpl.set(f"自定义 {Path(p).name}")
            else:
                self.var_tpl.set("自动")

    def on_thumb_choice(self, _e=None):
        if self.var_thumb.get() == "选择图片…":
            p = filedialog.askopenfilename(
                title="选择图片当封面",
                filetypes=[("图片", "*.jpg *.jpeg *.png *.bmp *.webp")])
            if p:
                self.thumb_path = p
                self.var_thumb.set(f"自定义 {Path(p).name}")
            else:
                self.var_thumb.set("自动")

    def toggle_log(self):
        if self.var_log.get():
            self.log_box.pack_forget()
            self.var_log.set(False)
            self.btn_log.configure(text="日志 ▾")
        else:
            self.var_log.set(True)
            self.btn_log.configure(text="日志 ▴")
            self.log_box.pack(side="bottom", fill="both", expand=True,
                              padx=20, pady=(0, 8))

    # ---------- 队列 ----------
    def set_mode(self, key):
        if self.busy:
            return
        self.mode = key
        for k, b in self.tab_btns.items():
            on = k == key
            b.configure(bg=ACCENT_SOFT if on else CARD,
                        fg=ACCENT if on else INK3,
                        activebackground=ACCENT_SOFT)
        self.btn_add.configure(text="添加视频…" if key == "pick" else "添加分片…")

    def next_name(self):
        d = self.out_dir()
        while True:
            self.seq += 1
            name = f"DSC_{self.seq:04d}.MOV"
            if not (d / name).exists():
                return name

    def out_dir(self):
        return Path(self.var_outdir.get().strip() or ".")

    def scan_seq(self):
        d = self.out_dir()
        best = 0
        if d.is_dir():
            for f in d.glob("DSC_*.MOV"):
                try:
                    best = max(best, int(f.stem.split("_")[1]))
                except Exception:
                    pass
        self.seq = best

    def add_paths(self, paths, audio=None):
        if not paths:
            return
        if not self.seq:
            self.scan_seq()
        for p in paths:
            p = Path(p)
            item = {"src": p, "audio": audio, "out": self.next_name(),
                    "iid": None, "status": "排队", "kind": "video"}
            self.items.append(item)
            item["iid"] = self.tv.insert(
                "", "end", values=(p.name, item["out"], "排队"), tags=("",))
        self.refresh_list()
        self.log_write(f"加入 {len(paths)} 个文件")

    def add_files(self):
        if self.mode == "pick":
            paths = filedialog.askopenfilenames(
                title="选择视频", filetypes=[("视频", VIDEO_EXT),
                                            ("所有文件", "*.*")])
            self.add_paths(paths)
        else:
            p = filedialog.askopenfilename(
                title="选择视频分片（音频会自动配对）",
                filetypes=[("分片", "*.m4s"), ("所有文件", "*.*")])
            if not p:
                return
            p = Path(p)
            audio = None
            for a, b in AUDIO_PAIRS:
                if a in p.name:
                    cand = p.with_name(p.name.replace(a, b))
                    if cand.exists():
                        audio = cand
                    break
            if audio:
                self.log_write(f"配对音频：{audio.name}")
            else:
                self.log_write("没找到配对音频，按单个文件处理")
            self.add_paths([p], audio=audio)

    def remove_sel(self):
        if self.busy:
            return
        for iid in self.tv.selection():
            self.items = [it for it in self.items if it["iid"] != iid]
            self.tv.delete(iid)
        self.refresh_list()

    def clear_all(self):
        if self.busy:
            return
        for iid in self.tv.get_children():
            self.tv.delete(iid)
        self.items = []
        self.seq = 0
        self.refresh_list()

    def refresh_list(self):
        n = len(self.items)
        self.lbl_count.configure(text=f"{n} 个")
        if not self.busy:
            self.btn_run.configure(state="normal" if n else "disabled")
            self.lbl_state.configure(
                text=f"{n} 个待转换" if n else "添加视频后开始",
                fg=INK2 if n else INK3)

    def pick_outdir(self):
        p = filedialog.askdirectory(title="选择输出目录")
        if p:
            self.var_outdir.set(p)
            self.seq = 0

    def on_quality(self, _e=None):
        sel = self.lb.curselection()
        if sel:
            self.var_quality.set(sel[0])

    # ---------- 跑 ----------
    def quality(self):
        return QUALITY[self.var_quality.get()]

    def toggle_run(self):
        self.cancel_job() if self.busy else self.run_queue()

    def run_queue(self):
        if not self.items:
            return
        if self.tools.get("conv") is None:
            return messagebox.showerror("错误", "转换模块没加载成功")
        self.queue = list(self.items)
        self.qi = 0
        self.fail = 0
        self.t_start = time.time()
        self.busy = True
        self.cancel_asked = False
        self.bar.configure(value=0)
        CANCEL_FLAG.unlink(missing_ok=True)
        self.btn_run.configure(state="disabled")
        self.btn_cancel.pack(side="right", padx=(0, 8))
        self.f_post.pack_forget()
        self.set_state("run")
        self.log_write(f"开始：{len(self.queue)} 个 · {self.quality()[0]}")
        self.next_item()

    def next_item(self):
        if self.cancel_asked or self.qi >= len(self.queue):
            return self.finish_queue()
        self.cur_item = self.queue[self.qi]
        item = self.cur_item
        self.enc_pct = None
        self.gate_ok = False
        self.done_file = None
        item["status"] = "转换中"
        self.tv.item(item["iid"], tags=("run",))
        self.tv.set(item["iid"], "st", "准备中")
        self.tv.see(item["iid"])
        self.lbl_state.configure(
            text=f"{self.qi + 1}/{len(self.queue)} · {item['src'].name}", fg=INK2)
        threading.Thread(target=self.prepare_item, args=(item,),
                         daemon=True).start()

    def prepare_item(self, item):
        """合并（如果需要）→ 定模板/帧率/封面 → 起转换线程。"""
        try:
            src = item["src"]
            if item.get("audio"):
                merged = src.with_name(src.stem.split("-")[0] + "_完整.mp4")
                if not merged.exists():
                    self.root.after(0, self.log_write, f"合并音视频 → {merged.name}")
                    cmd = [ffbin("ffmpeg"), "-y", "-v", "error", "-i", str(src),
                           "-i", str(item["audio"]), "-map", "0:v:0", "-map", "1:a:0",
                           "-c", "copy", "-movflags", "+faststart", str(merged)]
                    r = subprocess.run(cmd, capture_output=True, timeout=3600)
                    if r.returncode or not merged.exists():
                        raise RuntimeError("合并失败：" + r.stderr.decode(
                            "utf-8", "replace")[-300:])
                src = merged
            info = probe_info(src)
            fps = src_fps(info)
            follow = 29.5 <= fps <= 30.5
            tpl = self.pick_tpl(follow)
            thumb = self.pick_thumb(src)
            argv = self.build_argv(src, item["out"], tpl, follow, thumb)
            self.root.after(0, self.start_item, item, argv)
        except Exception:
            msg = traceback.format_exc()
            self.root.after(0, self.log_write, "[异常]\n" + msg)
            self.root.after(0, self.item_done, item, 1)

    def pick_tpl(self, follow):
        want = self.var_tpl.get()
        if want.startswith("自定义") and self.tpl_path:
            return self.tpl_path
        if want.startswith("随包"):
            for name in BUNDLED_TPLS.values():
                if name in want:
                    return str(RES / "templates" / name)
        key = "29.97" if follow else "59.94"
        return str(RES / "templates" / BUNDLED_TPLS[key])

    def pick_thumb(self, src):
        mode = self.var_thumb.get()
        if mode.startswith("自定义") and self.thumb_path:
            return self.thumb_path
        if mode == "自动":
            return str(auto_thumb(src) or "")
        if mode == "末尾一帧":
            info = probe_info(src)
            dur = float(info.get("duration") or 0)
            png = WORK / "th-last.png"
            return str(grab_frame(src, max(0.0, dur - 1.0), png, 640) or "")
        return ""

    def build_argv(self, src, outname, tpl, follow, thumb):
        _name, br, xp, _tip = self.quality()
        out = (self.out_dir() / outname).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        argv = [str(src), "--out", str(out), "--template", tpl,
                "--bitrate", br, "--preset", xp]
        if thumb:
            argv += ["--thumb-image", thumb]
        if follow:
            argv += ["--fps", "source"]
        argv += ["--jobs", "0"]
        self.used = {"bitrate": br, "preset": xp,
                     "fps": "source" if follow else "59.94",
                     "tpl": Path(tpl).name, "argv": " ".join(argv)}
        return argv

    def start_item(self, item, argv):
        if self.cancel_asked:
            return self.finish_queue()
        out = (self.out_dir() / item["out"]).resolve()
        tpl = argv[argv.index("--template") + 1]
        self.tv.set(item["iid"], "st", "转换中")
        self.done_file = None
        steps = [(self.tools["conv"].main, "① 转换：" + item["src"].name)]
        avs = [argv]
        scripts = ["video-to-camera-mov.py"]
        if self.var_verify.get():
            steps.append((self.tools["verify"].main, "② 校验门禁：" + item["out"]))
            avs.append([str(out), "--template", tpl])
            scripts.append("verify-output.py")
        self.launch(steps, argv=avs, scripts=scripts,
                    done=lambda code, it=item: self.item_done(it, code))

    def item_done(self, item, code):
        CANCEL_FLAG.unlink(missing_ok=True)
        ok = (code == 0) and not self.cancel_asked
        if self.cancel_asked:
            item["status"] = "已取消"
            self.tv.set(item["iid"], "st", "已取消")
            self.tv.item(item["iid"], tags=("",))
            self.clean_work(item)
            return self.finish_queue(cancelled=True)
        if ok and (self.gate_ok or not self.var_verify.get()):
            item["status"] = "完成" + ("·43/43" if self.gate_ok else "")
            self.tv.set(item["iid"], "st",
                        "完成 · 43/43" if self.gate_ok else "完成")
            self.tv.item(item["iid"], tags=("ok",))
        else:
            self.fail += 1
            item["status"] = "失败"
            self.tv.set(item["iid"], "st", "失败")
            self.tv.item(item["iid"], tags=("bad",))
        self.qi += 1
        self.bar.configure(value=self.qi / len(self.queue) * 100)
        self.next_item()

    def finish_queue(self, cancelled=False):
        self.busy = False
        self.btn_cancel.pack_forget()
        self.btn_run.configure(state="normal")
        if cancelled:
            self.lbl_state.configure(text="已取消", fg=WARN)
            self.set_state("idle")
            return
        n = len(self.queue)
        good = n - self.fail
        if self.fail == 0:
            self.lbl_state.configure(
                text=f"全部完成 · {good}/{n}" + ("，门禁全过" if self.var_verify.get()
                                                else ""), fg=OK)
            self.bar.configure(value=100)
            self.set_state("done")
            self.show_post()
        else:
            self.lbl_state.configure(text=f"完成 {good}/{n}，失败 {self.fail}",
                                     fg=DANGER)
            self.set_state("idle")
            self.show_post()

    def show_post(self):
        for w in self.f_post.winfo_children():
            w.destroy()
        for text, cb in (("打开文件夹", self.open_out), ("拷到相机卡", self.copy_last)):
            tk.Button(self.f_post, text=text, command=cb, bg=BG, fg=ACCENT if
                      text == "打开文件夹" else INK2, relief="flat", bd=0,
                      font=F_SMALL, cursor="hand2").pack(side="left", padx=(0, 14))
        self.f_post.pack(side="right", padx=(0, 14))

    # ---------- 工具调用 ----------
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

    def launch(self, steps, argv, scripts, done):
        old_argv, old_out = sys.argv, sys.stdout
        sink = self._Sink(self)

        def job():
            code = 0
            try:
                for (fn, title), av, sc in zip(steps, argv, scripts):
                    self.root.after(0, self.log_write, "\n" + "=" * 64)
                    self.root.after(0, self.log_write, title)
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
                self.root.after(0, self.log_write, "[异常]\n" + traceback.format_exc())
                code = 1
            finally:
                sys.argv, sys.stdout = old_argv, old_out
                self.root.after(0, done, code)

        threading.Thread(target=job, daemon=True).start()

    def on_line(self, line):
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
            self.bar.configure(value=self.overall())
            if self.cur_item:
                self.tv.set(self.cur_item["iid"], "st",
                            f"转换 {self.enc_pct or 0:.0f}%")
            sp = ev.get("speed")
            if sp and self.cur_item:
                out_fps = 29.97 if self.used.get("fps") == "source" else 59.94
                self.lbl_state.configure(
                    text=f"{self.qi + 1}/{len(self.queue)} · "
                         f"{out_fps * sp:.0f} fps（{sp:.2f}×） · 剩 "
                         f"{fmt_secs(ev.get('eta'))}", fg=INK2)
        elif k == "stage":
            self.bar.configure(value=self.overall())
        elif k == "gate":
            ok, bad = ev.get("ok", 0), ev.get("bad", 0)
            self.gate_ok = (bad == 0 and (ok + bad) > 0)
            self.log_write(f"[门禁] {ok}/{ok + bad} 项通过"
                           + ("" if self.gate_ok else f"，未通过：{ev.get('failed')}"))
        elif k == "done":
            self.done_file = ev.get("out")
        elif k == "cancelled":
            self.log_write(f"[已取消] 编到第 {ev.get('at', '?')} 秒画面被停掉。")

    def overall(self):
        n = max(1, len(getattr(self, "queue", []) or [1]))
        frac = (self.enc_pct or 0) / 100.0
        return min(100.0, (self.qi + frac) / n * 100.0)

    def cancel_job(self):
        if not self.busy:
            return
        self.cancel_asked = True
        CANCEL_FLAG.write_text("cancel", encoding="utf-8")
        self.log_write("[取消] 正在停 ffmpeg…")
        self.lbl_state.configure(text="正在取消…", fg=WARN)

    def clean_work(self, item):
        tag = Path(item["out"]).stem
        for pat in (f"enc-{tag}.265", f"enc-{tag}.pcm", f"seg-{tag}-*.265",
                    f"th-{tag}-*.png"):
            for f in WORK.glob(pat):
                try:
                    f.unlink()
                except OSError:
                    pass

    def open_out(self):
        d = self.out_dir()
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(d)          # noqa: S606
        except OSError as e:
            messagebox.showwarning("打开输出目录", str(e))

    def copy_last(self):
        if not self.done_file or not Path(self.done_file).exists():
            return
        try:
            copy = load_tool("copy-to-card.py", "copycard")
        except Exception:
            return messagebox.showerror("错误", "拷卡模块加载失败")
        old = sys.argv
        sys.argv = ["copy-to-card.py", str(self.done_file)]
        try:
            copy.main()
        except SystemExit as e:
            self.log_write(f"[拷卡] 退出码 {e.code}")
        finally:
            sys.argv = old

    # ---------- 状态 / 日志 ----------
    def set_state(self, st):
        self.state = st
        busy = st == "run"
        self.btn_run.configure(state="disabled" if (busy or not self.items)
                               else "normal")

    def log_write(self, text, tag=None):
        self.log.configure(state="normal")
        if tag is None:
            tag = ("bad" if ("[!!]" in text or "[FAIL]" in text or "失败" in text
                             or "异常" in text) else
                   "ok" if ("[OK]" in text or "通过" in text) else
                   "warn" if ("⚠" in text or "警告" in text) else "")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")



def selftest(report=None):
    headless_modals()
    root = tk.Tk()
    app = App(root)
    n = len(root.winfo_children())

    def fin():
        have = [k for k in ("conv", "verify", "split") if app.tools.get(k)]
        miss = [k for k in ("conv", "verify", "split") if not app.tools.get(k)]
        try:
            load_tool("copy-to-card.py", "copycard")
            extra = " / copy 能加载"
        except Exception:
            extra = " / copy 加载失败"
        app.add_paths([Path(__file__)])          # 队列能进条目
        text = (f"UI OK：顶层子控件 {n} 个、队列 {len(app.items)} 条 | "
                f"画质 {app.quality()[0]} | 校验 {app.var_verify.get()} | "
                f"工具: {'+'.join(have) or '无'}"
                + (f"（缺 {'+'.join(miss)}）" if miss else "") + extra)
        if report:
            Path(report).write_text(text + "\n", encoding="utf-8")
        print(text)
        root.destroy()

    root.after(1200, fin)
    root.mainloop()


def headless_modals():
    def rec(*a, **_k):
        NOTES.append("[弹窗] " + " ".join(str(x) for x in a))

    for _n in ("showerror", "showwarning", "showinfo"):
        setattr(messagebox, _n, rec)
    messagebox.askyesno = lambda *a, **_k: True


NOTES = []
AUTO_TIMEOUT = 3600


def auto_smoke():
    """端到端冒烟：走真实队列把「① 转换 + ② 门禁」跑完，结果写进 --report。"""
    a = sys.argv
    src = a[a.index("--auto") + 1]
    src2 = a[a.index("--auto2") + 1] if "--auto2" in a else ""
    outdir = a[a.index("--outdir") + 1] if "--outdir" in a else str(Path(src).parent)
    name = a[a.index("--outname") + 1] if "--outname" in a else "DSC_4700.MOV"
    report = Path(a[a.index("--report") + 1]) if "--report" in a else WORK / "auto.txt"
    quality = a[a.index("--quality") + 1] if "--quality" in a else "1"

    headless_modals()
    t0 = time.time()
    root = tk.Tk()
    app = App(root)
    app.var_outdir.set(outdir)
    try:
        app.var_quality.set(int(quality))
    except Exception:
        pass
    app.seq = int(Path(name).stem.split("_")[1]) - 1 if "_" in name else 0
    app.add_paths([Path(src).resolve()])
    app.items[-1]["out"] = name
    app.tv.set(app.items[-1]["iid"], "out", name)
    if src2:
        app.seq = int(Path(name).stem.split("_")[1])
        app.add_paths([Path(src2).resolve()])
    state = {"ok": False}

    def write_report(success):
        u = app.used or {}
        done = [it["out"] for it in app.items if it["status"] != ""]
        lines = [
            f"结果       : {'成功' if success else '失败'}",
            f"用时       : {time.time() - t0:.0f}s",
            f"输入       : {src}" + (f" + {src2}" if src2 else ""),
            f"输出目录   : {outdir}",
            f"输出       : {', '.join(done)}",
            "输出存在   : " + ", ".join(
                f"{it['out']}={Path(outdir, it['out']).exists()}"
                for it in app.items),
            f"界面状态   : {app.state}   门禁通过: {app.gate_ok}",
            "输出帧率   : " + ("29.97（跟随源）" if u.get("fps") == "source"
                              else "59.94") + "   模板: " + (u.get("tpl") or "-"),
            "码率/预设  : " + (f"{u.get('bitrate')} / {u.get('preset')}" if u else "-"),
            f"实际参数   : {u.get('argv', '-')}",
            "工具       : " + " / ".join(
                f"{k}{'OK' if app.tools.get(k) else '缺'}"
                for k in ("conv", "verify", "split")),
            "队列       : " + " | ".join(f"{it['out']}:{it['status']}"
                                        for it in app.items),
            "状态行     : " + app.lbl_state.cget("text"),
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
        app.run_queue()

    def watch():
        if app.busy:
            if time.time() - t0 > AUTO_TIMEOUT:
                write_report(False)
                root.destroy()
                return
            root.after(500, watch)
            return
        state["ok"] = app.fail == 0 and (app.gate_ok or not app.var_verify.get())
        write_report(state["ok"])
        root.destroy()

    root.after(400, go)
    root.after(1500, watch)
    root.mainloop()
    sys.exit(0 if state["ok"] else 1)


def main():
    root = tk.Tk()
    app = App(root)
    if len(sys.argv) > 1 and Path(sys.argv[1]).exists():
        app.add_paths([Path(sys.argv[1]).resolve()])
    root.mainloop()


if __name__ == "__main__":
    if "--auto" in sys.argv:
        auto_smoke()
    elif "--selftest" in sys.argv:
        selftest(sys.argv[sys.argv.index("--report") + 1]
                 if "--report" in sys.argv else None)
    else:
        main()
