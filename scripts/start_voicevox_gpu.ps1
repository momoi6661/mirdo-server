param(
    # 可选：手动指定 VOICEVOX 引擎目录或它的上级目录。
    # 不传时，脚本会从“当前工作目录”和“脚本所在目录”附近查找 run.exe。
    [string]$EngineDir = "",

    # VOICEVOX Engine 默认端口。
    [int]$Port = 50021,

    # Godot 和 Mirdo Server 都只需要本机访问，所以默认绑定 127.0.0.1。
    [string]$HostAddress = "127.0.0.1",

    # 端口被旧的 VOICEVOX 占用时，默认自动关闭旧进程再启动 GPU 版。
    [switch]$NoStopExisting
)

# 说明：
# 1. 只双击/运行 run.exe 不一定会启用 GPU。
# 2. VOICEVOX Engine 必须显式带上 --use_gpu，/synthesis 才会走 CUDA。
# 3. 本脚本不强制放在固定位置，也不写死 D 盘路径。
# 4. 默认会从当前工作目录、脚本所在目录附近查找 run.exe。
# 5. 如果找到多个 run.exe，会优先选择路径里包含 windows-nvidia 的那个。
# 6. 本脚本只启动 VOICEVOX，不启动 Mirdo Server，也不启动 Godot。

$ErrorActionPreference = "Stop"

function Add-UniquePath {
    param(
        [System.Collections.Generic.List[string]]$List,
        [string]$PathValue
    )
    if ([string]::IsNullOrWhiteSpace($PathValue)) { return }
    try { $full = [System.IO.Path]::GetFullPath($PathValue) } catch { return }
    if (-not $List.Contains($full)) { $List.Add($full) }
}

function Add-PathWithParents {
    param(
        [System.Collections.Generic.List[string]]$List,
        [string]$StartPath,
        [int]$MaxParents = 3
    )
    if ([string]::IsNullOrWhiteSpace($StartPath)) { return }
    try { $current = [System.IO.DirectoryInfo]::new([System.IO.Path]::GetFullPath($StartPath)) } catch { return }
    for ($i = 0; $i -le $MaxParents -and $null -ne $current; $i++) {
        Add-UniquePath -List $List -PathValue $current.FullName
        $current = $current.Parent
    }
}

function Add-KnownRunExeCandidates {
    param(
        [System.Collections.Generic.List[string]]$List,
        [string]$Root
    )
    Add-UniquePath -List $List -PathValue (Join-Path $Root "run.exe")
    Add-UniquePath -List $List -PathValue (Join-Path $Root "windows-nvidia\run.exe")
    Add-UniquePath -List $List -PathValue (Join-Path $Root "engine\windows-nvidia\run.exe")
    Add-UniquePath -List $List -PathValue (Join-Path $Root "engine-0.25.2\windows-nvidia\run.exe")
    Add-UniquePath -List $List -PathValue (Join-Path $Root "VOICEVOX\engine-0.25.2\windows-nvidia\run.exe")
}

function Find-RunExeNearby {
    param(
        [string]$Root,
        [int]$MaxDepth = 4
    )

    $results = [System.Collections.Generic.List[string]]::new()
    if ([string]::IsNullOrWhiteSpace($Root) -or -not (Test-Path -LiteralPath $Root)) {
        return $results
    }

    $queue = [System.Collections.Generic.Queue[object]]::new()
    $queue.Enqueue([pscustomobject]@{ Path = [System.IO.Path]::GetFullPath($Root); Depth = 0 })

    while ($queue.Count -gt 0) {
        $item = $queue.Dequeue()
        $dir = [string]$item.Path
        $depth = [int]$item.Depth

        $run = Join-Path $dir "run.exe"
        if (Test-Path -LiteralPath $run) {
            Add-UniquePath -List $results -PathValue $run
        }

        if ($depth -ge $MaxDepth) { continue }

        try {
            Get-ChildItem -LiteralPath $dir -Directory -ErrorAction SilentlyContinue |
                ForEach-Object { $queue.Enqueue([pscustomobject]@{ Path = $_.FullName; Depth = $depth + 1 }) }
        }
        catch {
            continue
        }
    }

    return $results
}

function Resolve-VoicevoxRunExe {
    param([string]$ManualEngineDir)

    $baseRoots = [System.Collections.Generic.List[string]]::new()
    if (-not [string]::IsNullOrWhiteSpace($ManualEngineDir)) {
        Add-UniquePath -List $baseRoots -PathValue $ManualEngineDir
    }
    Add-UniquePath -List $baseRoots -PathValue ((Get-Location).Path)
    Add-UniquePath -List $baseRoots -PathValue $PSScriptRoot

    $candidateRoots = [System.Collections.Generic.List[string]]::new()
    foreach ($root in $baseRoots) {
        Add-PathWithParents -List $candidateRoots -StartPath $root -MaxParents 3
    }

    $found = [System.Collections.Generic.List[string]]::new()

    # 父目录只检查常见结构，不递归，避免扫到整个磁盘。
    foreach ($root in $candidateRoots) {
        Add-KnownRunExeCandidates -List $found -Root $root
    }

    # 当前目录/脚本目录/手动目录附近做有限深度查找 run.exe。
    foreach ($root in $baseRoots) {
        $nearby = Find-RunExeNearby -Root $root -MaxDepth 5
        foreach ($path in $nearby) { Add-UniquePath -List $found -PathValue $path }
    }

    $existing = @($found | Where-Object { Test-Path -LiteralPath $_ })
    if ($existing.Count -eq 0) {
        throw @"
未找到 VOICEVOX 的 run.exe。
脚本不会强制指定位置；请在 VOICEVOX 目录附近运行，或手动传入 -EngineDir。
示例：
  powershell -ExecutionPolicy Bypass -File .\start_voicevox_gpu.ps1 -EngineDir "C:\path\to\voicevox"
"@
    }

    # 优先选择 GPU 包的 run.exe，其次选择其他 run.exe。
    $gpuRun = $existing | Where-Object { $_ -match "(?i)(^|[\\/])windows-nvidia([\\/])run\.exe$" } | Select-Object -First 1
    if ($gpuRun) { return [System.IO.Path]::GetFullPath([string]$gpuRun) }

    Write-Warning "找到了 run.exe，但路径里没有 windows-nvidia。仍会追加 --use_gpu 启动；如果失败，请确认下载的是 VOICEVOX GPU 版。"
    return [System.IO.Path]::GetFullPath([string]$existing[0])
}

function Get-PortOwnerProcess {
    param([int]$ListenPort)
    $listener = Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $listener) { return $null }
    return Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
}

$runExe = Resolve-VoicevoxRunExe -ManualEngineDir $EngineDir
$runDir = Split-Path -Parent $runExe
$expectedPath = [System.IO.Path]::GetFullPath($runExe)

$owner = Get-PortOwnerProcess -ListenPort $Port
if ($null -ne $owner) {
    $actualPath = ""
    if ($owner.ExecutablePath) { $actualPath = [System.IO.Path]::GetFullPath([string]$owner.ExecutablePath) }
    $commandLine = [string]$owner.CommandLine
    $alreadyGpu = ($actualPath -ieq $expectedPath) -and ($commandLine -match "(?i)(^|\s)--use_gpu(\s|$)")

    if ($alreadyGpu) {
        Write-Host "VOICEVOX GPU Engine 已经在运行：PID=$($owner.ProcessId), http://$HostAddress`:$Port"
        exit 0
    }

    if ($NoStopExisting) {
        throw "端口 $Port 已被占用：PID=$($owner.ProcessId), Path=$actualPath。当前不是本脚本找到的 GPU 引擎，或没有检测到 --use_gpu。请先关闭它，或去掉 -NoStopExisting。"
    }

    Write-Host "关闭旧的 VOICEVOX/占用进程：PID=$($owner.ProcessId), Path=$actualPath"
    Stop-Process -Id $owner.ProcessId -Force
    Start-Sleep -Seconds 2
}

$argsList = @("--host", $HostAddress, "--port", "$Port", "--use_gpu", "--output_log_utf8")

Write-Host "启动 VOICEVOX GPU Engine：$runExe"
Write-Host "参数：$($argsList -join ' ')"
Start-Process -FilePath $runExe -ArgumentList $argsList -WorkingDirectory $runDir -WindowStyle Hidden | Out-Null

$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $version = Invoke-RestMethod "http://$HostAddress`:$Port/version" -TimeoutSec 2
        $devices = Invoke-RestMethod "http://$HostAddress`:$Port/supported_devices" -TimeoutSec 2
        Write-Host "VOICEVOX Engine 已启动：version=$version, cuda=$($devices.cuda), cpu=$($devices.cpu)"
        Write-Host "检查地址：http://$HostAddress`:$Port/docs"
        $ready = $true
        break
    }
    catch { Start-Sleep -Seconds 1 }
}

if (-not $ready) {
    throw "VOICEVOX Engine 启动超时，请检查是否被杀毒软件拦截，或手动运行：`"$runExe`" --host $HostAddress --port $Port --use_gpu --output_log_utf8"
}
