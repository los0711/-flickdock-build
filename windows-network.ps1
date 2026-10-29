# Only the two executables in this specific installation are eligible.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$InstallDirectory,[ValidateSet('Repair','Remove')][string]$Action='Repair',[switch]$Quiet)
$ErrorActionPreference='Stop'
try {
    $appRoot=[IO.Path]::GetFullPath($InstallDirectory)
    $appExe=Join-Path $appRoot 'FlickDock.exe'
    $nodeExe=Join-Path $appRoot '_internal\p2p\runtime\node.exe'
    $policy=New-Object -ComObject HNetCfg.FwPolicy2
    $digest=[Security.Cryptography.SHA256]::Create()
    try { $key=([BitConverter]::ToString($digest.ComputeHash([Text.Encoding]::UTF8.GetBytes($appRoot.ToLowerInvariant())))).Replace('-','').Substring(0,16) } finally { $digest.Dispose() }
    $group='FlickDock-'+$key
    if($Action -eq 'Remove') {
        $owned=@($policy.Rules | Where-Object { $_.Grouping -eq $group -and ($_.ApplicationName -ieq $appExe -or $_.ApplicationName -ieq $nodeExe) })
        foreach($rule in $owned) { $policy.Rules.Remove($rule.Name) }
        exit 0
    }
    if (!(Test-Path -LiteralPath $appExe -PathType Leaf) -or !(Test-Path -LiteralPath $nodeExe -PathType Leaf)) { throw '安装文件不完整，请重新安装 FlickDock。' }
    # A user declining Windows' automatic first-listen prompt creates explicit
    # deny rules. Remove denies for these exact paths only. Managed rules cannot
    # be changed here and remain subject to the administrator's policy.
    $blocked=@($policy.Rules | Where-Object { $_.Action -eq 0 -and $_.ApplicationName -and ($_.ApplicationName -ieq $appExe -or $_.ApplicationName -ieq $nodeExe) })
    foreach($rule in $blocked) { try { $policy.Rules.Remove($rule.Name) } catch {} }
    $definitions=@(
        @('LAN-TCP-In',$appExe,6,1,'LocalSubnet','*'),
        @('LAN-UDP-In',$appExe,17,1,'LocalSubnet','*'),
        @('LAN-TCP-Out',$appExe,6,2,'LocalSubnet','*'),
        @('LAN-UDP-Out',$appExe,17,2,'LocalSubnet','*'),
        @('Cloud-HTTPS-Out',$appExe,6,2,'*','443'),
        @('P2P-UDP-In',$nodeExe,17,1,'*','*'),
        @('P2P-UDP-Out',$nodeExe,17,2,'*','*'),
        @('TURN-TLS-Out',$nodeExe,6,2,'*','443')
    )
    foreach($definition in $definitions) {
        $name=$group+'-'+$definition[0]
        try { $policy.Rules.Remove($name) } catch {}
        $rule=New-Object -ComObject HNetCfg.FWRule
        $rule.Name=$name; $rule.Grouping=$group
        $rule.Description='FlickDock 应用专用联网规则；卸载时移除。'
        $rule.ApplicationName=$definition[1]; $rule.Protocol=$definition[2]; $rule.Direction=$definition[3]
        $rule.RemoteAddresses=$definition[4]; $rule.RemotePorts=$definition[5]
        $rule.Profiles=2147483647; $rule.Action=1; $rule.Enabled=$true
        $policy.Rules.Add($rule)
    }
    $remaining=@($policy.Rules | Where-Object { $_.Enabled -and $_.Action -eq 0 -and $_.ApplicationName -and ($_.ApplicationName -ieq $appExe -or $_.ApplicationName -ieq $nodeExe) })
    if($remaining.Count) { throw '仍有系统策略阻止 FlickDock，请联系设备管理员放行。' }
    if(!$Quiet) {
        Add-Type -AssemblyName System.Windows.Forms
        [void][Windows.Forms.MessageBox]::Show('FlickDock 联网规则已更新，请返回软件重试连接。公司策略或安全软件仍可能限制连接。','FlickDock 联网修复')
    }
    exit 0
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    [void][Windows.Forms.MessageBox]::Show(('无法完成联网修复：'+$_.Exception.Message),'FlickDock 联网修复')
    exit 1
}
