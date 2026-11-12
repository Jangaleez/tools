<#
    PC Care Kit - a home-grown PC health and maintenance app for Windows 11.
    Uses only components that ship with Windows (PowerShell 5.1, WPF, winget).
    Start it with "Start PC Care Kit.bat".

    Safety rules this app follows:
      - Nothing is deleted or changed without you clicking a button and confirming.
      - Temp cleanup only removes files older than 24 hours.
      - Startup changes use the same on/off switch as Task Manager, so they are reversible.
      - No registry cleaning, no service tweaks, no driver changes.
#>
param(
    [switch]$Background,
    [switch]$NoElevate
)

$ErrorActionPreference = 'Continue'
$AppName    = 'PC Care Kit'
$TaskName   = 'PC Care Kit Weekly Check'
$DataDir    = Join-Path $env:LOCALAPPDATA 'PCCareKit'
$ReportFile = Join-Path $DataDir 'last-check.json'
if (-not (Test-Path -LiteralPath $DataDir)) { New-Item -ItemType Directory -Path $DataDir -Force | Out-Null }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Ask for admin rights once at start (needed for drive wear data, Windows temp, all-user startup items).
if (-not $Background -and -not $NoElevate -and -not (Test-Admin)) {
    try {
        Start-Process powershell.exe -Verb RunAs -ErrorAction Stop -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`""
        exit
    } catch {
        # UAC was declined: carry on with limited rights.
    }
}

# ---------------------------------------------------------------- helpers

function Format-Size([double]$Bytes) {
    if ($Bytes -ge 1GB) { return ('{0:N1} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N0} MB' -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ('{0:N0} KB' -f ($Bytes / 1KB)) }
    return ('{0:N0} bytes' -f $Bytes)
}

function Update-UI {
    if ($script:Window) {
        $script:Window.Dispatcher.Invoke([Action]{}, [System.Windows.Threading.DispatcherPriority]::Background)
    }
}

function Get-FolderSize([string]$Path) {
    $total = [long]0
    $count = 0
    $stack = New-Object System.Collections.Stack
    $stack.Push($Path)
    while ($stack.Count -gt 0) {
        $p = $stack.Pop()
        try {
            $di = New-Object System.IO.DirectoryInfo($p)
            foreach ($f in $di.GetFiles()) { $total += $f.Length }
            foreach ($d in $di.GetDirectories()) {
                # Skip junctions and symlinks so we never loop or double count.
                if (-not ($d.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { $stack.Push($d.FullName) }
            }
        } catch { }
        $count++
        if (($count % 200) -eq 0) { Update-UI }
    }
    return $total
}

function New-Row($Area, $Item, $Value, $Status = 'OK', $Advice = '') {
    [pscustomobject]@{ Area = $Area; Item = $Item; Value = "$Value"; Status = $Status; Advice = $Advice }
}

# ---------------------------------------------------------------- startup items

function Get-ApprovedState([string]$ApprovedPath, [string]$Name) {
    try {
        $k = Get-Item -LiteralPath $ApprovedPath -ErrorAction Stop
        $v = $k.GetValue($Name)
        if ($v -and (($v[0] % 2) -eq 1)) { return 'Disabled' }
    } catch { }
    return 'Enabled'
}

function Get-StartupItems {
    $items = @()
    $regSources = @(
        @{ Path = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'; Approved = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'; Scope = 'This user' },
        @{ Path = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run'; Approved = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'; Scope = 'All users' },
        @{ Path = 'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run'; Approved = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run32'; Scope = 'All users (32-bit)' }
    )
    foreach ($s in $regSources) {
        try {
            $key = Get-Item -LiteralPath $s.Path -ErrorAction Stop
            foreach ($name in $key.GetValueNames()) {
                if ([string]::IsNullOrEmpty($name)) { continue }
                $items += [pscustomobject]@{
                    Name     = $name
                    State    = (Get-ApprovedState $s.Approved $name)
                    Scope    = $s.Scope
                    Command  = "$($key.GetValue($name))"
                    Approved = $s.Approved
                }
            }
        } catch { }
    }
    $folderSources = @(
        @{ Path = [Environment]::GetFolderPath('Startup'); Approved = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder'; Scope = 'This user (Startup folder)' },
        @{ Path = [Environment]::GetFolderPath('CommonStartup'); Approved = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder'; Scope = 'All users (Startup folder)' }
    )
    foreach ($s in $folderSources) {
        if (-not $s.Path -or -not (Test-Path -LiteralPath $s.Path)) { continue }
        foreach ($f in (Get-ChildItem -LiteralPath $s.Path -File -Force -ErrorAction SilentlyContinue)) {
            if ($f.Name -ieq 'desktop.ini') { continue }
            $items += [pscustomobject]@{
                Name     = $f.Name
                State    = (Get-ApprovedState $s.Approved $f.Name)
                Scope    = $s.Scope
                Command  = $f.FullName
                Approved = $s.Approved
            }
        }
    }
    return @($items | Sort-Object State, Name)
}

function Set-StartupState($Item, [bool]$Enable) {
    if (-not (Test-Path -LiteralPath $Item.Approved)) { New-Item -Path $Item.Approved -Force -ErrorAction Stop | Out-Null }
    if ($Enable) {
        [byte[]]$bytes = 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
    } else {
        [byte[]]$bytes = @(3, 0, 0, 0) + [BitConverter]::GetBytes([DateTime]::UtcNow.ToFileTimeUtc())
    }
    New-ItemProperty -LiteralPath $Item.Approved -Name $Item.Name -PropertyType Binary -Value $bytes -Force -ErrorAction Stop | Out-Null
}

# ---------------------------------------------------------------- software updates (winget)

function Get-WingetUpgradeText {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        return 'winget was not found. Install "App Installer" from the Microsoft Store to use this tab.'
    }
    try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
    $raw = (& winget upgrade --source winget --disable-interactivity 2>&1 | Out-String)
    $lines = $raw -split "`r?`n" | Where-Object {
        ($_ -notmatch '^\s*[-\\|/]\s*$') -and ($_ -notmatch '[\u2580-\u259F]')
    }
    return (($lines -join "`r`n").Trim())
}

function Get-UpgradeCount([string]$Text) {
    if ($Text -match '(\d+)\s+upgrades?\s+available') { return [int]$Matches[1] }
    $lines = $Text -split "`r?`n"
    $n = 0; $started = $false
    foreach ($l in $lines) {
        if ($l -match '^-{5,}') { $started = $true; continue }
        if ($started) {
            if ([string]::IsNullOrWhiteSpace($l)) { break }
            $n++
        }
    }
    return $n
}

# ---------------------------------------------------------------- health data

function Get-HealthData([switch]$IncludeUpdates) {
    $rows = @()

    # Windows, uptime, pending restart
    try {
        $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        $rows += New-Row 'System' 'Windows' "$($os.Caption) (build $($os.BuildNumber))" 'Info'
        $up = (Get-Date) - $os.LastBootUpTime
        if ($up.TotalDays -gt 7) {
            $rows += New-Row 'System' 'Time since last restart' ('{0:N0} days' -f $up.TotalDays) 'Warning' 'Restart the PC. A restart clears memory leaks and finishes pending updates.'
        } else {
            $rows += New-Row 'System' 'Time since last restart' ('{0:N1} days' -f $up.TotalDays)
        }
        $totalGB = $os.TotalVisibleMemorySize / 1MB
        $usedPct = 100 * (1 - ($os.FreePhysicalMemory / $os.TotalVisibleMemorySize))
        $memText = '{0:N0}% of {1:N1} GB in use' -f $usedPct, $totalGB
        if ($usedPct -gt 85) {
            $rows += New-Row 'Memory' 'RAM' $memText 'Warning' 'Memory is nearly full. Close unused apps and browser tabs, and trim the Startup tab.'
        } elseif ($totalGB -lt 7.5) {
            $rows += New-Row 'Memory' 'RAM' $memText 'Info' 'Under 8 GB of RAM. Adding memory will help more than any software change.'
        } else {
            $rows += New-Row 'Memory' 'RAM' $memText
        }
    } catch { $rows += New-Row 'System' 'Windows' 'Could not read' 'Info' }

    $pending = (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') -or
               (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending')
    if ($pending) {
        $rows += New-Row 'System' 'Restart pending' 'Yes' 'Warning' 'Windows is waiting for a restart to finish installing updates.'
    } else {
        $rows += New-Row 'System' 'Restart pending' 'No'
    }

    # CPU
    try {
        $cpu = Get-CimInstance Win32_Processor -ErrorAction Stop | Select-Object -First 1
        $rows += New-Row 'Processor' $cpu.Name.Trim() "$($cpu.LoadPercentage)% load right now" 'Info'
    } catch { }

    # Volumes
    try {
        foreach ($v in (Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' -ErrorAction Stop)) {
            if (-not $v.Size) { continue }
            $freePct = 100 * $v.FreeSpace / $v.Size
            $text = '{0} free of {1} ({2:N0}%)' -f (Format-Size $v.FreeSpace), (Format-Size $v.Size), $freePct
            if ($freePct -lt 10) {
                $rows += New-Row 'Storage' "Drive $($v.DeviceID)" $text 'Problem' "Drive $($v.DeviceID) is almost full. Use the Cleanup and Disk Space tabs to free room."
            } elseif ($freePct -lt 20) {
                $rows += New-Row 'Storage' "Drive $($v.DeviceID)" $text 'Warning' "Drive $($v.DeviceID) is getting full. Use the Cleanup and Disk Space tabs."
            } else {
                $rows += New-Row 'Storage' "Drive $($v.DeviceID)" $text
            }
        }
    } catch { }

    # Physical disks
    try {
        foreach ($d in (Get-PhysicalDisk -ErrorAction Stop)) {
            $extra = ''
            try {
                $rc = $d | Get-StorageReliabilityCounter -ErrorAction Stop
                if ($null -ne $rc.Wear) { $extra += ", wear $($rc.Wear)%" }
                if ($rc.Temperature) { $extra += ", $($rc.Temperature) C" }
            } catch { }
            $text = "$($d.MediaType), $(Format-Size $d.Size), health: $($d.HealthStatus)$extra"
            if ("$($d.HealthStatus)" -ne 'Healthy') {
                $rows += New-Row 'Storage' $d.FriendlyName $text 'Problem' "Drive '$($d.FriendlyName)' reports a health problem. Back up your data now and plan to replace it."
            } elseif ("$($d.MediaType)" -eq 'HDD') {
                $rows += New-Row 'Storage' $d.FriendlyName $text 'Info' 'This is a hard disk. If Windows boots from it, moving to an SSD is the biggest speed upgrade available.'
            } else {
                $rows += New-Row 'Storage' $d.FriendlyName $text
            }
        }
    } catch { }

    # Battery wear (laptops)
    try {
        $full   = (Get-CimInstance -Namespace root\wmi -ClassName BatteryFullChargedCapacity -ErrorAction Stop | Select-Object -First 1).FullChargedCapacity
        $design = (Get-CimInstance -Namespace root\wmi -ClassName BatteryStaticData -ErrorAction Stop | Select-Object -First 1).DesignedCapacity
        if ($full -and $design) {
            $pct = 100 * $full / $design
            $text = '{0:N0}% of original capacity' -f $pct
            if ($pct -lt 60) {
                $rows += New-Row 'Battery' 'Battery health' $text 'Warning' 'The battery holds much less charge than when new. Consider a replacement.'
            } else {
                $rows += New-Row 'Battery' 'Battery health' $text
            }
        }
    } catch { }

    # Windows Security (Defender)
    try {
        $mp = Get-MpComputerStatus -ErrorAction Stop
        if (-not $mp.RealTimeProtectionEnabled) {
            $rows += New-Row 'Security' 'Real-time protection' 'Off' 'Problem' 'Real-time protection is off. Turn it on in Windows Security (ignore this if another antivirus is installed).'
        } else {
            $rows += New-Row 'Security' 'Real-time protection' 'On'
        }
        if ($mp.AntivirusSignatureAge -gt 7) {
            $rows += New-Row 'Security' 'Virus definitions' "$($mp.AntivirusSignatureAge) days old" 'Warning' 'Virus definitions are out of date. Run Windows Update.'
        } else {
            $rows += New-Row 'Security' 'Virus definitions' "$($mp.AntivirusSignatureAge) days old"
        }
        if ($mp.QuickScanAge -gt 10000) {
            $rows += New-Row 'Security' 'Last quick scan' 'Never' 'Warning' 'No virus scan on record. Run a quick scan in Windows Security.'
        } elseif ($mp.QuickScanAge -gt 14) {
            $rows += New-Row 'Security' 'Last quick scan' "$($mp.QuickScanAge) days ago" 'Warning' 'It has been over two weeks since a virus scan. Run a quick scan in Windows Security.'
        } else {
            $rows += New-Row 'Security' 'Last quick scan' "$($mp.QuickScanAge) days ago"
        }
    } catch { }

    # Event log, last 7 days
    try {
        $events = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; Level = 1, 2; StartTime = (Get-Date).AddDays(-7) } -MaxEvents 500 -ErrorAction SilentlyContinue)
        $rows += New-Row 'Reliability' 'System errors (7 days)' "$($events.Count)" 'Info'
        $diskErr = @($events | Where-Object { $_.ProviderName -match '^(disk|Ntfs|volmgr|stornvme|storahci)$' }).Count
        $crashes = @($events | Where-Object { $_.ProviderName -eq 'Microsoft-Windows-WER-SystemErrorReporting' -and $_.Id -eq 1001 }).Count
        $power   = @($events | Where-Object { $_.ProviderName -eq 'Microsoft-Windows-Kernel-Power' -and $_.Id -eq 41 }).Count
        if ($diskErr -gt 0) {
            $rows += New-Row 'Reliability' 'Disk errors (7 days)' "$diskErr" 'Warning' 'Windows logged disk errors this week. Back up important files and run the disk check on the Maintenance tab.'
        } else { $rows += New-Row 'Reliability' 'Disk errors (7 days)' '0' }
        if ($crashes -gt 0) {
            $rows += New-Row 'Reliability' 'Blue screen crashes (7 days)' "$crashes" 'Warning' 'Windows crashed this week. Run the system file check on the Maintenance tab and install pending updates.'
        } else { $rows += New-Row 'Reliability' 'Blue screen crashes (7 days)' '0' }
        if ($power -gt 0) {
            $rows += New-Row 'Reliability' 'Unexpected shutdowns (7 days)' "$power" 'Warning' 'The PC lost power or was forced off this week. If you did not do that yourself, check the power supply and temperatures.'
        } else { $rows += New-Row 'Reliability' 'Unexpected shutdowns (7 days)' '0' }
    } catch { }

    # Temp files
    try {
        $tempSize = Get-FolderSize $env:TEMP
        if ($tempSize -gt 1GB) {
            $rows += New-Row 'Housekeeping' 'Temporary files' (Format-Size $tempSize) 'Warning' 'Over 1 GB of temporary files. Clear them on the Cleanup tab.'
        } else {
            $rows += New-Row 'Housekeeping' 'Temporary files' (Format-Size $tempSize)
        }
    } catch { }

    # Startup apps
    try {
        $enabled = @(Get-StartupItems | Where-Object { $_.State -eq 'Enabled' }).Count
        if ($enabled -gt 10) {
            $rows += New-Row 'Housekeeping' 'Apps starting with Windows' "$enabled" 'Warning' 'Many apps start with Windows. Disable the ones you do not need on the Startup tab.'
        } else {
            $rows += New-Row 'Housekeeping' 'Apps starting with Windows' "$enabled"
        }
    } catch { }

    # Software updates (slow, so only when asked)
    if ($IncludeUpdates) {
        try {
            $n = Get-UpgradeCount (Get-WingetUpgradeText)
            if ($n -gt 0) {
                $rows += New-Row 'Software' 'App updates available' "$n" 'Warning' "$n app updates are available. Install them from the Updates tab."
            } else {
                $rows += New-Row 'Software' 'App updates available' '0'
            }
        } catch { }
    }

    return @($rows)
}

# ---------------------------------------------------------------- background mode (weekly scheduled check)

if ($Background) {
    $rows = Get-HealthData -IncludeUpdates
    $advice = @($rows | Where-Object { $_.Advice -and $_.Status -ne 'Info' })
    $report = [pscustomobject]@{ Time = (Get-Date).ToString('yyyy-MM-dd HH:mm'); Count = $advice.Count; Suggestions = @($advice | ForEach-Object { $_.Advice }) }
    $report | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ReportFile -Encoding UTF8
    if ($advice.Count -gt 0) {
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $n = New-Object System.Windows.Forms.NotifyIcon
        $n.Icon = [System.Drawing.SystemIcons]::Information
        $n.Visible = $true
        $n.ShowBalloonTip(10000, $AppName, "$($advice.Count) suggestion(s) for your PC. Open PC Care Kit to see them.", [System.Windows.Forms.ToolTipIcon]::Info)
        Start-Sleep -Seconds 12
        $n.Dispose()
    }
    exit
}

# ---------------------------------------------------------------- window

Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName PresentationCore
Add-Type -AssemblyName WindowsBase

[xml]$xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        Title="PC Care Kit" Width="1020" Height="680" MinWidth="820" MinHeight="520"
        WindowStartupLocation="CenterScreen" FontFamily="Segoe UI" FontSize="13" Background="#F4F5F7">
  <Window.Resources>
    <Style TargetType="Button">
      <Setter Property="Padding" Value="12,6"/>
      <Setter Property="Margin" Value="0,0,8,0"/>
    </Style>
    <Style TargetType="DataGrid">
      <Setter Property="AutoGenerateColumns" Value="False"/>
      <Setter Property="IsReadOnly" Value="True"/>
      <Setter Property="HeadersVisibility" Value="Column"/>
      <Setter Property="GridLinesVisibility" Value="Horizontal"/>
      <Setter Property="HorizontalGridLinesBrush" Value="#E3E5E8"/>
      <Setter Property="RowHeight" Value="28"/>
      <Setter Property="SelectionMode" Value="Single"/>
      <Setter Property="Background" Value="White"/>
      <Setter Property="BorderBrush" Value="#D0D3D8"/>
    </Style>
    <Style TargetType="DataGridRow">
      <Style.Triggers>
        <DataTrigger Binding="{Binding Status}" Value="Warning">
          <Setter Property="Background" Value="#FFF3D0"/>
        </DataTrigger>
        <DataTrigger Binding="{Binding Status}" Value="Problem">
          <Setter Property="Background" Value="#FBD5D5"/>
        </DataTrigger>
        <DataTrigger Binding="{Binding State}" Value="Disabled">
          <Setter Property="Foreground" Value="#8A8F98"/>
        </DataTrigger>
      </Style.Triggers>
    </Style>
  </Window.Resources>
  <DockPanel>
    <Border DockPanel.Dock="Bottom" Background="#E7E9ED" Padding="10,5">
      <TextBlock Name="txtStatus" Text="Ready"/>
    </Border>
    <TabControl Margin="10" Padding="12">

      <TabItem Header="Overview">
        <DockPanel>
          <StackPanel DockPanel.Dock="Top" Orientation="Horizontal" Margin="0,0,0,10">
            <Button Name="btnRefresh" Content="Refresh"/>
            <TextBlock Name="txtAdmin" VerticalAlignment="Center" Foreground="#555B66"/>
          </StackPanel>
          <DataGrid Name="gridHealth">
            <DataGrid.Columns>
              <DataGridTextColumn Header="Area" Binding="{Binding Area}" Width="110"/>
              <DataGridTextColumn Header="Item" Binding="{Binding Item}" Width="280"/>
              <DataGridTextColumn Header="Reading" Binding="{Binding Value}" Width="*"/>
              <DataGridTextColumn Header="Status" Binding="{Binding Status}" Width="90"/>
            </DataGrid.Columns>
          </DataGrid>
        </DockPanel>
      </TabItem>

      <TabItem Header="Advisor">
        <DockPanel>
          <TextBlock DockPanel.Dock="Top" TextWrapping="Wrap" Margin="0,0,0,10"
                     Text="Suggestions based on the current state of this PC. Nothing here is applied automatically."/>
          <Border DockPanel.Dock="Bottom" Background="White" BorderBrush="#D0D3D8" BorderThickness="1" Padding="12" Margin="0,10,0,0">
            <StackPanel>
              <TextBlock FontWeight="SemiBold" Text="Weekly background check"/>
              <TextBlock TextWrapping="Wrap" Margin="0,4,0,8"
                         Text="Optional. Once a week (Sunday midday, or the next time the PC is on) it checks health and app updates, then shows a notification only if something needs attention. It never changes anything by itself."/>
              <TextBlock Name="txtTask" Margin="0,0,0,8" Foreground="#555B66"/>
              <StackPanel Orientation="Horizontal">
                <Button Name="btnTaskOn" Content="Turn on weekly check"/>
                <Button Name="btnTaskOff" Content="Turn off"/>
              </StackPanel>
            </StackPanel>
          </Border>
          <DataGrid Name="gridAdvice" RowHeight="NaN">
            <DataGrid.Columns>
              <DataGridTextColumn Header="Level" Binding="{Binding Status}" Width="90"/>
              <DataGridTextColumn Header="About" Binding="{Binding Item}" Width="220"/>
              <DataGridTextColumn Header="Suggestion" Binding="{Binding Advice}" Width="*">
                <DataGridTextColumn.ElementStyle>
                  <Style TargetType="TextBlock">
                    <Setter Property="TextWrapping" Value="Wrap"/>
                    <Setter Property="Margin" Value="4,5"/>
                  </Style>
                </DataGridTextColumn.ElementStyle>
              </DataGridTextColumn>
            </DataGrid.Columns>
          </DataGrid>
        </DockPanel>
      </TabItem>

      <TabItem Header="Startup">
        <DockPanel>
          <TextBlock DockPanel.Dock="Top" TextWrapping="Wrap" Margin="0,0,0,10"
                     Text="Apps that start with Windows. Disabling one here is the same as switching it off in Task Manager, and you can re-enable it at any time. Nothing is deleted."/>
          <StackPanel DockPanel.Dock="Bottom" Orientation="Horizontal" Margin="0,10,0,0">
            <Button Name="btnStartupRefresh" Content="Refresh"/>
            <Button Name="btnStartupDisable" Content="Disable selected"/>
            <Button Name="btnStartupEnable" Content="Enable selected"/>
            <Button Name="btnTaskMgr" Content="Open Task Manager"/>
          </StackPanel>
          <DataGrid Name="gridStartup">
            <DataGrid.Columns>
              <DataGridTextColumn Header="Name" Binding="{Binding Name}" Width="220"/>
              <DataGridTextColumn Header="State" Binding="{Binding State}" Width="80"/>
              <DataGridTextColumn Header="Applies to" Binding="{Binding Scope}" Width="180"/>
              <DataGridTextColumn Header="Command" Binding="{Binding Command}" Width="*"/>
            </DataGrid.Columns>
          </DataGrid>
        </DockPanel>
      </TabItem>

      <TabItem Header="Cleanup">
        <DockPanel>
          <TextBlock DockPanel.Dock="Top" TextWrapping="Wrap" Margin="0,0,0,10"
                     Text="Pick what to clear, press Analyse to see how much space each holds, then Clean. Temporary files newer than 24 hours are left alone, and files in use are skipped. Close your browsers before clearing their caches."/>
          <StackPanel DockPanel.Dock="Bottom" Orientation="Horizontal" Margin="0,10,0,0">
            <Button Name="btnAnalyse" Content="Analyse"/>
            <Button Name="btnClean" Content="Clean selected"/>
            <Button Name="btnStorage" Content="Open Windows Storage settings"/>
          </StackPanel>
          <Border Background="White" BorderBrush="#D0D3D8" BorderThickness="1" Padding="14">
            <StackPanel>
              <StackPanel Name="pnlClean"/>
              <TextBlock Name="txtClean" Margin="0,14,0,0" FontWeight="SemiBold"/>
            </StackPanel>
          </Border>
        </DockPanel>
      </TabItem>

      <TabItem Header="Disk Space">
        <DockPanel>
          <DockPanel DockPanel.Dock="Top" Margin="0,0,0,10">
            <StackPanel DockPanel.Dock="Right" Orientation="Horizontal" Margin="8,0,0,0">
              <Button Name="btnScan" Content="Scan"/>
              <Button Name="btnUp" Content="Up a level"/>
              <Button Name="btnExplore" Content="Open in Explorer" Margin="0"/>
            </StackPanel>
            <TextBox Name="txtPath" VerticalContentAlignment="Center" Padding="4"/>
          </DockPanel>
          <TextBlock DockPanel.Dock="Bottom" Margin="0,8,0,0" Foreground="#555B66"
                     Text="Double-click a folder to look inside it. This tab only measures; it never deletes anything."/>
          <DataGrid Name="gridDisk">
            <DataGrid.Columns>
              <DataGridTextColumn Header="Folder" Binding="{Binding Name}" Width="*"/>
              <DataGridTextColumn Header="Size" Binding="{Binding SizeText}" Width="120"/>
              <DataGridTextColumn Header="Share" Binding="{Binding Share}" Width="80"/>
            </DataGrid.Columns>
          </DataGrid>
        </DockPanel>
      </TabItem>

      <TabItem Header="Updates">
        <DockPanel>
          <TextBlock DockPanel.Dock="Top" TextWrapping="Wrap" Margin="0,0,0,10"
                     Text="Checks your installed apps for newer versions using winget, the package manager built into Windows 11. Update all opens a separate window so you can watch each update install."/>
          <StackPanel DockPanel.Dock="Bottom" Orientation="Horizontal" Margin="0,10,0,0">
            <Button Name="btnCheckUpdates" Content="Check for app updates"/>
            <Button Name="btnUpdateAll" Content="Update all"/>
            <Button Name="btnWinUpdate" Content="Open Windows Update"/>
          </StackPanel>
          <TextBox Name="txtUpdates" IsReadOnly="True" FontFamily="Consolas" FontSize="12"
                   VerticalScrollBarVisibility="Auto" HorizontalScrollBarVisibility="Auto"
                   Text="Press 'Check for app updates' to begin."/>
        </DockPanel>
      </TabItem>

      <TabItem Header="Maintenance">
        <ScrollViewer VerticalScrollBarVisibility="Auto">
          <StackPanel>
            <TextBlock TextWrapping="Wrap" Margin="0,0,0,12"
                       Text="These run Windows' own repair tools. Each opens in its own window so you can read the result, and each asks for administrator permission."/>
            <Grid>
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="230"/>
                <ColumnDefinition Width="*"/>
              </Grid.ColumnDefinitions>
              <Grid.RowDefinitions>
                <RowDefinition Height="Auto"/><RowDefinition Height="Auto"/><RowDefinition Height="Auto"/>
                <RowDefinition Height="Auto"/><RowDefinition Height="Auto"/>
              </Grid.RowDefinitions>
              <Button Grid.Row="0" Name="btnRestore" Content="Create restore point" Margin="0,0,12,10"/>
              <TextBlock Grid.Row="0" Grid.Column="1" TextWrapping="Wrap" VerticalAlignment="Center" Margin="0,0,0,10"
                         Text="Saves a snapshot of system settings you can roll back to. Worth doing before the repairs below. Needs System Protection switched on."/>
              <Button Grid.Row="1" Name="btnSfc" Content="System file check (SFC)" Margin="0,0,12,10"/>
              <TextBlock Grid.Row="1" Grid.Column="1" TextWrapping="Wrap" VerticalAlignment="Center" Margin="0,0,0,10"
                         Text="Checks Windows system files and repairs any that are damaged. About 10 to 20 minutes."/>
              <Button Grid.Row="2" Name="btnDism" Content="Repair Windows image (DISM)" Margin="0,0,12,10"/>
              <TextBlock Grid.Row="2" Grid.Column="1" TextWrapping="Wrap" VerticalAlignment="Center" Margin="0,0,0,10"
                         Text="Repairs the store SFC copies from. Run this if SFC reports problems it could not fix. Needs internet."/>
              <Button Grid.Row="3" Name="btnChkdsk" Content="Disk check (read-only scan)" Margin="0,0,12,10"/>
              <TextBlock Grid.Row="3" Grid.Column="1" TextWrapping="Wrap" VerticalAlignment="Center" Margin="0,0,0,10"
                         Text="Scans the system drive for file system errors while Windows keeps running. It reports problems but does not change the drive."/>
              <Button Grid.Row="4" Name="btnOptimise" Content="Optimise system drive" Margin="0,0,12,10"/>
              <TextBlock Grid.Row="4" Grid.Column="1" TextWrapping="Wrap" VerticalAlignment="Center" Margin="0,0,0,10"
                         Text="Runs TRIM on an SSD or a defrag on a hard disk. Windows picks the right one for the drive type."/>
            </Grid>
          </StackPanel>
        </ScrollViewer>
      </TabItem>

    </TabControl>
  </DockPanel>
</Window>
'@

$script:Window = [Windows.Markup.XamlReader]::Load((New-Object System.Xml.XmlNodeReader $xaml))
foreach ($node in $xaml.SelectNodes('//*[@Name]')) {
    $n = $node.GetAttribute('Name')
    Set-Variable -Name $n -Value $script:Window.FindName($n) -Scope Script
}

$script:Busy = $false
$script:UpgradeCount = $null

function Start-Work([string]$Message) {
    if ($script:Busy) { return $false }
    $script:Busy = $true
    $txtStatus.Text = $Message
    $script:Window.Cursor = [System.Windows.Input.Cursors]::Wait
    Update-UI
    return $true
}

function Stop-Work([string]$Message) {
    $script:Busy = $false
    $txtStatus.Text = $Message
    $script:Window.Cursor = $null
}

function Show-Message([string]$Text) {
    [System.Windows.MessageBox]::Show($script:Window, $Text, $AppName) | Out-Null
}

function Confirm-Action([string]$Text) {
    $r = [System.Windows.MessageBox]::Show($script:Window, $Text, $AppName, [System.Windows.MessageBoxButton]::YesNo, [System.Windows.MessageBoxImage]::Question)
    return ($r -eq [System.Windows.MessageBoxResult]::Yes)
}

# ---------------------------------------------------------------- Overview + Advisor

function Update-TaskText {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $text = 'Weekly check is OFF.'
    if ($task) { $text = 'Weekly check is ON.' }
    if (Test-Path -LiteralPath $ReportFile) {
        try {
            $rep = Get-Content -LiteralPath $ReportFile -Raw | ConvertFrom-Json
            $text += "  Last background check: $($rep.Time), $($rep.Count) suggestion(s)."
        } catch { }
    }
    $txtTask.Text = $text
}

function Update-Health {
    if (-not (Start-Work 'Checking system health...')) { return }
    try {
        $rows = Get-HealthData
        if ($null -ne $script:UpgradeCount) {
            if ($script:UpgradeCount -gt 0) {
                $rows += New-Row 'Software' 'App updates available' "$($script:UpgradeCount)" 'Warning' "$($script:UpgradeCount) app updates are available. Install them from the Updates tab."
            } else {
                $rows += New-Row 'Software' 'App updates available' '0'
            }
        }
        $gridHealth.ItemsSource = @($rows)
        $advice = @($rows | Where-Object { $_.Advice })
        if ($advice.Count -eq 0) {
            $advice = @([pscustomobject]@{ Status = 'OK'; Item = 'Everything'; Advice = 'Nothing needs attention right now.' })
        }
        $order = @{ 'Problem' = 0; 'Warning' = 1; 'Info' = 2; 'OK' = 3 }
        $gridAdvice.ItemsSource = @($advice | Sort-Object { $order[$_.Status] })
        Update-TaskText
        $need = @($rows | Where-Object { $_.Status -eq 'Problem' -or $_.Status -eq 'Warning' }).Count
        Stop-Work "Health check finished at $((Get-Date).ToString('HH:mm')). $need item(s) need attention."
    } catch {
        Stop-Work 'Health check failed.'
        Show-Message $_.Exception.Message
    }
}

$btnRefresh.Add_Click({ Update-Health })

$btnTaskOn.Add_Click({
    try {
        $arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" -Background"
        $action   = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
        $trigger  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '12:00'
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description 'Weekly PC Care Kit health and update check. Reports only; changes nothing.' -Force -ErrorAction Stop | Out-Null
        Update-TaskText
        $txtStatus.Text = 'Weekly check turned on.'
    } catch { Show-Message "Could not create the scheduled task: $($_.Exception.Message)" }
})

$btnTaskOff.Add_Click({
    try {
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        }
        Update-TaskText
        $txtStatus.Text = 'Weekly check turned off.'
    } catch { Show-Message "Could not remove the scheduled task: $($_.Exception.Message)" }
})

# ---------------------------------------------------------------- Startup

function Update-Startup { $gridStartup.ItemsSource = @(Get-StartupItems) }

function Set-SelectedStartup([bool]$Enable) {
    $item = $gridStartup.SelectedItem
    if (-not $item) { Show-Message 'Select a startup item first.'; return }
    try {
        Set-StartupState $item $Enable
        Update-Startup
        $word = 'disabled'; if ($Enable) { $word = 'enabled' }
        $txtStatus.Text = "'$($item.Name)' $word. This takes effect the next time you sign in."
    } catch {
        Show-Message "Could not change '$($item.Name)'. Items that apply to all users need the app to be running as administrator.`n`n$($_.Exception.Message)"
    }
}

$btnStartupRefresh.Add_Click({ Update-Startup })
$btnStartupDisable.Add_Click({ Set-SelectedStartup $false })
$btnStartupEnable.Add_Click({ Set-SelectedStartup $true })
$btnTaskMgr.Add_Click({ Start-Process taskmgr.exe })

# ---------------------------------------------------------------- Cleanup

$local = $env:LOCALAPPDATA
$script:CleanItems = @(
    @{ Label = 'Your temporary files';          Type = 'files';   Paths = @($env:TEMP);                                                   AgeHours = 24; Default = $true  },
    @{ Label = 'Windows temporary files';       Type = 'files';   Paths = @((Join-Path $env:WINDIR 'Temp'));                              AgeHours = 24; Default = $true  },
    @{ Label = 'Microsoft Edge cache';          Type = 'files';   Paths = @((Join-Path $local 'Microsoft\Edge\User Data\Default\Cache')); AgeHours = 0;  Default = $false },
    @{ Label = 'Google Chrome cache';           Type = 'files';   Paths = @((Join-Path $local 'Google\Chrome\User Data\Default\Cache'));  AgeHours = 0;  Default = $false },
    @{ Label = 'Firefox cache';                 Type = 'firefox'; Paths = @((Join-Path $local 'Mozilla\Firefox\Profiles'));               AgeHours = 0;  Default = $false },
    @{ Label = 'Recycle Bin';                   Type = 'recycle'; Paths = @();                                                            AgeHours = 0;  Default = $false }
)
foreach ($ci in $script:CleanItems) {
    $cb = New-Object System.Windows.Controls.CheckBox
    $cb.Content = $ci.Label
    $cb.IsChecked = $ci.Default
    $cb.Margin = '0,0,0,8'
    $pnlClean.Children.Add($cb) | Out-Null
    $ci.Box = $cb
}

function Get-CleanTargets($Item) {
    $paths = @($Item.Paths)
    if ($Item.Type -eq 'firefox') {
        $paths = @()
        foreach ($root in $Item.Paths) {
            if (Test-Path -LiteralPath $root) {
                foreach ($prof in (Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue)) {
                    $paths += (Join-Path $prof.FullName 'cache2')
                }
            }
        }
    }
    $cutoff = (Get-Date).AddHours(-1 * $Item.AgeHours)
    $files = @()
    foreach ($p in $paths) {
        if (-not (Test-Path -LiteralPath $p)) { continue }
        $files += @(Get-ChildItem -LiteralPath $p -Recurse -Force -File -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt $cutoff })
        Update-UI
    }
    return @($files)
}

function Get-RecycleBinSize {
    $size = [long]0
    try {
        $shell = New-Object -ComObject Shell.Application
        foreach ($i in $shell.Namespace(10).Items()) { $size += [long]$i.Size }
    } catch { }
    return $size
}

$btnAnalyse.Add_Click({
    if (-not (Start-Work 'Measuring what can be cleaned...')) { return }
    $total = [long]0
    foreach ($ci in $script:CleanItems) {
        $size = [long]0
        if ($ci.Type -eq 'recycle') { $size = Get-RecycleBinSize }
        else {
            $m = Get-CleanTargets $ci | Measure-Object -Property Length -Sum
            if ($m.Sum) { $size = [long]$m.Sum }
        }
        $ci.Box.Content = "$($ci.Label)  -  $(Format-Size $size)"
        if ($ci.Box.IsChecked) { $total += $size }
        Update-UI
    }
    $txtClean.Text = "Ticked items hold about $(Format-Size $total)."
    Stop-Work 'Analysis finished.'
})

$btnClean.Add_Click({
    $chosen = @($script:CleanItems | Where-Object { $_.Box.IsChecked })
    if ($chosen.Count -eq 0) { Show-Message 'Tick at least one item first.'; return }
    $names = ($chosen | ForEach-Object { '  - ' + $_.Label }) -join "`n"
    if (-not (Confirm-Action "Delete the contents of:`n`n$names`n`nThis cannot be undone. Continue?")) { return }
    if (-not (Start-Work 'Cleaning...')) { return }
    $freed = [long]0; $skipped = 0
    foreach ($ci in $chosen) {
        if ($ci.Type -eq 'recycle') {
            $before = Get-RecycleBinSize
            try { Clear-RecycleBin -Force -ErrorAction Stop; $freed += $before } catch { }
        } else {
            foreach ($f in (Get-CleanTargets $ci)) {
                try {
                    $len = $f.Length
                    Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop
                    $freed += $len
                } catch { $skipped++ }
            }
        }
        $ci.Box.Content = $ci.Label
        Update-UI
    }
    $txtClean.Text = "Freed $(Format-Size $freed). $skipped file(s) were in use and left alone."
    Stop-Work 'Cleanup finished.'
})

$btnStorage.Add_Click({ Start-Process 'ms-settings:storagesense' })

# ---------------------------------------------------------------- Disk Space

function Invoke-DiskScan([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Container)) { Show-Message 'That folder does not exist.'; return }
    if (-not (Start-Work "Measuring $Path ...")) { return }
    try {
        $txtPath.Text = $Path
        $rows = @()
        $di = New-Object System.IO.DirectoryInfo($Path)
        $own = [long]0
        try { foreach ($f in $di.GetFiles()) { $own += $f.Length } } catch { }
        $dirs = @()
        try { $dirs = @($di.GetDirectories() | Where-Object { -not ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) }) } catch { }
        $i = 0
        foreach ($d in $dirs) {
            $i++
            $txtStatus.Text = "Measuring $($d.Name) ($i of $($dirs.Count))..."
            Update-UI
            $rows += [pscustomobject]@{ Name = $d.Name; Path = $d.FullName; Size = (Get-FolderSize $d.FullName); SizeText = ''; Share = '' }
        }
        if ($own -gt 0) { $rows += [pscustomobject]@{ Name = '(files directly in this folder)'; Path = $null; Size = $own; SizeText = ''; Share = '' } }
        $total = ($rows | Measure-Object -Property Size -Sum).Sum
        foreach ($r in $rows) {
            $r.SizeText = Format-Size $r.Size
            if ($total -gt 0) { $r.Share = '{0:N0}%' -f (100 * $r.Size / $total) }
        }
        $gridDisk.ItemsSource = @($rows | Sort-Object Size -Descending)
        if (-not $total) { $total = 0 }
        Stop-Work "$Path holds $(Format-Size $total)."
    } catch {
        Stop-Work 'Scan failed.'
        Show-Message $_.Exception.Message
    }
}

$txtPath.Text = $env:USERPROFILE
$btnScan.Add_Click({ Invoke-DiskScan $txtPath.Text })
$btnUp.Add_Click({
    $parent = Split-Path -Path $txtPath.Text -Parent
    if ($parent) { Invoke-DiskScan $parent }
})
$btnExplore.Add_Click({ if (Test-Path -LiteralPath $txtPath.Text) { Start-Process explorer.exe -ArgumentList "`"$($txtPath.Text)`"" } })
$gridDisk.Add_MouseDoubleClick({
    $row = $gridDisk.SelectedItem
    if ($row -and $row.Path) { Invoke-DiskScan $row.Path }
})

# ---------------------------------------------------------------- Updates

$btnCheckUpdates.Add_Click({
    if (-not (Start-Work 'Checking for app updates (this can take half a minute)...')) { return }
    try {
        $text = Get-WingetUpgradeText
        $txtUpdates.Text = $text
        $script:UpgradeCount = Get-UpgradeCount $text
        Stop-Work "$($script:UpgradeCount) app update(s) available."
    } catch {
        Stop-Work 'Update check failed.'
        Show-Message $_.Exception.Message
    }
})

$btnUpdateAll.Add_Click({
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { Show-Message 'winget was not found on this PC.'; return }
    if (-not (Confirm-Action 'This opens a window and installs every available app update. Apps being updated may close. Continue?')) { return }
    Start-Process powershell.exe -ArgumentList '-NoExit -NoProfile -Command "winget upgrade --all --source winget; Write-Host; Write-Host Finished. You can close this window."'
})

$btnWinUpdate.Add_Click({ Start-Process 'ms-settings:windowsupdate' })

# ---------------------------------------------------------------- Maintenance

function Start-AdminConsole([string]$Command) {
    try {
        Start-Process powershell.exe -Verb RunAs -ErrorAction Stop -ArgumentList "-NoExit -NoProfile -Command `"$Command; Write-Host; Write-Host 'Finished. You can close this window.'`""
    } catch {
        $txtStatus.Text = 'Cancelled (administrator permission was not given).'
    }
}

$sysDrive = $env:SystemDrive.TrimEnd(':')
$btnRestore.Add_Click({ Start-AdminConsole "Checkpoint-Computer -Description 'PC Care Kit' -RestorePointType MODIFY_SETTINGS" })
$btnSfc.Add_Click({ Start-AdminConsole 'sfc /scannow' })
$btnDism.Add_Click({ Start-AdminConsole 'DISM /Online /Cleanup-Image /RestoreHealth' })
$btnChkdsk.Add_Click({ Start-AdminConsole "chkdsk ${sysDrive}: /scan" })
$btnOptimise.Add_Click({ Start-AdminConsole "Optimize-Volume -DriveLetter $sysDrive -Verbose" })

# ---------------------------------------------------------------- go

if (Test-Admin) { $txtAdmin.Text = 'Running as administrator: all readings available.' }
else { $txtAdmin.Text = 'Running without administrator rights: some readings and all-user startup changes are unavailable.' }

$script:Window.Add_ContentRendered({
    Update-Startup
    Update-Health
})

$script:Window.ShowDialog() | Out-Null
