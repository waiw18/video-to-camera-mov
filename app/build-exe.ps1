# 把 app/main.py 打成 exe —— 直接跑这个脚本，不需要改任何东西
#
#   powershell -ExecutionPolicy Bypass -File app\build-exe.ps1
#
# 产出：
#   dist\相机视频转换器\相机视频转换器.exe     （--onedir，推荐：启动快、好排错）
#   dist\相机视频转换器-单文件.exe             （--onefile，只有一个文件，启动稍慢）
#
# 打包后**不需要额外装 Python**：工具模块被一起冻结，
# app/main.py 用 importlib 在进程内加载它们；ffmpeg/ffprobe 随包放进 bin\。
#
# 冻结后 app/main.py 会自动设置 CAMMOV_FFMPEG / CAMMOV_FFPROBE / CAMMOV_WORK，
# 所以随包的 ffmpeg 能被 binpath.py 找到，中间文件写到 exe 旁边而不是只读的 _MEIPASS。

$ErrorActionPreference = 'Stop'

$ROOT = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ROOT

$NAME = '相机视频转换器'

Write-Host "== 定位 Python =="
# PATH 里可能有多个 Python，必须挑**装了 PyInstaller + Pillow**的那个；
# 一个都没有的话，脚本会提示你用 CAMMOV_PYTHON 指定。
$cands = New-Object System.Collections.Generic.List[string]
if ($env:CAMMOV_PYTHON) { $cands.Add($env:CAMMOV_PYTHON) }
foreach ($exe in 'python.exe', 'python3.exe') {
    $cmd = Get-Command $exe -ErrorAction SilentlyContinue
    if ($cmd) { $cands.Add($cmd.Source) }
}
# 没进 PATH 的常见安装位置
foreach ($pat in "$env:LOCALAPPDATA\Programs\Python\Python*\python.exe",
                 'C:\Python3*\python.exe',
                 'C:\Program Files\Python3*\python.exe') {
    foreach ($p in (Get-ChildItem $pat -ErrorAction SilentlyContinue)) { $cands.Add($p.FullName) }
}

$PY = $null
$firstSeen = $null
foreach ($c in $cands) {
    if (-not $c -or -not (Test-Path $c)) { continue }
    if (-not $firstSeen) { $firstSeen = $c }
    & $c -c "import PyInstaller, PIL" 2>$null
    if ($LASTEXITCODE -eq 0) { $PY = $c; break }
}
if (-not $PY) {
    Write-Host "  以下解释器都没有 PyInstaller/Pillow："
    foreach ($c in $cands) { if ($c -and (Test-Path $c)) { Write-Host "    $c" } }
    throw "请用 CAMMOV_PYTHON 指定一个装了 PyInstaller 和 Pillow 的 Python"
}
Write-Host "  $PY"
Write-Host "  PyInstaller $(& $PY -m PyInstaller --version)"
Write-Host "  Pillow $(& $PY -c 'import PIL; print(PIL.__version__)')"

Write-Host "== 定位 ffmpeg =="
$FF = $null
if ($env:CAMMOV_FFMPEG -and (Test-Path $env:CAMMOV_FFMPEG)) {
    $FF = Split-Path -Parent $env:CAMMOV_FFMPEG
} else {
    $cmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($cmd) { $FF = Split-Path -Parent $cmd.Source }
}
if (-not $FF -or -not (Test-Path "$FF\ffmpeg.exe")) {
    # 常见的手工解压位置：C:\Program Files\ffmpeg-<版本>-full_build\bin
    foreach ($d in (Get-ChildItem 'C:\Program Files\ffmpeg*\bin' -Directory -ErrorAction SilentlyContinue)) { $FF = $d.FullName; break }
}
if (-not (Test-Path "$FF\ffmpeg.exe")) { throw "找不到 ffmpeg，请设置环境变量 CAMMOV_FFMPEG" }
Write-Host "  $FF"

Write-Host "`n== 检查依赖 =="
& $PY -m PyInstaller --version
& $PY -c "import PIL; print('  Pillow', PIL.__version__)"

# 只打包真正会被加载的模块。tools\ 下还有一百多个历史实验脚本
# （连调试 DSH 用的 .cjs 都在），不该跟着发布包发出去。
$TOOL_FILES = @(
    'video-to-camera-mov.py',   # 转换主程序（被 app\main.py 加载）
    'verify-output.py',         # 43 项门禁
    'copy-to-card.py',          # 拷卡
    'make-camera-mov.py',       # 上面三个的依赖：装相机 MOV 容器
    'camera-jpeg.py',           #   相机规格 JPEG（缩略图）
    'binpath.py',               #   找 ffmpeg/ffprobe（认 CAMMOV_FFMPEG 等环境变量）
    'boxio.py',                 #   MOV box 读写
    'hevc-ps.py'                #   H.265 参数集
)
# 模板必须是**你自己相机录的同规格 MOV**（仓库里没有：媒体文件不进版本库），
# 放在 templates\DSC_8955.MOV；换机型就换成对应规格的模板。
foreach ($p in @("$FF\ffmpeg.exe", "$FF\ffprobe.exe", 'templates\DSC_8955.MOV', 'app\main.py')) {
    if (-not (Test-Path $p)) { throw "缺少: $p" }
    Write-Host "  OK  $p"
}
foreach ($t in $TOOL_FILES) {
    if (-not (Test-Path "tools\$t")) { throw "缺少: tools\$t" }
    Write-Host "  OK  tools\$t"
}

$env:PYTHONIOENCODING = 'utf-8'
$common = @(
    '--noconfirm', '--clean',
    '--add-data', 'templates;templates',
    '--add-binary', "$FF\ffmpeg.exe;bin",
    '--add-binary', "$FF\ffprobe.exe;bin",
    '--hidden-import', 'PIL._tkinter_finder'
)
foreach ($t in $TOOL_FILES) { $common += @('--add-data', "tools\$t;tools") }

Write-Host "`n== 1/2 打包 --onedir（推荐）=="
& $PY -m PyInstaller @common --onedir --windowed --name $NAME app\main.py
if ($LASTEXITCODE -ne 0) { throw "onedir 打包失败" }

Write-Host "`n== 2/2 打包 --onefile（单文件）=="
& $PY -m PyInstaller @common --onefile --windowed --name "$NAME-单文件" app\main.py
if ($LASTEXITCODE -ne 0) { throw "onefile 打包失败" }

Write-Host "`n== 产出 =="
Get-ChildItem "$ROOT\dist" -Recurse -Filter *.exe |
    Select-Object FullName, @{n='MB'; e={[math]::Round($_.Length/1MB,1)}} | Format-Table -AutoSize

Write-Host @"

== 下一步：必须端到端实测一次 ==
  0) 先跑一遍不需要人点的冒烟测试（结果写进报告文件，--windowed 没有控制台可看）：
       相机视频转换器.exe --auto <源视频> --outdir <输出目录> --outname DSC_4700.MOV ^
                          --report <报告文件.txt>
     报告里"结果: 成功" + "界面状态: done" + "门禁通过: True" 才算通过
  1) 双击 dist\$NAME\$NAME.exe
  2) 选一个源视频，输出编号写一个卡上没占用的（如 DSC_4600.MOV）
  3) 依次点 ①转换 → ②校验（必须 43/43）→ ③拷到卡上
  4) 相机里真机播放，确认：能播、时长对、音视频同步、完整播完
  5) 如果窗口起不来：先去掉 --windowed 重打一次，看控制台的报错

== 打包后必查 ==
  - dist\...\_internal\bin\ 下要有 ffmpeg.exe 与 ffprobe.exe
  - dist\...\_internal\templates\DSC_8955.MOV 要在
  - dist\...\_internal\tools\ 里只该有那 8 个 .py（多出来就是又把实验脚本打进去了）
  - 在**没装 Python、没装 ffmpeg** 的机器上跑一次（这是测试版的意义）
"@

exit 0
