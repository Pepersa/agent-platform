# 启用本地 K8s 依赖（WSL + Docker Desktop）
#
# ⚠️ 需要**管理员权限**运行 PowerShell
# ⚠️ 这些服务默认被禁用，可能是公司安全策略（禁用 WSL/Docker 是常见管控手段）
#    执行前请确认公司 IT 政策允许
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File deploy\enable_k8s.ps1

$ErrorActionPreference = 'Continue'

Write-Host "=== 1/4 检查当前服务状态 ===" -ForegroundColor Cyan
Get-Service -Name 'WslService', 'com.docker.service' -ErrorAction SilentlyContinue |
    Select-Object Name, Status, StartType | Format-Table -AutoSize

Write-Host "=== 2/4 设置为自动启动并启动 ===" -ForegroundColor Cyan
foreach ($svc in @('WslService', 'com.docker.service')) {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if (-not $s) { Write-Host "  跳过 $svc（服务不存在）" -ForegroundColor Yellow; continue }
    if ($s.StartType -eq 'Disabled') {
        Write-Host "  正在启用 $svc ..." -ForegroundColor Yellow
        try {
            Set-Service -Name $svc -StartupType Automatic -ErrorAction Stop
            Start-Service -Name $svc -ErrorAction Stop
            Write-Host "  [OK] $svc 已启用并启动" -ForegroundColor Green
        } catch {
            Write-Host "  [FAIL] $svc : $_" -ForegroundColor Red
            Write-Host "         可能是组策略限制，请联系 IT" -ForegroundColor DarkYellow
        }
    } else {
        try { Start-Service -Name $svc -ErrorAction Stop } catch {}
        Write-Host "  [OK] $svc 已在运行" -ForegroundColor Green
    }
}

Write-Host "`n=== 3/4 确保 WSL2 内核可用 ===" -ForegroundColor Cyan
$wslStatus = wsl --status 2>&1 | Out-String
if ($wslStatus -match 'ERROR_SERVICE_DISABLED') {
    Write-Host "  WSL 仍不可用，尝试安装内核..." -ForegroundColor Yellow
    wsl --install --no-distribution
    Write-Host "  安装后需要重启 Windows" -ForegroundColor Yellow
} else {
    Write-Host "  WSL 状态: $($wslStatus.Trim())" -ForegroundColor Green
}

Write-Host "`n=== 4/4 后续步骤 ===" -ForegroundColor Cyan
Write-Host @"
  1) 重启 Windows（若第 3 步触发了内核安装）
  2) 安装 kind + kubectl:
       winget install Kubernetes.kind
       winget install Kubernetes.kubectl
  3) 创建集群:
       kind create cluster --config deploy\kind.yaml
  4) 部署:
       kubectl apply -f deploy\k8s.yaml
  5) 验证:
       kubectl -n agent-platform get pods -w

  注意：NVIDIA GPU 在 kind 中需要额外配置 device plugin，
  单节点验证时建议先只跑 CPU 组件（postgres + agent-api），
  推理服务留在宿主机用 deploy\start_servers.ps1 跑。
"@ -ForegroundColor Gray
