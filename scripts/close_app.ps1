param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8766,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\', '/')

function Get-Listeners {
    # Query all listeners once: a missing port is a normal "already stopped" result.
    @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalPort -eq $Port })
}

try {
    $listeners = Get-Listeners
    if ($listeners.Count -eq 0) {
        Write-Host "PaperRead Sync 未运行（端口 $Port），无需关闭。"
        exit 0
    }

    $request = [Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/health")
    $request.Proxy = $null
    $request.AllowAutoRedirect = $false
    $request.Timeout = 3000
    $request.ReadWriteTimeout = 3000
    $response = $request.GetResponse()
    try {
        $reader = New-Object IO.StreamReader($response.GetResponseStream())
        try { $service = $reader.ReadToEnd() | ConvertFrom-Json }
        finally { $reader.Dispose() }
    }
    finally { $response.Dispose() }

    if ($service.status -ne 'ok' -or $service.account -ne 'default' -or
        $service.mock -isnot [bool] -or $service.mock -or
        $service.project_root -isnot [string] -or $service.module -isnot [string]) {
        throw '端口上的服务身份不匹配，未关闭任何进程。'
    }
    $reportedRoot = [IO.Path]::GetFullPath($service.project_root).TrimEnd('\', '/')
    $reportedModule = [IO.Path]::GetFullPath($service.module).TrimEnd('\', '/')
    if (-not [string]::Equals($reportedRoot, $projectRoot, [StringComparison]::OrdinalIgnoreCase) -or
        -not [string]::Equals($reportedModule, (Join-Path $projectRoot 'paperread'), [StringComparison]::OrdinalIgnoreCase)) {
        throw '该服务属于其他项目，未关闭任何进程。'
    }
    $servicePid = 0
    if (-not [int]::TryParse([string]$service.pid, [ref]$servicePid) -or $servicePid -le 0 -or
        $servicePid -notin @($listeners.OwningProcess)) {
        throw '服务 PID 与端口监听进程不一致，未关闭任何进程。'
    }

    $details = Get-CimInstance Win32_Process -Filter "ProcessId = $servicePid"
    $rootArgument = '(?i)(?:^|\s)--root\s+(?:"' + [regex]::Escape($projectRoot) + '"|' +
                    [regex]::Escape($projectRoot) + '(?=\s|$))'
    if (-not $details -or $details.Name -notin @('python.exe', 'pythonw.exe') -or
        $details.CommandLine -notmatch '(?i)(?:^|\s)-m\s+paperread(?:\s|$)' -or
        $details.CommandLine -notmatch '(?i)(?:^|\s)serve(?:\s|$)' -or
        $details.CommandLine -notmatch $rootArgument) {
        throw '无法确认进程是本项目的阅读服务，未关闭任何进程。'
    }

    $serviceProcess = Get-Process -Id $servicePid
    # Retain a handle to this exact process, including while checking the port again.
    $null = $serviceProcess.Handle
    try {
        if ($servicePid -notin @((Get-Listeners).OwningProcess)) {
            throw '端口监听进程已变化，请重新运行关闭脚本。'
        }
        if ($CheckOnly) {
            Write-Host "已确认本项目服务：端口 $Port，PID $servicePid（仅检查，未关闭）。"
            exit 0
        }
        Write-Host "正在关闭 PaperRead Sync：端口 $Port，PID $servicePid …"
        Stop-Process -InputObject $serviceProcess -ErrorAction Stop
        if (-not $serviceProcess.WaitForExit(5000)) {
            throw '服务尚未退出，请在任务管理器中检查。'
        }
        Write-Host '服务已关闭。阅读账本和已保存的授权仍保留。'
        exit 0
    }
    finally { $serviceProcess.Dispose() }
}
catch {
    Write-Host ('无法关闭：' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
