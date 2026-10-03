param(
    [Parameter(Mandatory=$true)][ValidateSet('status','enable','start','disable','remove')][string]$Mode,
    [Parameter(Mandatory=$true)][string]$TaskName,
    [Parameter(Mandatory=$true)][string]$Pythonw,
    [Parameter(Mandatory=$true)][string]$Worker,
    [Parameter(Mandatory=$true)][string]$SettingsPath,
    [switch]$Packaged
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
if ($TaskName -notmatch '^Biandengbao-LAN-[a-f0-9]{12}$') { throw 'Invalid task name.' }
foreach ($path in @($Pythonw, $Worker, $SettingsPath)) {
    if (![IO.Path]::IsPathRooted($path) -or $path.Contains('"')) { throw 'Invalid task path.' }
}
$arguments = "-B `"$Worker`" --settings `"$SettingsPath`""
if ($Packaged) { $arguments = "--worker `"$SettingsPath`"" }
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task -and ($task.Actions.Count -ne 1 -or
        $task.Actions[0].Execute -ne $Pythonw -or
        $task.Actions[0].Arguments -ne $arguments)) {
    throw 'A different task owns this name; no changes made.'
}
switch ($Mode) {
    'enable' {
        if (!(Test-Path -LiteralPath $Pythonw -PathType Leaf) -or
                (!$Packaged -and !(Test-Path -LiteralPath $Worker -PathType Leaf)) -or
                !(Test-Path -LiteralPath $SettingsPath -PathType Leaf)) {
            throw 'Autostart files are missing.'
        }
        $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $action = New-ScheduledTaskAction -Execute $Pythonw -Argument $arguments -WorkingDirectory ([IO.Path]::GetDirectoryName($Worker))
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        if ($task) {
            Set-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
            Enable-ScheduledTask -TaskName $TaskName | Out-Null
        } else {
            Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Optional password-protected LAN gateway. Wait for the existing Codex App; never launch or stop it.' | Out-Null
        }
    }
    'start' {
        if (!$task) { throw 'Autostart task is missing.' }
        Start-ScheduledTask -TaskName $TaskName
    }
    'disable' {
        if ($task) { Disable-ScheduledTask -TaskName $TaskName | Out-Null }
    }
    'remove' {
        if ($task -and $task.State -eq 'Running') { throw 'Wait for the worker to exit before removal.' }
        if ($task) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
    }
}
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$lastResult = $null
if ($task) { $lastResult = (Get-ScheduledTaskInfo -TaskName $TaskName).LastTaskResult }
[PSCustomObject]@{
    exists = [bool]$task
    enabled = [bool]($task -and $task.Settings.Enabled)
    state = $(if ($task) { $task.State.ToString() } else { 'NotInstalled' })
    lastResult = $lastResult
} | ConvertTo-Json -Compress
