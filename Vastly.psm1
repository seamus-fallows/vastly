# Vastly - Vast.ai instance manager
# https://github.com/seamusfallows/vastly

$script:ModuleVersion = (Import-PowerShellDataFile "$PSScriptRoot\Vastly.psd1").ModuleVersion
$script:ConfigPath = Join-Path $env:USERPROFILE '.vastly.json'
$script:SshConfigDir = Join-Path (Join-Path $env:USERPROFILE '.ssh') 'vast.d'
# Quick probes (echo ok, test -f, find): fail fast on dead connections
$script:SshOpts = @('-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2')
# Long-running commands (setup script): allow more time between keepalives
$script:SshSetupOpts = @('-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4')

# -- Configuration ----------------------------------------------------

function Get-VastConfig {
    if (-not (Test-Path $script:ConfigPath)) {
        $template = Join-Path $PSScriptRoot '.vastly.template.json'
        if (Test-Path $template) {
            Copy-Item $template $script:ConfigPath
            Write-Host "Created config at $($script:ConfigPath) - edit to customize." -ForegroundColor Cyan
        }
        else {
            '{}' | Set-Content $script:ConfigPath
        }
    }

    $raw = Get-Content $script:ConfigPath -Raw | ConvertFrom-Json

    @{
        ide             = if ($raw.ide) { $raw.ide } else { 'code' }
        sshKeyPath      = $raw.sshKeyPath
        sshUser         = if ($raw.sshUser) { $raw.sshUser } else { 'root' }
        portForwards    = @(if ($null -ne $raw.portForwards) { $raw.portForwards } else { @{ local = 8080; remote = 8080 } })
        workspace       = if ($raw.workspace) { $raw.workspace } else { '/workspace' }
        disableAutoTmux = if ($null -ne $raw.disableAutoTmux) { [bool]$raw.disableAutoTmux } else { $true }
        gitRemote       = if ($raw.gitRemote) { $raw.gitRemote } else { 'origin' }
        postInstall     = if ($raw.postInstall) { @($raw.postInstall) } else { @() }
        installCommand  = $raw.installCommand
    }
}

# -- Prerequisites ----------------------------------------------------

function Test-VastPrerequisites {
    param([switch]$NeedIDE)

    $ok = $true

    if (-not (Get-Command vastai -ErrorAction SilentlyContinue)) {
        Write-Host "Missing: vastai CLI. Install with: pip install vastai" -ForegroundColor Red
        $ok = $false
    }
    if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
        Write-Host "Missing: ssh. Should ship with Windows 10+." -ForegroundColor Red
        $ok = $false
    }
    if ($NeedIDE) {
        $ide = (Get-VastConfig).ide
        if (-not (Get-Command $ide -ErrorAction SilentlyContinue)) {
            Write-Host "Missing: $ide. Download from the official website." -ForegroundColor Red
            $ok = $false
        }
    }

    $ok
}

# -- Port helpers -----------------------------------------------------

function Test-PortAvailable {
    param([int]$Port)
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
    try { $listener.Start(); $listener.Stop(); $true }
    catch { $false }
}

function Find-AvailablePort {
    param([int]$Start, [hashtable]$Exclude = @{})
    $port = $Start
    while (-not (Test-PortAvailable $port) -or $Exclude.ContainsKey($port)) { $port++ }
    $port
}

# -- SSH config sync --------------------------------------------------

function Sync-VastConfigs {
    $config = Get-VastConfig

    # Ensure SSH config directory exists
    if (-not (Test-Path $script:SshConfigDir)) {
        New-Item -ItemType Directory -Path $script:SshConfigDir -Force | Out-Null
    }

    # Ensure main SSH config includes vast.d/*
    $sshConfig = Join-Path (Join-Path $env:USERPROFILE '.ssh') 'config'
    $includeLine = 'Include vast.d/*'
    if (Test-Path $sshConfig) {
        $content = Get-Content $sshConfig -Raw
        if ($content -notmatch 'Include\s+vast\.d[/\\]\*') {
            Set-Content $sshConfig -Value ("$includeLine`n$content") -NoNewline
        }
    }
    else {
        $sshDir = Join-Path $env:USERPROFILE '.ssh'
        if (-not (Test-Path $sshDir)) {
            New-Item -ItemType Directory -Path $sshDir -Force | Out-Null
        }
        Set-Content $sshConfig -Value $includeLine
    }

    # Fetch instances from API
    Write-Host 'Syncing instances...' -ForegroundColor Gray
    $json = vastai show instances --raw 2>&1
    $all = try { $json | ConvertFrom-Json } catch { $null }

    if (-not $all) {
        $cached = @(Get-ChildItem $script:SshConfigDir -ErrorAction SilentlyContinue)
        if ($cached.Count -gt 0) {
            Write-Host 'API unreachable - using cached configs.' -ForegroundColor Yellow
            return $cached | ForEach-Object {
                [PSCustomObject]@{ Name = $_.Name; Cached = $true; Id = $null; DphTotal = 0; GpuName = ''; NumGpus = 0; StartDate = $null }
            }
        }
        return $null
    }

    $running = @($all | Where-Object { $_.cur_state -eq 'running' })

    # Clear old configs
    Remove-Item "$($script:SshConfigDir)\*" -ErrorAction SilentlyContinue

    if ($running.Count -eq 0) { return ,@() }

    $nameCount = @{}
    $usedPorts = @{}
    $results = @()

    foreach ($inst in $running) {
        # Build name: {country}-{num_gpus}x{gpu_name}
        $gpuName = $inst.gpu_name -replace '\s+', ''
        $country = if ($inst.geolocation -match ',\s*(\w+)$') { $Matches[1] } else { '' }
        $baseName = if ($country) { "$country-$($inst.num_gpus)x$gpuName" } else { "$($inst.num_gpus)x$gpuName" }

        if ($nameCount.ContainsKey($baseName)) {
            $name = "$baseName-$($inst.id)"
        }
        else {
            $nameCount[$baseName] = $true
            $name = $baseName
        }

        # Get SSH port - skip instances without port 22 exposed
        $sshPort = try { $inst.ports.'22/tcp'[0].HostPort } catch { $null }
        if (-not $sshPort) { continue }

        # Build SSH config lines
        $lines = @(
            "Host $name"
            "    HostName $($inst.public_ipaddr)"
            "    Port $sshPort"
            "    User $($config.sshUser)"
            "    ForwardAgent yes"
            "    StrictHostKeyChecking accept-new"
        )

        if ($config.sshKeyPath) {
            $lines += "    IdentityFile $($config.sshKeyPath)"
        }

        foreach ($pf in $config.portForwards) {
            $localPort = Find-AvailablePort ([int]$pf.local + $results.Count) $usedPorts
            $usedPorts[$localPort] = $true
            $lines += "    LocalForward $localPort localhost:$($pf.remote)"
        }

        ($lines -join "`n") | Set-Content (Join-Path $script:SshConfigDir $name) -NoNewline

        $results += [PSCustomObject]@{
            Name      = $name
            Id        = $inst.id
            DphTotal  = $inst.dph_total
            GpuName   = $inst.gpu_name
            NumGpus   = $inst.num_gpus
            StartDate = $inst.start_date
            Cached    = $false
        }
    }

    ,$results
}

function Get-SyncedInstances {
    $syncResult = Sync-VastConfigs
    if ($null -eq $syncResult) {
        Write-Host 'No running instances found and API unreachable.' -ForegroundColor Red
        return $null
    }
    $instances = @($syncResult)
    if ($instances.Count -eq 0) {
        Write-Host 'No running Vast instances.' -ForegroundColor Yellow
        return $null
    }
    $instances
}

function Convert-ToSshUrl {
    param([string]$Url)
    if ($Url -match 'https://github\.com/(.+)') {
        return "git@github.com:$($Matches[1])"
    }
    $Url
}

# -- Display & Selection ----------------------------------------------

function Format-Uptime {
    param($UnixTimestamp)
    if (-not $UnixTimestamp) { return '?' }
    $started = [DateTimeOffset]::FromUnixTimeSeconds($UnixTimestamp).LocalDateTime
    $span = (Get-Date) - $started
    if ($span.TotalHours -ge 1) { return "$([math]::Round($span.TotalHours, 1))h" }
    return "$([math]::Round($span.TotalMinutes))m"
}

function Show-InstanceTable {
    param([array]$Instances)
    foreach ($inst in $Instances) {
        if ($inst.Cached) {
            Write-Host "  $($inst.Name)  (cached)" -ForegroundColor DarkGray
        }
        else {
            $cost = '$' + [math]::Round($inst.DphTotal, 2) + '/hr'
            $uptime = Format-Uptime $inst.StartDate
            Write-Host "  $($inst.Name)   $cost   ${uptime} uptime"
        }
    }
    Write-Host ''
}

function Select-VastInstance {
    param(
        [array]$Instances,
        [string]$Name,
        [string]$Prompt = 'Select instance:',
        [switch]$AllowMultiple
    )

    $names = @($Instances | ForEach-Object { $_.Name })

    # Named selection
    if ($Name) {
        $match = $Instances | Where-Object { $_.Name -eq $Name }
        if (-not $match) {
            Write-Host "No instance named '$Name'. Available: $($names -join ', ')" -ForegroundColor Red
            return @()
        }
        return @($match)
    }

    # Auto-select if only one
    if ($Instances.Count -eq 1) { return @($Instances[0]) }

    # Interactive selection
    Write-Host $Prompt
    if ($AllowMultiple) { Write-Host '  [0] All' }
    for ($i = 0; $i -lt $names.Count; $i++) {
        Write-Host "  [$($i + 1)] $($names[$i])"
    }

    $choice = Read-Host 'Choice'
    if ($choice -notmatch '^\d+$') { return @() }
    $choice = [int]$choice

    if ($AllowMultiple -and $choice -eq 0) { return $Instances }
    if ($choice -lt 1 -or $choice -gt $names.Count) { return @() }
    @($Instances[$choice - 1])
}

# -- Remote setup -----------------------------------------------------

function Invoke-VastRemoteSetup {
    param(
        [PSCustomObject[]]$Instances,
        [string]$RepoUrl,
        [string]$RepoName
    )

    $config = Get-VastConfig
    $gitName = git config --global user.name 2>$null
    $gitEmail = git config --global user.email 2>$null

    if (-not $gitName -or -not $gitEmail) {
        Write-Host 'Git identity not configured. Run: git config --global user.name "Your Name"' -ForegroundColor Red
        return @()
    }

    $setupScript = Join-Path $PSScriptRoot 'setup-remote.sh'
    if (-not (Test-Path $setupScript)) {
        Write-Host "Setup script not found at $setupScript" -ForegroundColor Red
        return @()
    }

    $installCmd = if ($config.installCommand) { $config.installCommand } else { 'auto' }
    $disableTmux = if ($config.disableAutoTmux) { 'true' } else { 'false' }

    $successNames = @()

    foreach ($inst in $Instances) {
        $name = $inst.Name
        Write-Host "  ${name}: " -NoNewline

        # SSH retry loop - instances may still be booting
        $reachable = $false
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            $null = ssh $script:SshOpts $name 'echo ok' 2>&1
            if ($LASTEXITCODE -eq 0) { $reachable = $true; break }
            if ($attempt -lt 3) {
                Write-Host 'waiting...' -NoNewline -ForegroundColor Yellow
                Start-Sleep -Seconds 5
            }
        }

        if (-not $reachable) {
            Write-Host 'unreachable after 3 attempts. Check vst-show to confirm it is running.' -ForegroundColor Red
            continue
        }

        # Check setup marker
        $marker = ssh $script:SshOpts $name "test -f ~/.vastly/setup/${RepoName}.json && echo done" 2>&1
        if ($marker -eq 'done') {
            Write-Host 'already set up.' -ForegroundColor Green
            $successNames += $name
            continue
        }

        Write-Host 'running setup...' -ForegroundColor Cyan

        # SCP setup script and execute
        scp $script:SshSetupOpts "$setupScript" "${name}:/tmp/_vastly-setup.sh" 2>&1 | Out-Null

        # Build argument list
        $setupArgs = @(
            $RepoUrl, $RepoName, $gitName, $gitEmail,
            $config.workspace, $disableTmux, $installCmd, $script:ModuleVersion
        ) + $config.postInstall

        $quotedArgs = $setupArgs | ForEach-Object { "'$($_ -replace "'", "'\\''")'" }
        $argString = $quotedArgs -join ' '

        # SSH outputs UTF-8; tell PowerShell to decode it correctly
        $savedEncoding = [Console]::OutputEncoding
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
        ssh $script:SshSetupOpts $name "sed -i 's/\r$//' /tmp/_vastly-setup.sh && bash /tmp/_vastly-setup.sh $argString; e=`$?; rm -f /tmp/_vastly-setup.sh; exit `$e" 2>&1 |
            ForEach-Object { Write-Host "    $_" }
        [Console]::OutputEncoding = $savedEncoding

        if ($LASTEXITCODE -ne 0) {
            Write-Host "  ${name}: setup failed (exit $LASTEXITCODE)" -ForegroundColor Red
            continue
        }

        Write-Host "  ${name}: done." -ForegroundColor Green
        $successNames += $name
    }

    $successNames
}

# -- Public commands --------------------------------------------------

function Connect-VastInstance {
    <#
    .SYNOPSIS
    Connect to a Vast.ai instance - detect projects, set up if needed, open IDE.
    #>
    param(
        [Parameter(Position = 0)]
        [string]$Name,
        [switch]$NoSetup
    )

    if (-not (Test-VastPrerequisites -NeedIDE)) { return }

    $config = Get-VastConfig
    $instances = Get-SyncedInstances
    if (-not $instances) { return }

    Show-InstanceTable $instances

    $selected = Select-VastInstance -Instances $instances -Name $Name
    if ($selected.Count -eq 0) { return }

    foreach ($inst in $selected) {
        $instName = $inst.Name
        Write-Host "Checking $instName..." -ForegroundColor Gray

        # List non-hidden directories in workspace
        $dirs = ssh $script:SshOpts $instName "find $($config.workspace) -mindepth 1 -maxdepth 1 -type d -not -name '.*' -printf '%f\n'" 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  ${instName}: unreachable via SSH. Check vst-show to confirm it is running." -ForegroundColor Red
            continue
        }
        $dirList = @($dirs | Where-Object { $_ })

        if ($dirList.Count -eq 1) {
            $remotePath = "$($config.workspace)/$($dirList[0])"
            Write-Host "  Opening $remotePath" -ForegroundColor Green
            & $config.ide --remote "ssh-remote+$instName" $remotePath
        }
        elseif ($dirList.Count -gt 1) {
            Write-Host "  Multiple projects found on ${instName}:"
            for ($i = 0; $i -lt $dirList.Count; $i++) {
                Write-Host "    [$($i + 1)] $($dirList[$i])"
            }
            Write-Host "    [0] Open $($config.workspace)"

            $pick = Read-Host '  Choice'
            if ($pick -notmatch '^\d+$') { continue }
            $pick = [int]$pick

            $remotePath = if ($pick -eq 0) { $config.workspace }
            elseif ($pick -ge 1 -and $pick -le $dirList.Count) { "$($config.workspace)/$($dirList[$pick - 1])" }
            else { continue }

            & $config.ide --remote "ssh-remote+$instName" $remotePath
        }
        else {
            # No projects found on remote
            if ($NoSetup) {
                Write-Host "  Opening $($config.workspace)" -ForegroundColor Green
                & $config.ide --remote "ssh-remote+$instName" $config.workspace
                continue
            }

            $repoUrl = git remote get-url $config.gitRemote 2>$null
            if (-not $repoUrl) {
                Write-Host "  No projects on remote and not in a local git repo." -ForegroundColor Yellow
                Write-Host "  Opening $($config.workspace). Tip: run vst from inside a git repo to auto-setup." -ForegroundColor Gray
                & $config.ide --remote "ssh-remote+$instName" $config.workspace
                continue
            }

            $repoUrl = Convert-ToSshUrl $repoUrl
            $repoName = ($repoUrl -split '/')[-1] -replace '\.git$', ''

            $success = Invoke-VastRemoteSetup -Instances @($inst) -RepoUrl $repoUrl -RepoName $repoName
            if ($success) {
                & $config.ide --remote "ssh-remote+$instName" "$($config.workspace)/$repoName"
            }
        }
    }
}

function Show-VastInstances {
    <#
    .SYNOPSIS
    Display live Vast.ai instance info: name, GPU, cost, uptime.
    #>

    if (-not (Test-VastPrerequisites)) { return }

    $instances = Get-SyncedInstances
    if (-not $instances) { return }

    # Table header
    $fmt = '{0,-25} {1,-20} {2,10} {3,10}'
    Write-Host ($fmt -f 'NAME', 'GPU', '$/HR', 'UPTIME') -ForegroundColor DarkGray
    Write-Host ($fmt -f '----', '---', '----', '------') -ForegroundColor DarkGray

    foreach ($inst in $instances) {
        if ($inst.Cached) {
            Write-Host ($fmt -f $inst.Name, '(cached)', '-', '-') -ForegroundColor DarkGray
        }
        else {
            $gpu = "$($inst.NumGpus)x $($inst.GpuName)"
            $cost = '$' + [math]::Round($inst.DphTotal, 2)
            $uptime = Format-Uptime $inst.StartDate
            Write-Host ($fmt -f $inst.Name, $gpu, $cost, $uptime)
        }
    }
}

function Stop-VastInstance {
    <#
    .SYNOPSIS
    Stop Vast.ai instance(s) after confirmation.
    #>
    param(
        [Parameter(Position = 0)]
        [string]$Name,
        [switch]$Destroy
    )

    if (-not (Test-VastPrerequisites)) { return }

    $instances = Get-SyncedInstances
    if (-not $instances) { return }

    Show-InstanceTable $instances

    $selected = Select-VastInstance -Instances $instances -Name $Name -Prompt 'Select instances to stop:' -AllowMultiple
    if ($selected.Count -eq 0) { return }

    # Confirmation
    $action = if ($Destroy) { 'Destroy' } else { 'Stop' }
    $nameList = ($selected | ForEach-Object { $_.Name }) -join ', '
    Write-Host "$action ${nameList}? [y/N] " -NoNewline -ForegroundColor Yellow
    $confirm = Read-Host
    if ($confirm -notmatch '^[Yy]') {
        Write-Host 'Cancelled.'
        return
    }

    foreach ($inst in $selected) {
        if ($inst.Cached) {
            Write-Host "  $($inst.Name): skipped (no instance ID - cached config)" -ForegroundColor Yellow
            continue
        }
        $verb = if ($Destroy) { 'destroying' } else { 'stopping' }
        Write-Host "  $($inst.Name): ${verb}..." -NoNewline
        $cmd = if ($Destroy) { 'destroy' } else { 'stop' }
        vastai $cmd instance $inst.Id 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host ' done.' -ForegroundColor Green
            $configFile = Join-Path $script:SshConfigDir $inst.Name
            Remove-Item $configFile -ErrorAction SilentlyContinue
        }
        else {
            Write-Host ' failed.' -ForegroundColor Red
        }
    }
}

# -- Aliases & Tab Completion -----------------------------------------

Set-Alias -Name vst -Value Connect-VastInstance
Set-Alias -Name vst-show -Value Show-VastInstances
Set-Alias -Name vst-stop -Value Stop-VastInstance

Register-ArgumentCompleter -CommandName Connect-VastInstance, Stop-VastInstance -ParameterName Name -ScriptBlock {
    param($commandName, $parameterName, $wordToComplete)
    $dir = Join-Path (Join-Path $env:USERPROFILE '.ssh') 'vast.d'
    Get-ChildItem $dir -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "$wordToComplete*" } |
        ForEach-Object {
            [System.Management.Automation.CompletionResult]::new($_.Name, $_.Name, 'ParameterValue', $_.Name)
        }
}

Export-ModuleMember -Function Connect-VastInstance, Show-VastInstances, Stop-VastInstance -Alias vst, vst-show, vst-stop
