<#
    build_desktop.ps1 —— 把桌面版打包成**单目录** exe（PyInstaller onedir，可选增强）

    用法：
        powershell -ExecutionPolicy Bypass -File deploy\build_desktop.ps1
        powershell -ExecutionPolicy Bypass -File deploy\build_desktop.ps1 -SkipInstall

    前置：
        pip install pyinstaller
        （Windows 桌面版还需要 Microsoft Edge WebView2 Runtime：
          https://developer.microsoft.com/microsoft-edge/webview2/）

    说明：
        · 本脚本是**可选增强**；即便打包失败，源码方式
          `python -m agent_platform.desktop` 仍然完全可用。
        · 打包会把 static 前端目录与 models 路径提示写进产物。
        · ★ **onedir 而非 onefile**（2026-11-24 重大修正）：
          ``--onefile`` 每次启动都要把整个运行时解压到 ``%TEMP%\_MEIxxxxxx``
          （实测约 **7.5 GB**），于是：
            ① 启动要等 **30~90 秒**（用户反馈「很慢」）；
            ② 连点几下桌面图标就会起**多个并发实例**同时解压，
               互相抢磁盘 I/O 和空间，解压失败后 bootloader 直接退出，
               **没有任何报错**（用户反馈「还闪退了」）。
          ``--onedir`` 在**打包时**就把运行时落盘到
          ``dist\desktop\AgentPlatform\``，启动只是普通 exe 加载
          （实测 1~2 秒），且**不再产生任何临时解压目录**。
          代价是产物从「一个 exe」变成「一个文件夹」，所以
          VBS / 快捷方式 / 文档里的路径都要跟着改成
          ``dist\desktop\AgentPlatform\AgentPlatform.exe``。
#>

[CmdletBinding()]
param(
    [switch] $SkipInstall,
    [string] $OutputDir = '',
    # 显式指定 Python 解释器（留空则自动探测）。
    [string] $PythonPath = ''
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if (-not $OutputDir) { $OutputDir = Join-Path $RepoRoot 'dist\desktop' }

Write-Host ''
Write-Host '===============================================================' -ForegroundColor Cyan
Write-Host ' Agent平台 —— 桌面版 exe 打包脚本' -ForegroundColor Cyan
Write-Host '===============================================================' -ForegroundColor Cyan
Write-Host " 仓库根目录 : $RepoRoot"
Write-Host " 输出目录   : $OutputDir"
Write-Host ''

# ----------------------------------------------------------------------
# 0) 解析 Python 解释器
# ----------------------------------------------------------------------
# ★ 不能直接用裸 `python`：某些机器的 PATH 上第一个 python 是
#   Microsoft Store 的 **占位符**（WindowsApps\python.exe），
#   它会弹商店窗口或直接失败，脚本就会莫名其妙挂掉。
#   这里按优先级找：显式 -PythonPath > PATH 上的真 python >
#   常见的 miniconda/anaconda 安装位置。
$PythonExe = $null
if ($PythonPath) {
    if (Test-Path $PythonPath) { $PythonExe = (Resolve-Path $PythonPath).Path }
} else {
    $candidates = @()
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    $candidates += @(
        'C:\ProgramData\miniconda3\python.exe',
        "$env:LOCALAPPDATA\miniconda3\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        'C:\Python313\python.exe'
    )
    foreach ($c in $candidates) {
        if (-not $c) { continue }
        if (-not (Test-Path $c)) { continue }
        # 跳过 Microsoft Store 的占位符
        if ($c -like '*\WindowsApps\python.exe') { continue }
        $PythonExe = $c
        break
    }
}
if (-not $PythonExe) {
    Write-Host '  !! 找不到可用的 Python 解释器。' -ForegroundColor Red
    Write-Host '     请用 -PythonPath 指定，例如：' -ForegroundColor Yellow
    Write-Host '       -PythonPath C:\ProgramData\miniconda3\python.exe' -ForegroundColor Yellow
    exit 2
}
Write-Host " Python 解释器 : $PythonExe" -ForegroundColor DarkGray

# ----------------------------------------------------------------------
# 1) 依赖检查
# ----------------------------------------------------------------------
Write-Host '[1/4] 检查 PyInstaller ...' -ForegroundColor Cyan
if (-not $SkipInstall) {
    Write-Host '  正在安装 PyInstaller（若已安装会很快跳过）...' -ForegroundColor DarkGray
    & $PythonExe -m pip install --disable-pip-version-check pyinstaller
    if ($LASTEXITCODE -ne 0) {
        Write-Host '  !! PyInstaller 安装失败。' -ForegroundColor Red
        Write-Host '     源码方式启动仍然完全可用：python -m agent_platform.desktop' -ForegroundColor Yellow
        exit 3
    }
}

& $PythonExe -c "import PyInstaller; print('PyInstaller', PyInstaller.__version__)" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '  !! 未检测到 PyInstaller。请先执行：pip install pyinstaller' -ForegroundColor Red
    Write-Host '     或跳过安装检查：-SkipInstall（但仍需已安装）' -ForegroundColor Yellow
    exit 3
}
Write-Host '  PyInstaller 可用' -ForegroundColor Green

# ----------------------------------------------------------------------
# 2) 模型路径提示（本地权重不进包，只在运行时通过环境变量指向）
# ----------------------------------------------------------------------
Write-Host ''
Write-Host '[2/4] 收集运行时资源 ...' -ForegroundColor Cyan
$StaticDir = Join-Path $RepoRoot 'agent_platform\api\static'
if (-not (Test-Path $StaticDir)) {
    Write-Host "  !! 找不到前端静态目录：$StaticDir" -ForegroundColor Red
    exit 4
}
Write-Host "  static 前端目录 : $StaticDir（将用 --add-data 打进 exe）" -ForegroundColor DarkGray

$BgeM3  = Join-Path $RepoRoot 'models\BAAI\bge-m3'
$BgeRer = Join-Path $RepoRoot 'models\BAAI\bge-reranker-v2-m3'
# ★ 本机的权重实际放在 E:\model\models\ 下（不在仓库里），
#   仓库内的 models\ 路径只是历史约定 —— 两处都找一遍。
if (-not (Test-Path $BgeM3) -and (Test-Path 'E:\model\models\bge-m3')) {
    $BgeM3 = 'E:\model\models\bge-m3'
}
if (-not (Test-Path $BgeRer) -and (Test-Path 'E:\model\models\bge-reranker-v2-m3')) {
    $BgeRer = 'E:\model\models\bge-reranker-v2-m3'
}
if (Test-Path $BgeM3)   { Write-Host "  BGE_M3_PATH       = $BgeM3" -ForegroundColor DarkGray }
else                   { Write-Host '  BGE_M3_PATH       = (未找到，运行时用环境变量指定)' -ForegroundColor DarkGray }
if (Test-Path $BgeRer) { Write-Host "  BGE_RERANKER_PATH = $BgeRer" -ForegroundColor DarkGray }
else                   { Write-Host '  BGE_RERANKER_PATH = (未找到，运行时用环境变量指定)' -ForegroundColor DarkGray }

# ----------------------------------------------------------------------
# 3) 打包
# ----------------------------------------------------------------------
Write-Host ''
Write-Host '[3/4] 执行 PyInstaller ...' -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$Args = @(
    '--noconfirm',
    '--clean',
    # ★ ★ onedir（2026-11-24 重大修正）★★
    #   为什么不用 onefile：onefile 每次启动都把整个运行时解压到
    #   %TEMP%\_MEIxxxxxx（实测约 7.5 GB），启动要 30~90 秒；
    #   连点几下桌面图标就会起多个实例同时解压，抢 I/O + 抢空间，
    #   bootloader 解压失败后**静默退出**（用户看不到任何报错）。
    #   onedir 在打包时就把运行时落盘，启动 = 普通 exe 加载（1~2 秒）。
    #
    #   产物形态变成一个**目录**：dist\desktop\AgentPlatform\AgentPlatform.exe
    #   ⚠️ 不要再加 ``--runtime-tmpdir`` —— 它**只对 onefile 有效**，
    #      onedir 下加它会让 PyInstaller 报警/行为不可预期。
    #      同时它本来就是「C 盘撑爆」风险的来源，去掉即从根上消除该类问题。
    '--onedir',
    # ★ 静默模式（2026-11-24）：``--window`` = **不弹控制台**。
    #   用户反馈「双击一下出来俩窗口」（黑框 + 应用窗口），改成 windowed 后
    #   只剩应用窗口一个。
    #   ⚠️ 代价：``--window`` 的产物**没有控制台子系统**，
    #      ``sys.stdout`` / ``sys.stderr`` 是 None —— 所以入口脚本
    #      （agent_platform/desktop/entry_point.py）**必须**先把它们
    #      重定向到日志文件，否则第一个 print() 就会 AttributeError 崩掉。
    '--window',
    '--name', 'AgentPlatform',
    '--distpath', $OutputDir,
    '--workpath', (Join-Path $RepoRoot 'build\pyinstaller'),
    '--specpath', (Join-Path $RepoRoot 'build\pyinstaller'),
    '--add-data', "$StaticDir;agent_platform/api/static",
    # ---- uvicorn 的动态后端（uvicorn 自己用 importlib 按字符串加载） ----
    '--hidden-import', 'uvicorn.logging',
    '--hidden-import', 'uvicorn.loops.auto',
    '--hidden-import', 'uvicorn.protocols.http.auto',
    '--hidden-import', 'uvicorn.protocols.websockets.auto',
    '--hidden-import', 'uvicorn.lifespan.on',
    # ---- 平台自身的全部子模块（tools/__init__.py 用 importlib.import_module
    #      动态加载 agent_platform.knowledge.*，静态分析抓不到） ----
    '--collect-submodules', 'agent_platform',
    # ---- 桌面窗口（pywebview）----
    # pywebview 自带 webview\__pyinstaller\hook-webview.py 钩子，
    # 但它只带 WebView2 的 DLL，不带各后端平台模块；这里显式补全，
    # 避免在别的机器上因缺后端模块而降级到浏览器模式。
    '--hidden-import', 'webview',
    '--hidden-import', 'webview.platforms.edgechromium',
    '--hidden-import', 'webview.platforms.winforms',
    '--collect-all', 'webview',
    # ---- 推理 / 检索栈（FlagEmbedding、sentence-transformers 内部大量
    #      动态 import 编码器与 tokenizer，静态分析抓不全） ----
    '--collect-all', 'FlagEmbedding',
    '--collect-all', 'sentence_transformers',
    '--collect-all', 'transformers',
    # ---- LangChain / LangGraph（langgraph 用 entrypoint 字符串动态解析） ----
    '--collect-all', 'langgraph',
    '--collect-all', 'langchain_core',
    # ---- OpenAI SDK（httpx / anyio 后端是动态挑选的） ----
    '--collect-all', 'openai',
    '--collect-all', 'httpx',
    # ---- SQLite（平台的知识库 / 会话 / 审计库都用它） ----
    '--collect-all', 'sqlite3',
    # ---- torch：transformers / FlagEmbedding 依赖它的算子注册表 ----
    '--collect-all', 'torch',
    '--paths', $RepoRoot
)
# 入口用绝对导入的 entry_point.py（PyInstaller 直接跑 __main__.py 时相对导入会失败）
$Args += (Join-Path $RepoRoot 'agent_platform\desktop\entry_point.py')

# ★ PYTHONUNBUFFERED：stdout 被重定向时（`> log.txt`、Start-Process -Redirect）
#   Python 的 stdout 会退化成块缓冲，导致启动日志全部滞留。
#   置 1 让每行立即落盘。
#   ★ 静默模式（``--window``）下 stdout 会被入口脚本重定向到
#   ``%LOCALAPPDATA%\AgentPlatform\logs\desktop.log``（行缓冲），
#   这里的环境变量同样生效，保证日志实时可读。
$env:PYTHONUNBUFFERED = '1'

# ★ 用探测出来的 $PythonExe，而不是裸 `python` ——
#   裸 `python` 在某些机器上会命中 Microsoft Store 的占位符。
& $PythonExe -m PyInstaller @Args
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host '  !! 打包失败。' -ForegroundColor Red
    Write-Host '     源码方式启动仍然完全可用：' -ForegroundColor Yellow
    Write-Host '        python -m agent_platform.desktop' -ForegroundColor Yellow
    exit 5
}

# ----------------------------------------------------------------------
# 4) 完成
# ----------------------------------------------------------------------
Write-Host ''
Write-Host '[4/4] 打包完成' -ForegroundColor Cyan
# ★ onedir 产物是一个**目录**：dist\desktop\AgentPlatform\AgentPlatform.exe
$BundleDir = Join-Path $OutputDir 'AgentPlatform'
$Exe = Join-Path $BundleDir 'AgentPlatform.exe'
if (Test-Path $Exe) {
    $BundleSizeMB = [math]::Round(
        (Get-ChildItem $BundleDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
    Write-Host ''
    Write-Host '===============================================================' -ForegroundColor Green
    Write-Host " 可执行文件 : $Exe" -ForegroundColor Green
    Write-Host " 产物目录   : $BundleDir" -ForegroundColor Green
    Write-Host " 目录总大小 : $BundleSizeMB MB" -ForegroundColor Green
    Write-Host ''
    Write-Host ' 运行前请设置模型路径环境变量，例如：' -ForegroundColor DarkGray
    Write-Host "   `$env:BGE_M3_PATH='$BgeM3'" -ForegroundColor DarkGray
    Write-Host "   `$env:BGE_RERANKER_PATH='$BgeRer'" -ForegroundColor DarkGray
    Write-Host '   $env:HF_HUB_OFFLINE=1' -ForegroundColor DarkGray
    Write-Host ''
    Write-Host ' onedir 模式：**不再有运行时解压目录**，也没有 _MEI* 残留。' -ForegroundColor DarkGray
    Write-Host '   启动只需 1~2 秒（不再是 30~90 秒）。' -ForegroundColor DarkGray
    Write-Host ''
    Write-Host ' 静默模式（--window）：双击 exe **不会**再出现控制台黑框。' -ForegroundColor DarkGray
    Write-Host '   启动诊断信息改写到日志文件：' -ForegroundColor DarkGray
    Write-Host '     %LOCALAPPDATA%\AgentPlatform\logs\desktop.log' -ForegroundColor DarkGray
    Write-Host '   调试时想看控制台输出，请用：启动Agent平台.bat' -ForegroundColor DarkGray
    Write-Host ''
    Write-Host ' 然后运行：' -ForegroundColor DarkGray
    Write-Host "   $Exe" -ForegroundColor DarkGray
    Write-Host '===============================================================' -ForegroundColor Green
    exit 0
} else {
    Write-Host "  !! 未找到产物 exe：$Exe" -ForegroundColor Red
    exit 6
}
