<#
    start_api.ps1 —— 启动本地 Agent Web 工作台的 API 服务

    用法：
        powershell -ExecutionPolicy Bypass -File deploy\start_api.ps1

    行为：
        1. 检查两个 llama-server 端点（8080 / 8081）是否可达
           —— 不可达时给出明确提示，**不会自动去启停 llama-server**
        2. 设置必要环境变量
        3. 用 uvicorn 启动 agent_platform.api.app:app（127.0.0.1:8000）
        4. 打印访问地址
#>

[CmdletBinding()]
param(
    [int]    $Port       = 8000,
    [string] $BindHost   = '127.0.0.1',
    [string] $LlmBaseUrl = '',
    [string] $LlmModel   = 'gpt-oss-20b',
    [switch] $SkipCheck
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

Write-Host ''
Write-Host '===============================================================' -ForegroundColor Cyan
Write-Host ' Agent平台 —— Web 工作台 API 启动脚本' -ForegroundColor Cyan
Write-Host '===============================================================' -ForegroundColor Cyan
Write-Host " 仓库根目录 : $RepoRoot"
Write-Host ''

# ----------------------------------------------------------------------
# 1) 检查 llama-server 端点（只读探测，不启停）
# ----------------------------------------------------------------------
$Endpoints = @(
    @{ Name = 'gpt-oss-20b (GPU)'; Port = 8080 },
    @{ Name = 'qwen2.5-14b (CPU)'; Port = 8081 }
)

if (-not $SkipCheck) {
    Write-Host '[1/4] 检查本地推理端点 ...' -ForegroundColor Cyan
    $Down = @()
    foreach ($ep in $Endpoints) {
        $uri = "http://127.0.0.1:$($ep.Port)/v1/models"
        try {
            $r = Invoke-WebRequest -Uri $uri -TimeoutSec 8 -UseBasicParsing
            $model = ''
            try { $model = ($r.Content | ConvertFrom-Json).data[0].id } catch { }
            Write-Host ("  [OK]   {0,-22} :{1}  ({2})" -f $ep.Name, $ep.Port, $model) -ForegroundColor Green
        } catch {
            Write-Host ("  [FAIL] {0,-22} :{1}  不可达" -f $ep.Name, $ep.Port) -ForegroundColor Red
            $Down += $ep
        }
    }
    if ($Down.Count -gt 0) {
        Write-Host ''
        Write-Host '  !! 以下端点不可达：' -ForegroundColor Yellow
        foreach ($ep in $Down) { Write-Host "     - $($ep.Name)  http://127.0.0.1:$($ep.Port)/v1" -ForegroundColor Yellow }
        Write-Host ''
        Write-Host '  本脚本**不会**自动启动/重启 llama-server（避免打断已在跑的推理进程）。' -ForegroundColor Yellow
        Write-Host '  请手动确认 llama-server 已启动后再重试；' -ForegroundColor Yellow
        Write-Host '  或加 -SkipCheck 跳过检查强制启动（此时 /health 会显示 LLM 不可达）。' -ForegroundColor Yellow
        Write-Host ''
        exit 2
    }
    Write-Host ''
} else {
    Write-Host '[1/4] 已跳过端点检查 (-SkipCheck)' -ForegroundColor DarkGray
}

# ----------------------------------------------------------------------
# 2) 端口占用检查（不 kill 任何进程）
# ----------------------------------------------------------------------
$Busy = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($Busy) {
    Write-Host ''
    Write-Host "  !! 端口 $Port 已被占用（PID $($Busy[0].OwningProcess)）。" -ForegroundColor Yellow
    Write-Host '     本脚本不会自动结束占用进程。请换一个端口，例如：' -ForegroundColor Yellow
    Write-Host "        powershell -ExecutionPolicy Bypass -File deploy\start_api.ps1 -Port 8010" -ForegroundColor Yellow
    Write-Host ''
    exit 4
}

# ----------------------------------------------------------------------
# 3) 环境变量
# ----------------------------------------------------------------------
Write-Host '[2/4] 设置环境变量 ...' -ForegroundColor Cyan

if (-not $LlmBaseUrl) { $LlmBaseUrl = 'http://127.0.0.1:8080/v1' }

$env:LLM_BASE_URL        = $LlmBaseUrl
$env:LLM_MODEL           = $LlmModel
$env:LLM_REASONING_EFFORT= 'medium'
$env:LLM_TEMPERATURE     = '0.2'
$env:LLM_MAX_TOKENS      = '2048'
$env:LLM_API_KEY         = 'local'
$env:LLM_TIMEOUT         = '180'
$env:HF_HUB_OFFLINE      = '1'

# BGE-M3 / reranker 本地权重路径（存在才设置，避免指向不存在的目录）
# 依次探测：仓库内 models\BAAI\* → 环境变量 → 常见本地模型仓目录
$BgeCandidates = @(
    (Join-Path $RepoRoot 'models\BAAI\bge-m3'),
    $env:BGE_M3_PATH,
    'E:\model\models\bge-m3',
    (Join-Path $env:USERPROFILE '.cache\huggingface\hub\models--BAAI--bge-m3')
)
$BgeRerCandidates = @(
    (Join-Path $RepoRoot 'models\BAAI\bge-reranker-v2-m3'),
    $env:BGE_RERANKER_PATH,
    'E:\model\models\bge-reranker-v2-m3',
    (Join-Path $env:USERPROFILE '.cache\huggingface\hub\models--BAAI--bge-reranker-v2-m3')
)
$BgeM3  = $BgeCandidates  | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
$BgeRer = $BgeRerCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if ($BgeM3)   { $env:BGE_M3_PATH        = $BgeM3;   Write-Host "  BGE_M3_PATH        = $BgeM3" -ForegroundColor DarkGray }
else          { Write-Host '  BGE_M3_PATH        = (未找到本地目录，沿用代码内默认)' -ForegroundColor DarkGray }
if ($BgeRer)  { $env:BGE_RERANKER_PATH  = $BgeRer; Write-Host "  BGE_RERANKER_PATH  = $BgeRer" -ForegroundColor DarkGray }
else          { Write-Host '  BGE_RERANKER_PATH  = (未找到本地目录，沿用代码内默认)' -ForegroundColor DarkGray }
Write-Host "  LLM_BASE_URL       = $env:LLM_BASE_URL" -ForegroundColor DarkGray
Write-Host "  LLM_MODEL          = $env:LLM_MODEL" -ForegroundColor DarkGray
Write-Host '  HF_HUB_OFFLINE     = 1' -ForegroundColor DarkGray

# ----------------------------------------------------------------------
# 4) 依赖自检
# ----------------------------------------------------------------------
Write-Host ''
Write-Host '[3/4] 检查 Python 依赖 ...' -ForegroundColor Cyan
$missing = @()
foreach ($m in @('fastapi', 'uvicorn', 'langgraph', 'openai')) {
    & python -c "import $m" 2>$null
    if ($LASTEXITCODE -ne 0) { $missing += $m }
}
if ($missing.Count -gt 0) {
    Write-Host ("  缺少依赖: {0}" -f ($missing -join ', ')) -ForegroundColor Red
    Write-Host '  请执行: pip install fastapi uvicorn langgraph openai' -ForegroundColor Yellow
    exit 3
}
Write-Host '  依赖齐备 (fastapi / uvicorn / langgraph / openai)' -ForegroundColor Green

# ----------------------------------------------------------------------
# 5) 启动 uvicorn
# ----------------------------------------------------------------------
Write-Host ''
Write-Host '[4/4] 启动 uvicorn ...' -ForegroundColor Cyan
$Url = "http://${BindHost}:${Port}/ui/"
Write-Host ''
Write-Host '===============================================================' -ForegroundColor Green
Write-Host "  Web 工作台 : $Url" -ForegroundColor Green
Write-Host "  API 文档   : http://${BindHost}:${Port}/docs" -ForegroundColor Green
Write-Host "  健康检查   : http://${BindHost}:${Port}/health" -ForegroundColor Green
Write-Host '===============================================================' -ForegroundColor Green
Write-Host ''

# --timeout-keep-alive 120：
#   默认 5 秒的 keep-alive 在本机**太短** —— /kb/search 等接口要跑
#   几十秒（CPU 版 bge-m3 + 交叉编码器），客户端在等响应时连接早已被
#   服务端回收，下一个请求就撞上 ``ReadError: [WinError 10054]``。
& python -m uvicorn agent_platform.api.app:app --host $BindHost --port $Port --log-level info --timeout-keep-alive 120
