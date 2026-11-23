<#
    start_platform.ps1 —— 「一键启动整个平台」的编排脚本

    用法：
        powershell -ExecutionPolicy Bypass -File deploy\start_platform.ps1
        powershell -ExecutionPolicy Bypass -File deploy\start_platform.ps1 -Models all
        powershell -ExecutionPolicy Bypass -File deploy\start_platform.ps1 -Models gpt-oss-20b
        powershell -ExecutionPolicy Bypass -File deploy\start_platform.ps1 -Models gpt-oss-20b,qwen2.5-14b
        powershell -ExecutionPolicy Bypass -File deploy\start_platform.ps1 -NoDesktop

    行为（**全程幂等**，重复跑不会重复起进程）：
        1. 解析要启动的模型列表（默认只要 gpt-oss-20b —— 绝大多数任务用它）
        2. 通过 **ModelManager** 启动这些模型（已在本管理器之外启动的，
           会被识别为「已在运行」而**不会**起重复进程）
        3. 启动 API 服务（端口 8010，已在跑则跳过）
        4. 拉起桌面工作台窗口

    为什么要有这个脚本
    ----------------
    之前「起平台」需要用户手动分三步：
        deploy\start_servers.ps1  （两个 llama-server 永远都起，CPU 白占 6 个线程）
        deploy\start_api.ps1
        deploy\start_desktop.ps1
    而用户真正要的是「**点一下就能用**」，并且**用不到的模型别启动**
    （qwen2.5-14b 跑在 CPU 上，不推理也占着 6 个逻辑线程，
      会挤占用户要求的「整机 CPU < 50%」预算）。

    这个脚本把三步合成一步，并且把「模型启停」交给
    ``agent_platform.core.model_lifecycle.ModelManager``（与 Web 前端
    的 /models/{alias}/start 是同一套逻辑，行为一致）。
#>

[CmdletBinding()]
param(
    # 要启动的模型别名（逗号分隔）。'all' = 全部。
    # 默认只要 gpt-oss-20b —— 大多数任务（agent / reasoning / code）都走它。
    [string] $Models      = 'gpt-oss-20b',
    [int]    $Port        = 8010,
    [string] $BindHost    = '127.0.0.1',
    # 不拉起桌面窗口（只起 API + 模型，适合无头 / 远程场景）
    [switch] $NoDesktop,
    # 只把模型起起来，不等就绪就返回（不常用）
    [switch] $NoWait,
    # 不启动 API（只管模型）
    [switch] $NoApi
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# ---------- 路径常量（与 deploy\start_servers.ps1 同源） ----------
$LlamaDir   = 'C:\Users\admin\.lmstudio\extensions\backends\llama.cpp-win-x86_64-nvidia-cuda12-avx2-2.65.1'
$VendorDir  = 'C:\Users\admin\.lmstudio\extensions\backends\vendor\win-llama-cuda12-vendor-v2'

# ---------- 数据目录（跟随 AGENT_DATA_DIR，与 core\paths.py 一致） ----------
if ($env:AGENT_DATA_DIR) {
    $DataDir = $env:AGENT_DATA_DIR
} else {
    $DataDir = Join-Path $RepoRoot 'data'
}
$LogDir = Join-Path $DataDir 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# ---------- 解析模型列表 ----------
$KnownModels = @('gpt-oss-20b', 'qwen2.5-14b')
$Requested = @()
foreach ($m in ($Models -split ',')) {
    $t = $m.Trim()
    if (-not $t) { continue }
    if ($t -eq 'all') { $Requested = $KnownModels; break }
    if ($KnownModels -notcontains $t) {
        Write-Host "  !! 未知模型 '$t'（已知：$($KnownModels -join ', ')，或 'all'）" -ForegroundColor Yellow
        exit 5
    }
    if ($Requested -notcontains $t) { $Requested += $t }
}

Write-Host ''
Write-Host '===============================================================' -ForegroundColor Cyan
Write-Host ' Agent平台 —— 一键启动（模型按需 + API + 桌面）' -ForegroundColor Cyan
Write-Host '===============================================================' -ForegroundColor Cyan
Write-Host " 仓库根目录 : $RepoRoot"
Write-Host " 数据目录   : $DataDir"
Write-Host " 待启动模型 : $($Requested -join ', ')"
Write-Host ''

# ==============================================================
# 1) 前置检查
# ==============================================================
Write-Host '[1/4] 前置检查 ...' -ForegroundColor Cyan
if (-not (Test-Path "$LlamaDir\llama-server.exe")) {
    Write-Host "  !! 找不到 llama-server.exe: $LlamaDir\llama-server.exe" -ForegroundColor Red
    exit 3
}
if (-not (Test-Path $VendorDir)) {
    Write-Host "  !! 找不到 CUDA vendor DLL 目录: $VendorDir" -ForegroundColor Red
    Write-Host '     缺少 cublas/cudart 会导致 llama-server 以 0xC0000135 秒退。' -ForegroundColor Red
    exit 3
}
$Python = 'python'
if (Test-Path 'C:\ProgramData\miniconda3\python.exe') {
    $Python = 'C:\ProgramData\miniconda3\python.exe'
}
Write-Host "  Python : $Python" -ForegroundColor DarkGray
Write-Host '  llama-server / CUDA vendor 就位' -ForegroundColor Green

# ==============================================================
# 2) 启动模型（交给 ModelManager —— 幂等，不起重复进程）
# ==============================================================
Write-Host ''
Write-Host '[2/4] 启动模型 ...' -ForegroundColor Cyan

$ManagerScript = @"
import json, sys
sys.path.insert(0, r'$RepoRoot')
from agent_platform.core.model_lifecycle import ModelManager

mgr = ModelManager()
aliases = json.loads(r'''$($Requested | ConvertTo-Json -Compress)''')
if not isinstance(aliases, list):
    aliases = [aliases]

# 第二个参数 'nowait' 表示不等模型就绪（后台慢慢加载）
nowait = len(sys.argv) > 1 and sys.argv[1] == 'nowait'

results = []
for a in aliases:
    results.append(mgr.start_model(a, wait=not nowait))

# 把最终状态一起带出来，便于脚本打印
state = {m['alias']: m for m in mgr.list_models()}
print(json.dumps({'results': results, 'state': state}, ensure_ascii=False))
"@

$ManagerPy = Join-Path $env:TEMP ('agent_lifecycle_{0}.py' -f $PID)
Set-Content -Path $ManagerPy -Value $ManagerScript -Encoding UTF8

$ManagerArgs = @($ManagerPy)
if ($NoWait) { $ManagerArgs += 'nowait' }

# ★ stdout / stderr **分开**捕获：
#   用 2>&1 合并时，Python 打到 stderr 的任何一行（哪怕只是 warning）
#   都会被 PowerShell 当作「错误流」抛出，整个脚本以 exit 1 结束。
$ManagerErrFile = Join-Path $env:TEMP ('agent_lifecycle_err_{0}.txt' -f $PID)
$ManagerOut = & $Python $ManagerArgs 2>$ManagerErrFile
$ManagerExit = $LASTEXITCODE
Remove-Item $ManagerPy -Force -ErrorAction SilentlyContinue

if ($ManagerExit -ne 0) {
    Write-Host '  !! 模型启动脚本执行失败：' -ForegroundColor Red
    if (Test-Path $ManagerErrFile) {
        Get-Content $ManagerErrFile | ForEach-Object { Write-Host "     $_" -ForegroundColor Red }
        Remove-Item $ManagerErrFile -Force -ErrorAction SilentlyContinue
    }
    exit 6
}
Remove-Item $ManagerErrFile -Force -ErrorAction SilentlyContinue

# 解析 JSON 输出（最后一行是 JSON，前面可能有日志）
$JsonLine = ($ManagerOut | Where-Object { $_ -is [string] -and $_.Trim().StartsWith('{') } | Select-Object -Last 1)
if (-not $JsonLine) {
    Write-Host '  !! 无法解析模型启动结果：' -ForegroundColor Red
    $ManagerOut | ForEach-Object { Write-Host "     $_" -ForegroundColor Red }
    exit 6
}
$Payload = $JsonLine | ConvertFrom-Json

foreach ($r in $Payload.results) {
    $color = switch ($r.status) {
        'started'          { 'Green' }
        'already_running'  { 'DarkGray' }
        'starting'         { 'Yellow' }
        default            { 'Red' }
    }
    Write-Host ("  [{0}] {1}" -f $r.status.ToUpper(), $r.message) -ForegroundColor $color
}
foreach ($p in $Payload.state.PSObject.Properties) {
    $m = $p.Value
    $mark = if ($m.reachable) { 'OK  ' } elseif ($m.running) { '加载中' } else { '未运行' }
    Write-Host ("     {0,-14} :{1}  {2}  pid={3}  {4}" -f $m.alias, $m.port, $m.device, $m.pid, $mark) -ForegroundColor DarkGray
}

# ==============================================================
# 3) 启动 API（幂等：已在跑就跳过）
# ==============================================================
Write-Host ''
if ($NoApi) {
    Write-Host '[3/4] 已跳过 API（-NoApi）' -ForegroundColor DarkGray
} else {
    Write-Host '[3/4] 启动 API ...' -ForegroundColor Cyan
    $Busy = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($Busy) {
        Write-Host ("  API 已在运行（端口 {0}，PID {1}）—— 跳过，不重复启动。" -f $Port, $Busy[0].OwningProcess) -ForegroundColor Green
        Write-Host ("  健康检查 : http://{0}:{1}/health" -f $BindHost, $Port) -ForegroundColor DarkGray
    } else {
        # API 环境变量（与 deploy\start_api.ps1 保持一致）
        $env:LLM_BASE_URL         = 'http://127.0.0.1:8080/v1'
        $env:LLM_MODEL            = 'gpt-oss-20b'
        $env:LLM_REASONING_EFFORT = 'medium'
        $env:LLM_TEMPERATURE      = '0.2'
        $env:LLM_MAX_TOKENS       = '2048'
        $env:LLM_API_KEY          = 'local'
        $env:LLM_TIMEOUT          = '180'
        $env:HF_HUB_OFFLINE       = '1'

        $ApiLog = Join-Path $LogDir 'api.log'
        Write-Host "  uvicorn → http://${BindHost}:${Port}  日志：$ApiLog" -ForegroundColor Cyan
        # --timeout-keep-alive 120：与 start_api.ps1 同因（长请求会被 5 s keep-alive 掐断）
        Start-Process -FilePath $Python `
            -ArgumentList @('-m', 'uvicorn', 'agent_platform.api.app:app',
                            '--host', $BindHost, '--port', "$Port",
                            '--log-level', 'info', '--timeout-keep-alive', '120') `
            -WorkingDirectory $RepoRoot `
            -RedirectStandardOutput $ApiLog `
            -RedirectStandardError "$ApiLog.err" `
            -WindowStyle Hidden | Out-Null

        # 轮询 /health 直到就绪（最多 120 s：含 BGE 后台预热）
        $deadline = (Get-Date).AddSeconds(120)
        $apiOk = $false
        while ((Get-Date) -lt $deadline) {
            try {
                $h = Invoke-RestMethod -Uri "http://${BindHost}:$Port/health" -TimeoutSec 5
                if ($h) { $apiOk = $true; break }
            } catch { }
            Start-Sleep -Seconds 3
        }
        if ($apiOk) {
            Write-Host '  API 已就绪。' -ForegroundColor Green
        } else {
            Write-Host '  !! API 未在超时内就绪，请查看日志：' -ForegroundColor Yellow
            Write-Host "     $ApiLog" -ForegroundColor Yellow
            Write-Host "     $ApiLog.err" -ForegroundColor Yellow
        }
    }
}

# ==============================================================
# 4) 桌面工作台
# ==============================================================
Write-Host ''
if ($NoDesktop) {
    Write-Host '[4/4] 已跳过桌面窗口（-NoDesktop）' -ForegroundColor DarkGray
} else {
    Write-Host '[4/4] 启动桌面工作台 ...' -ForegroundColor Cyan
    # 桌面版会把 API 拉进**本进程**；这里 API 已在跑，桌面端只需连过去
    $DesktopLog = Join-Path $LogDir 'desktop.log'
    $ApiUrl = "http://${BindHost}:${Port}"
    Write-Host "  工作台地址 : $ApiUrl/ui/" -ForegroundColor Green
    Write-Host "  日志       : $DesktopLog" -ForegroundColor DarkGray
    Start-Process -FilePath $Python `
        -ArgumentList @('-m', 'agent_platform.desktop', '--api', $ApiUrl) `
        -WorkingDirectory $RepoRoot `
        -RedirectStandardOutput $DesktopLog `
        -RedirectStandardError "$DesktopLog.err" `
        -WindowStyle Hidden | Out-Null
}

Write-Host ''
Write-Host '===============================================================' -ForegroundColor Green
Write-Host " 工作台 : http://${BindHost}:${Port}/ui/" -ForegroundColor Green
Write-Host " API 文档: http://${BindHost}:${Port}/docs" -ForegroundColor Green
Write-Host " 模型状态: http://${BindHost}:${Port}/models/lifecycle" -ForegroundColor Green
Write-Host " 启停接口: POST /models/{alias}/start | POST /models/{alias}/stop" -ForegroundColor Green
Write-Host '===============================================================' -ForegroundColor Green
Write-Host ''
exit 0
