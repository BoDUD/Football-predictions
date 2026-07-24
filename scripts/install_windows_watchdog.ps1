[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "status", "uninstall")]
    [string] $Action = "status",

    [string] $Workspace,

    [string] $PythonPath,

    [string] $CodexExecutable,

    [ValidatePattern("^[0-9a-fA-F]{64}$")]
    [string] $CodexSha256,

    [ValidatePattern("^[^\\/:*?`"<>|]+$")]
    [string] $TaskName = "SoccerPredict-Watchdog"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$watchdogPath = [System.IO.Path]::GetFullPath(
    (Join-Path -Path $PSScriptRoot -ChildPath "soccer_watchdog.py")
)
$skillRoot = [System.IO.Path]::GetFullPath(
    (Split-Path -Parent $PSScriptRoot)
)

if (-not [string]::IsNullOrWhiteSpace($Workspace)) {
    $Workspace = [System.IO.Path]::GetFullPath($Workspace)
}

function Resolve-PythonExecutable {
    param([string] $RequestedPath)

    if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
        $resolved = [System.IO.Path]::GetFullPath($RequestedPath)
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            throw "Python executable does not exist: $resolved"
        }
        return $resolved
    }

    $command = Get-Command -Name "python.exe" -CommandType Application -ErrorAction Stop |
        Select-Object -First 1
    return [System.IO.Path]::GetFullPath($command.Source)
}

function Quote-TaskArgument {
    param([Parameter(Mandatory = $true)][string] $Value)
    if ($Value.Contains('"')) {
        throw "Task argument paths must not contain a double quote: $Value"
    }
    return '"' + $Value + '"'
}

function Get-WatchdogStatus {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        return [pscustomobject]@{
            Installed = $false
            TaskName = $TaskName
            Workspace = $Workspace
        }
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    return [pscustomobject]@{
        Installed = $true
        TaskName = $TaskName
        State = [string] $task.State
        LastRunTime = $info.LastRunTime
        LastTaskResult = $info.LastTaskResult
        NextRunTime = $info.NextRunTime
        Execute = $task.Actions.Execute
        Arguments = $task.Actions.Arguments
        UserId = $task.Principal.UserId
        LogonType = [string] $task.Principal.LogonType
        Workspace = $Workspace
    }
}

switch ($Action) {
    "status" {
        Get-WatchdogStatus
        break
    }

    "uninstall" {
        $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -ne $existing) {
            # Exact task name only: no wildcard or broad task-folder deletion.
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
        [pscustomobject]@{
            Installed = $false
            TaskName = $TaskName
            Removed = ($null -ne $existing)
        }
        break
    }

    "install" {
        if ([string]::IsNullOrWhiteSpace($Workspace)) {
            throw "install requires an explicit -Workspace path"
        }
        if (-not (Test-Path -LiteralPath $watchdogPath -PathType Leaf)) {
            throw "Watchdog script does not exist: $watchdogPath"
        }
        if (-not (Test-Path -LiteralPath $Workspace -PathType Container)) {
            throw "Workspace does not exist: $Workspace"
        }
        $pythonExe = Resolve-PythonExecutable -RequestedPath $PythonPath

        if (
            [string]::IsNullOrWhiteSpace($CodexExecutable) -xor
            [string]::IsNullOrWhiteSpace($CodexSha256)
        ) {
            throw "-CodexExecutable and -CodexSha256 must be supplied together"
        }

        $arguments = @(
            (Quote-TaskArgument $watchdogPath),
            "--workspace",
            (Quote-TaskArgument $Workspace),
            "--skill-dir",
            (Quote-TaskArgument $skillRoot),
            "--python-executable",
            (Quote-TaskArgument $pythonExe)
        )
        if (-not [string]::IsNullOrWhiteSpace($CodexExecutable)) {
            $codexPath = [System.IO.Path]::GetFullPath($CodexExecutable)
            if (-not (Test-Path -LiteralPath $codexPath -PathType Leaf)) {
                throw "Codex executable does not exist: $codexPath"
            }
            if ([System.IO.Path]::GetFileName($codexPath) -ine "Codex.exe") {
                throw "Codex executable filename must be Codex.exe"
            }
            $actualHash = (Get-FileHash -LiteralPath $codexPath -Algorithm SHA256).Hash
            if ($actualHash -ine $CodexSha256) {
                throw "Codex executable SHA-256 does not match -CodexSha256"
            }
            $arguments += @(
                "--codex-executable",
                (Quote-TaskArgument $codexPath),
                "--codex-sha256",
                $CodexSha256.ToLowerInvariant()
            )
        }

        $taskAction = New-ScheduledTaskAction `
            -Execute $pythonExe `
            -Argument ($arguments -join " ") `
            -WorkingDirectory $skillRoot

        $repeatTrigger = New-ScheduledTaskTrigger `
            -Once `
            -At ((Get-Date).AddMinutes(1)) `
            -RepetitionInterval (New-TimeSpan -Minutes 5) `
            -RepetitionDuration (New-TimeSpan -Days 3650)
        $userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
        $principal = New-ScheduledTaskPrincipal `
            -UserId $userId `
            -LogonType Interactive `
            -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
            -WakeToRun `
            -MultipleInstances IgnoreNew
        $definition = New-ScheduledTask `
            -Action $taskAction `
            -Trigger @($repeatTrigger, $logonTrigger) `
            -Principal $principal `
            -Settings $settings `
            -Description (
                "Every five minutes, sync and persist due soccer-predict lineup, " +
                "review, and cleanup events, then wake the verified Codex app. " +
                "It does not fabricate analysis."
            )

        Register-ScheduledTask `
            -TaskName $TaskName `
            -InputObject $definition `
            -Force | Out-Null

        Get-WatchdogStatus
        break
    }
}
