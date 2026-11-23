# 本地 Agent 平台 —— 启动脚本（Windows 原生，不依赖 WSL / Docker）
#
# 本机事实（实测，2026-11-23）：
#   WslService / com.docker.service 均为 Disabled，因此全部走 Windows 原生路径
#   llama-server 来自 LM Studio 的 llama.cpp 后端（2.65.1 / build 0.7.0-dev）
#   GPU: RTX 5070 Ti 16GB (16303 MiB), driver 581.80
#
# ★ 上下文窗口（-c）为什么默认 32768：
#   gpt-oss-20b 的 MXFP4 权重占 ~11.3 GB，8K 上下文时实测显存已用 15819/16303 MiB，
#   只剩 89 MiB —— 写操作审批 / 并发 / 长 prompt 极易触发 OOM 或抢占失败。
#   但 gpt-oss-20b 用的是 **GQA + q8_0 KV 量化**：
#       KV cache/token = 24 层 x 8 KV头 x 64 head_dim x 1 字节 x 2(K,V) = 24 KiB
#       8K  -> 0.19 GB      16K -> 0.38 GB      32K -> 0.75 GB      64K -> 1.50 GB
#   即 KV cache 极便宜，**扩窗口的边际成本只有几百 MB**，而 8192 上下文
#   才是「复杂代码任务」真正撞墙的原因（改一个 200 行的类就超窗）。
#   故默认提升到 32768（KV 0.75 GB，仍留足 ~2.8 GB 余量给激活与碎片）。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File deploy\start_servers.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\start_servers.ps1 -CtxSize 65536
#   powershell -ExecutionPolicy Bypass -File deploy\start_servers.ps1 -CpuOnly

param(
    [switch]$CpuOnly,
    [int]$GpuPort = 8080,
    [int]$CpuPort = 8081,
    # ★ 默认从 8192 提升到 32768（见文件头说明）
    [int]$CtxSize = 32768,
    # CPU 模型跑在 CPU 上（-ngl 0），不占显存；它的窗口可以给得更大
    [int]$CpuCtxSize = 32768
)

$ErrorActionPreference = 'Stop'

# ---------- 路径常量 ----------
$LlamaDir = 'C:\Users\admin\.lmstudio\extensions\backends\llama.cpp-win-x86_64-nvidia-cuda12-avx2-2.65.1'
$VendorDir = 'C:\Users\admin\.lmstudio\extensions\backends\vendor\win-llama-cuda12-vendor-v2'
$ModelDir = 'E:\model\models\lmstudio-community'
$GptOssModel = "$ModelDir\gpt-oss-20b-GGUF\gpt-oss-20b-MXFP4.gguf"
$QwenModel  = "$ModelDir\Qwen2.5-14B-Instruct-1M-GGUF\Qwen2.5-14B-Instruct-1M-Q4_K_M.gguf"

# 日志目录跟随**数据目录**（可用环境变量 AGENT_DATA_DIR 整体外置到别的盘；
# 不设时仍是 <repo>/data/logs，与改造前一致）
if ($env:AGENT_DATA_DIR) {
    $DataDir = $env:AGENT_DATA_DIR
} else {
    $DataDir = Join-Path $PSScriptRoot '..\data'
}
$LogDir = Join-Path $DataDir 'logs'

# ---------- 前置检查 ----------
if (-not (Test-Path "$LlamaDir\llama-server.exe")) {
    throw "找不到 llama-server.exe: $LlamaDir\llama-server.exe`n请检查 LM Studio 的 llama.cpp 后端版本"
}
if (-not (Test-Path $VendorDir)) {
    throw "找不到 CUDA vendor DLL 目录: $VendorDir`n缺少 cublas/cudart 会导致 llama-server 无法启动"
}
if (-not (Test-Path $GptOssModel)) { throw "找不到模型: $GptOssModel" }
if (-not (Test-Path $QwenModel))  { throw "找不到模型: $QwenModel" }

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# CUDA DLL 必须先于 exe 可被加载
$env:Path = "$LlamaDir;$VendorDir;$env:Path"
Write-Host "[env] PATH 已加入 llama.cpp 与 CUDA vendor 目录" -ForegroundColor DarkGray

# ---------- 缓存外置（pip / HuggingFace）→ 避免 C 盘再次被撑大 ----------
# 用户级环境变量只对**新开的**进程生效；从旧窗口运行时 $env: 里没有，
# 这里回读 HKCU\Environment 补齐，确保 llama-server 子进程不把
# HuggingFace 缓存写回 C 盘（详见 docs\07_C盘空间治理.md）。
foreach ($_cacheVar in @('PIP_CACHE_DIR', 'HF_HOME')) {
    # ⚠️ PowerShell 无法用 `$env:$_cacheVar` 动态取环境变量（会解析成 `$env:` + 字面量），
    #    必须用 [Environment]::GetEnvironmentVariable() 走进程环境块。
    $_cur = [Environment]::GetEnvironmentVariable($_cacheVar, 'Process')
    if (-not $_cur) {
        $_reg = [Environment]::GetEnvironmentVariable($_cacheVar, 'User')
        if ($_reg) {
            Set-Item -Path "Env:$_cacheVar" -Value $_reg
            Write-Host "[env] $_cacheVar = $_reg（从用户级环境变量回读）" -ForegroundColor DarkGray
        } else {
            Write-Host "[warn] 未找到用户级 $_cacheVar，缓存可能仍落在 C 盘" -ForegroundColor Yellow
        }
    } else {
        Write-Host "[env] $_cacheVar = $_cur" -ForegroundColor DarkGray
    }
}

function Start-LlamaServer {
    param(
        [string]$Model,
        [int]$Port,
        [string]$Alias,
        [int]$Ngl,
        [int]$Ctx,
        [string]$LogName
    )

    $log = Join-Path $LogDir $LogName
    Write-Host "[start] $Alias  port=$Port  ngl=$Ngl  ctx=$Ctx  log=$log" -ForegroundColor Cyan

    $args = @(
        '-m', $Model,
        '--host', '127.0.0.1',
        '--port', "$Port",
        '--alias', $Alias,
        '-ngl', "$Ngl",
        '-c', "$Ctx",
        '--parallel', '1',
        '--cache-type-k', 'q8_0',
        '--cache-type-v', 'q8_0',
        '--flash-attn', 'on',
        '--jinja',
        '--no-webui'
    )

    Start-Process -FilePath "$LlamaDir\llama-server.exe" `
        -ArgumentList $args `
        -WorkingDirectory $LlamaDir `
        -RedirectStandardOutput $log `
        -RedirectStandardError "$log.err" `
        -WindowStyle Hidden `
        -PassThru | Select-Object Id, ProcessName
}

# ---------- 启动 GPU 主模型（gpt-oss-20b） ----------
$ngl = if ($CpuOnly) { 0 } else { 99 }
Start-LlamaServer -Model $GptOssModel -Port $GpuPort -Alias 'gpt-oss-20b' -Ngl $ngl -Ctx $CtxSize -LogName 'gpt-oss-20b.log'

Start-Sleep -Seconds 12

# ---------- 启动 CPU 中文模型（Qwen2.5-14B） ----------
Start-LlamaServer -Model $QwenModel -Port $CpuPort -Alias 'qwen2.5-14b' -Ngl 0 -Ctx $CpuCtxSize -LogName 'qwen2.5-14b.log'

# ---------- 健康检查 ----------
Write-Host "`n等待服务就绪..." -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds(180)
$gpuOk = $false; $cpuOk = $false

while ((Get-Date) -lt $deadline) {
    if (-not $gpuOk) {
        try {
            $r = Invoke-RestMethod -Uri "http://127.0.0.1:$GpuPort/v1/models" -TimeoutSec 5
            if ($r.data) { $gpuOk = $true; Write-Host "  [OK] gpt-oss-20b  http://127.0.0.1:$GpuPort  ctx=$CtxSize" -ForegroundColor Green }
        } catch { }
    }
    if (-not $cpuOk) {
        try {
            $r = Invoke-RestMethod -Uri "http://127.0.0.1:$CpuPort/v1/models" -TimeoutSec 5
            if ($r.data) { $cpuOk = $true; Write-Host "  [OK] qwen2.5-14b   http://127.0.0.1:$CpuPort  ctx=$CpuCtxSize" -ForegroundColor Green }
        } catch { }
    }
    if ($gpuOk -and $cpuOk) { break }
    Start-Sleep -Seconds 5
}

if (-not ($gpuOk -and $cpuOk)) {
    Write-Host "`n部分服务未就绪，请查看 $LogDir 下的日志。" -ForegroundColor Yellow
    Write-Host "  gpt-oss-20b : $gpuOk"
    Write-Host "  qwen2.5-14b  : $cpuOk"
} else {
    Write-Host "`n两个服务均已就绪。" -ForegroundColor Green
}

# ---------- 显存占用报告（启动后实测） ----------
if (-not $CpuOnly) {
    try {
        $gpu = & nvidia-smi --query-gpu=name,memory.total,memory.used,memory.free --format=csv,noheader
        Write-Host "`n[gpu] $gpu" -ForegroundColor Cyan
    } catch { }
}

Write-Host "`n环境变量（写入 .env 供应用读取）：" -ForegroundColor Cyan
Write-Host "  LLM_BASE_URL=http://127.0.0.1:$GpuPort/v1"
Write-Host "  LLM_MODEL=gpt-oss-20b"
Write-Host "  LLM_CONTEXT_WINDOW=$CtxSize"
Write-Host "  LLM_SECONDARY_BASE_URL=http://127.0.0.1:$CpuPort/v1"
Write-Host "  LLM_SECONDARY_MODEL=qwen2.5-14b"
