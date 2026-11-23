<#
    start_desktop.ps1 —— 启动 Agent平台 桌面版工作台

    用法：
        powershell -ExecutionPolicy Bypass -File deploy\start_desktop.ps1
        powershell -ExecutionPolicy Bypass -File deploy\start_desktop.ps1 -Port 8010 -BrowserOnly

    行为：
        1. 检查两个 llama-server 端点（8080 / 8081）是否可达
           —— 不可达时给出明确提示并退出，**不会自动启停 llama-server**
        2. 设置环境变量（BGE_M3_PATH / BGE_RERANKER_PATH / HF_HUB_OFFLINE / LLM_* 等）
        3. 检查 Python 依赖（pywebview 缺失时提示降级到浏览器模式）
        4. 运行 python -m agent_platform.desktop
#>

[CmdletBinding()]
param(
    [int]    $Port       = 8010,
    [string] $BindHost   = '127.0.0.1',
    [string] $LlmBaseUrl = '',
    [string] $LlmModel   = 'gpt-oss-20b',
    [switch] $BrowserOnly,
    [switch] $SkipCheck
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

Write-Host ''
Write-Host '===============================================================' -ForegroundColor Cyan
Write-Host ' Agent平台 —— 桌面版工作台启动脚本' -ForegroundColor Cyan
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
# 2) 环境变量
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
$BgeM3 = Join-Path $RepoRoot 'models\BAAI\bge-m3'
$BgeRer = Join-Path $RepoRoot 'models\BAAI\bge-reranker-v2-m3'
if (Test-Path $BgeM3)   { $env:BGE_M3_PATH        = $BgeM3;   Write-Host "  BGE_M3_PATH        = $BgeM3" -ForegroundColor DarkGray }
else                   { Write-Host '  BGE_M3_PATH        = (未找到本地目录，沿用代码内默认)' -ForegroundColor DarkGray }
if (Test-Path $BgeRer) { $env:BGE_RERANKER_PATH  = $BgeRer; Write-Host "  BGE_RERANKER_PATH  = $BgeRer" -ForegroundColor DarkGray }
else                  { Write-Host '  BGE_RERANKER_PATH  = (未找到本地目录，沿用代码内默认)' -ForegroundColor DarkGray }
Write-Host "  LLM_BASE_URL       = $env:LLM_BASE_URL" -ForegroundColor DarkGray
Write-Host "  LLM_MODEL          = $env:LLM_MODEL" -ForegroundColor DarkGray
Write-Host '  HF_HUB_OFFLINE     = 1' -ForegroundColor DarkGray

# ----------------------------------------------------------------------
# 3) 依赖自检
# ----------------------------------------------------------------------
Write-Host ''
Write-Host '[3/4] 检查 Python 依赖 ...' -ForegroundColor Cyan
$missing = @()
foreach ($m in @('fastapi', 'uvicorn', 'langgraph')) {
    & python -c "import $m" 2>$null
    if ($LASTEXITCODE -ne 0) { $missing += $m }
}
if ($missing.Count -gt 0) {
    Write-Host ("  缺少依赖: {0}" -f ($missing -join ', ')) -ForegroundColor Red
    Write-Host '  请执行: pip install fastapi uvicorn langgraph' -ForegroundColor Yellow
    exit 3
}
Write-Host '  核心依赖齐备 (fastapi / uvicorn / langgraph)' -ForegroundColor Green

& python -c "import webview" 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host '  pywebview 已安装 → 桌面窗口模式' -ForegroundColor Green
} else {
    Write-Host '  pywebview 未安装 → 将自动降级为「默认浏览器模式」' -ForegroundColor Yellow
    Write-Host '    如需原生窗口：pip install pywebview' -ForegroundColor Yellow
}

# ----------------------------------------------------------------------
# 4) 启动桌面版
# ----------------------------------------------------------------------
Write-Host ''
Write-Host '[4/4] 启动桌面版 ...' -ForegroundColor Cyan
$ApiUrl = "http://${BindHost}:${Port}"
Write-Host ''
Write-Host '===============================================================' -ForegroundColor Green
Write-Host ' 桌面版工作台将自动打开。' -ForegroundColor Green
Write-Host " 若 8010 上没有 API，桌面版会在**本进程内**自动启动一个后端。" -ForegroundColor DarkGray
Write-Host " 工作台地址 : $ApiUrl/ui/" -ForegroundColor Green
Write-Host '===============================================================' -ForegroundColor Green
Write-Host ''

if ($BrowserOnly) {
    & python -m agent_platform.desktop --api $ApiUrl --browser-only
} else {
    & python -m agent_platform.desktop --api $ApiUrl
}
exit $LASTEXITCODE
