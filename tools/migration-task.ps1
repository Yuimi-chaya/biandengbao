param(
    [Parameter(Mandatory=$true)][ValidateSet('status','disable')][string]$Mode,
    [Parameter(Mandatory=$true)][string]$TaskName,
    [Parameter(Mandatory=$true)][string]$Worker
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
if ($TaskName -ne 'Biandengbao-LAN') { throw 'Only the legacy Biandengbao-LAN task is supported.' }
if (![IO.Path]::IsPathRooted($Worker) -or $Worker.Contains('"')) { throw 'Invalid worker path.' }
$task = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue
if ($task) {
    $expected = "-B `"$Worker`""
    if ($task.Actions.Count -ne 1 -or $task.Actions[0].Arguments -ne $expected -or
            [IO.Path]::GetFileName($task.Actions[0].Execute) -ne 'pythonw.exe' -or
            $task.Principal.RunLevel.ToString() -ne 'Limited' -or
            $task.Principal.LogonType.ToString() -ne 'Interactive') {
        throw 'Foreign task ownership; no changes made.'
    }
    $user = [Security.Principal.WindowsIdentity]::GetCurrent()
    $owner = [Security.Principal.NTAccount]::new($task.Principal.UserId)
    try { $sid = $owner.Translate([Security.Principal.SecurityIdentifier]).Value }
    catch { $sid = $task.Principal.UserId }
    if ($sid -ne $user.User.Value) { throw 'Task belongs to another user.' }
    $xml = Export-ScheduledTask -TaskName $TaskName -TaskPath '\'
    if ($Mode -eq 'disable') {
        Disable-ScheduledTask -TaskName $TaskName -TaskPath '\' | Out-Null
        $task = Get-ScheduledTask -TaskName $TaskName -TaskPath '\'
    }
} else { $xml = $null }
@{exists=[bool]$task; enabled=[bool]($task -and $task.Settings.Enabled);
  state=$(if($task){$task.State.ToString()}else{'NotInstalled'}); xml=$xml} | ConvertTo-Json -Compress
