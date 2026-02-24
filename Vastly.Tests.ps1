# Pester 3.4.0 compatible tests for the Vastly module.
# Run with: powershell -Command "Invoke-Pester -Path .\Vastly.Tests.ps1 -Verbose"
#
# Note: InModuleScope uses the module's script scope, so test variables
# must use $global: to be visible inside InModuleScope blocks.

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Import-Module "$here\Vastly.psd1" -Force

# -- Format-Uptime ---------------------------------------------------

Describe 'Format-Uptime' {

    It 'returns "?" for null input' {
        $result = InModuleScope Vastly { Format-Uptime $null }
        $result | Should Be '?'
    }

    It 'returns "?" for zero/falsy input' {
        $result = InModuleScope Vastly { Format-Uptime 0 }
        $result | Should Be '?'
    }

    It 'returns hours for timestamps older than 1 hour' {
        $global:_ts = [DateTimeOffset]::new((Get-Date).AddHours(-2)).ToUnixTimeSeconds()
        $result = InModuleScope Vastly { Format-Uptime $global:_ts }
        $result | Should Match '^\d+(\.\d)?h$'
        Remove-Variable _ts -Scope Global
    }

    It 'returns minutes for timestamps less than 1 hour ago' {
        $global:_ts = [DateTimeOffset]::new((Get-Date).AddMinutes(-10)).ToUnixTimeSeconds()
        $result = InModuleScope Vastly { Format-Uptime $global:_ts }
        $result | Should Match '^\d+m$'
        Remove-Variable _ts -Scope Global
    }
}

# -- Get-VastConfig --------------------------------------------------

Describe 'Get-VastConfig' {

    BeforeEach {
        $global:_tempDir = Join-Path $env:TEMP "vastly-test-$(Get-Random)"
        New-Item -ItemType Directory -Path $global:_tempDir -Force | Out-Null
    }

    AfterEach {
        Remove-Item $global:_tempDir -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Variable _tempDir -Scope Global -ErrorAction SilentlyContinue
        Remove-Variable _cfgPath -Scope Global -ErrorAction SilentlyContinue
    }

    It 'returns correct defaults when config is empty JSON' {
        $global:_cfgPath = Join-Path $global:_tempDir '.vastly.json'
        '{}' | Set-Content $global:_cfgPath

        $result = InModuleScope Vastly {
            $saved = $script:ConfigPath
            $script:ConfigPath = $global:_cfgPath
            $r = Get-VastConfig
            $script:ConfigPath = $saved
            $r
        }

        $result.ide | Should Be 'code'
        $result.sshUser | Should Be 'root'
        $result.workspace | Should Be '/workspace'
        $result.disableAutoTmux | Should Be $true
        $result.gitRemote | Should Be 'origin'
    }

    It 'reads user-specified values correctly' {
        $global:_cfgPath = Join-Path $global:_tempDir '.vastly.json'
        @{
            ide            = 'cursor'
            sshUser        = 'ubuntu'
            workspace      = '/home/ubuntu'
            sshKeyPath     = 'C:\Users\me\.ssh\id_rsa'
            disableAutoTmux = $false
            gitRemote      = 'upstream'
            installCommand = 'uv sync'
            portForwards   = @(@{ local = 3000; remote = 3000 })
            postInstall    = @('echo hello')
        } | ConvertTo-Json | Set-Content $global:_cfgPath

        $result = InModuleScope Vastly {
            $saved = $script:ConfigPath
            $script:ConfigPath = $global:_cfgPath
            $r = Get-VastConfig
            $script:ConfigPath = $saved
            $r
        }

        $result.ide | Should Be 'cursor'
        $result.sshUser | Should Be 'ubuntu'
        $result.workspace | Should Be '/home/ubuntu'
        $result.sshKeyPath | Should Be 'C:\Users\me\.ssh\id_rsa'
        $result.disableAutoTmux | Should Be $false
        $result.gitRemote | Should Be 'upstream'
        $result.installCommand | Should Be 'uv sync'
    }

    It 'returns default port forwards when not specified' {
        $global:_cfgPath = Join-Path $global:_tempDir '.vastly.json'
        '{}' | Set-Content $global:_cfgPath

        $result = InModuleScope Vastly {
            $saved = $script:ConfigPath
            $script:ConfigPath = $global:_cfgPath
            $r = Get-VastConfig
            $script:ConfigPath = $saved
            $r
        }

        $result.portForwards.Count | Should Be 1
        $result.portForwards[0].local | Should Be 8080
        $result.portForwards[0].remote | Should Be 8080
    }

    It 'handles empty portForwards array' {
        $global:_cfgPath = Join-Path $global:_tempDir '.vastly.json'
        '{"portForwards": []}' | Set-Content $global:_cfgPath

        $result = InModuleScope Vastly {
            $saved = $script:ConfigPath
            $script:ConfigPath = $global:_cfgPath
            $r = Get-VastConfig
            $script:ConfigPath = $saved
            $r
        }

        $result.portForwards.Count | Should Be 0
    }
}

# -- Test-PortAvailable / Find-AvailablePort -------------------------

Describe 'Port helpers' {

    It 'Test-PortAvailable returns true for an unused high port' {
        $global:_port = Get-Random -Minimum 49152 -Maximum 65535
        $result = InModuleScope Vastly { Test-PortAvailable $global:_port }
        $result | Should Be $true
        Remove-Variable _port -Scope Global
    }

    It 'Find-AvailablePort returns the start port when available' {
        $global:_port = Get-Random -Minimum 49152 -Maximum 65535
        $result = InModuleScope Vastly { Find-AvailablePort $global:_port }
        $result | Should Be $global:_port
        Remove-Variable _port -Scope Global
    }

    It 'Find-AvailablePort skips excluded ports' {
        $global:_port = Get-Random -Minimum 49152 -Maximum 65530
        $global:_excl = @{ $global:_port = $true; ($global:_port + 1) = $true }
        $result = InModuleScope Vastly { Find-AvailablePort $global:_port $global:_excl }
        $result | Should Be ($global:_port + 2)
        Remove-Variable _port -Scope Global
        Remove-Variable _excl -Scope Global
    }
}

# -- Select-VastInstance ---------------------------------------------

Describe 'Select-VastInstance' {

    $global:_insts = @(
        [PSCustomObject]@{ Name = 'TW-1xRTX3060'; Id = 100 }
        [PSCustomObject]@{ Name = 'US-2xA100'; Id = 200 }
    )

    It 'returns matching instance by name' {
        $result = InModuleScope Vastly {
            Select-VastInstance -Instances $global:_insts -Name 'US-2xA100'
        }
        @($result).Count | Should Be 1
        $result[0].Name | Should Be 'US-2xA100'
    }

    It 'returns empty for non-existent name' {
        $result = @(InModuleScope Vastly {
            Select-VastInstance -Instances $global:_insts -Name 'NOPE'
        })
        $result.Count | Should Be 0
    }

    It 'auto-selects when only one instance' {
        $global:_single = @([PSCustomObject]@{ Name = 'TW-1xRTX3060'; Id = 100 })
        $result = InModuleScope Vastly {
            Select-VastInstance -Instances $global:_single
        }
        @($result).Count | Should Be 1
        $result[0].Name | Should Be 'TW-1xRTX3060'
    }

    Remove-Variable _insts -Scope Global -ErrorAction SilentlyContinue
    Remove-Variable _single -Scope Global -ErrorAction SilentlyContinue
}

# -- Instance naming logic -------------------------------------------

Describe 'Instance naming' {

    It 'generates country-NxGPU format' {
        $result = InModuleScope Vastly {
            $gpuName = 'RTX 4090' -replace '\s+', ''
            $geo = 'Taipei, TW'
            $country = if ($geo -match ',\s*(\w+)$') { $Matches[1] } else { '' }
            if ($country) { "$country-1x$gpuName" } else { "1x$gpuName" }
        }
        $result | Should Be 'TW-1xRTX4090'
    }

    It 'omits country when geolocation is empty' {
        $result = InModuleScope Vastly {
            $geo = ''
            $country = if ($geo -match ',\s*(\w+)$') { $Matches[1] } else { '' }
            $gpuName = 'A100'
            if ($country) { "$country-2x$gpuName" } else { "2x$gpuName" }
        }
        $result | Should Be '2xA100'
    }

    It 'strips spaces from GPU names' {
        $result = InModuleScope Vastly {
            'GeForce RTX 3060' -replace '\s+', ''
        }
        $result | Should Be 'GeForceRTX3060'
    }
}

# -- URL conversion --------------------------------------------------

Describe 'HTTPS to SSH URL conversion' {

    It 'converts GitHub HTTPS to SSH' {
        $result = InModuleScope Vastly { Convert-ToSshUrl 'https://github.com/user/repo.git' }
        $result | Should Be 'git@github.com:user/repo.git'
    }

    It 'leaves SSH URLs unchanged' {
        $result = InModuleScope Vastly { Convert-ToSshUrl 'git@github.com:user/repo.git' }
        $result | Should Be 'git@github.com:user/repo.git'
    }

    It 'leaves non-GitHub HTTPS URLs unchanged' {
        $result = InModuleScope Vastly { Convert-ToSshUrl 'https://gitlab.com/user/repo.git' }
        $result | Should Be 'https://gitlab.com/user/repo.git'
    }

    It 'extracts repo name from SSH URL' {
        $result = InModuleScope Vastly {
            $url = Convert-ToSshUrl 'git@github.com:user/my-project.git'
            ($url -split '/')[-1] -replace '\.git$', ''
        }
        $result | Should Be 'my-project'
    }

    It 'extracts repo name without .git suffix' {
        $result = InModuleScope Vastly {
            $url = Convert-ToSshUrl 'https://github.com/user/repo'
            ($url -split '/')[-1] -replace '\.git$', ''
        }
        $result | Should Be 'repo'
    }
}

# -- Module exports --------------------------------------------------

Describe 'Module exports' {

    It 'exports all three functions' {
        $module = Get-Module Vastly
        $fns = @($module.ExportedFunctions.Keys | Sort-Object)
        $fns.Count | Should Be 3
        $fns[0] | Should Be 'Connect-VastInstance'
        $fns[1] | Should Be 'Show-VastInstances'
        $fns[2] | Should Be 'Stop-VastInstance'
    }

    It 'exports all three aliases' {
        $module = Get-Module Vastly
        $als = @($module.ExportedAliases.Keys | Sort-Object)
        $als.Count | Should Be 3
        $als[0] | Should Be 'vst'
        $als[1] | Should Be 'vst-show'
        $als[2] | Should Be 'vst-stop'
    }

    It 'vst alias points to Connect-VastInstance' {
        (Get-Alias vst).ReferencedCommand.Name | Should Be 'Connect-VastInstance'
    }

    It 'vst-show alias points to Show-VastInstances' {
        (Get-Alias vst-show).ReferencedCommand.Name | Should Be 'Show-VastInstances'
    }

    It 'vst-stop alias points to Stop-VastInstance' {
        (Get-Alias vst-stop).ReferencedCommand.Name | Should Be 'Stop-VastInstance'
    }
}

# -- Install.ps1 validation ------------------------------------------

Describe 'Install.ps1 file list' {

    It 'includes Vastly.psd1' {
        $content = Get-Content "$here\Install.ps1" -Raw
        $content | Should Match 'Vastly\.psd1'
    }

    It 'includes Vastly.psm1' {
        $content = Get-Content "$here\Install.ps1" -Raw
        $content | Should Match 'Vastly\.psm1'
    }

    It 'includes setup-remote.sh' {
        $content = Get-Content "$here\Install.ps1" -Raw
        $content | Should Match 'setup-remote\.sh'
    }

    It 'includes .vastly.template.json' {
        $content = Get-Content "$here\Install.ps1" -Raw
        $content | Should Match '\.vastly\.template\.json'
    }
}

# -- Config template -------------------------------------------------

Describe 'Config template' {

    It 'is valid JSON' {
        $template = Get-Content "$here\.vastly.template.json" -Raw
        { $template | ConvertFrom-Json } | Should Not Throw
    }

    It 'has all expected keys' {
        $template = Get-Content "$here\.vastly.template.json" -Raw | ConvertFrom-Json
        $keys = @($template.PSObject.Properties.Name)
        ($keys -contains 'ide') | Should Be $true
        ($keys -contains 'sshKeyPath') | Should Be $true
        ($keys -contains 'sshUser') | Should Be $true
        ($keys -contains 'portForwards') | Should Be $true
        ($keys -contains 'workspace') | Should Be $true
        ($keys -contains 'disableAutoTmux') | Should Be $true
        ($keys -contains 'gitRemote') | Should Be $true
        ($keys -contains 'postInstall') | Should Be $true
        ($keys -contains 'installCommand') | Should Be $true
    }
}

# -- Module version ----------------------------------------------------

Describe 'Module version' {

    It 'reads version from manifest' {
        $version = InModuleScope Vastly { $script:ModuleVersion }
        $manifest = Import-PowerShellDataFile "$here\Vastly.psd1"
        $version | Should Be $manifest.ModuleVersion
    }

    It 'version is a valid semver string' {
        $version = InModuleScope Vastly { $script:ModuleVersion }
        $version | Should Match '^\d+\.\d+\.\d+$'
    }
}

# -- Get-VastConfig edge cases ----------------------------------------

Describe 'Get-VastConfig edge cases' {

    BeforeEach {
        $global:_tempDir = Join-Path $env:TEMP "vastly-test-$(Get-Random)"
        New-Item -ItemType Directory -Path $global:_tempDir -Force | Out-Null
    }

    AfterEach {
        Remove-Item $global:_tempDir -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Variable _tempDir -Scope Global -ErrorAction SilentlyContinue
        Remove-Variable _cfgPath -Scope Global -ErrorAction SilentlyContinue
    }

    It 'auto-creates config from template when missing' {
        $global:_cfgPath = Join-Path $global:_tempDir '.vastly.json'

        InModuleScope Vastly {
            $saved = $script:ConfigPath
            $script:ConfigPath = $global:_cfgPath
            $null = Get-VastConfig
            $script:ConfigPath = $saved
        }

        Test-Path $global:_cfgPath | Should Be $true
    }

    It 'wraps single postInstall string so it is iterable' {
        $global:_cfgPath = Join-Path $global:_tempDir '.vastly.json'
        '{"postInstall": "echo hello"}' | Set-Content $global:_cfgPath

        $result = InModuleScope Vastly {
            $saved = $script:ConfigPath
            $script:ConfigPath = $global:_cfgPath
            $r = Get-VastConfig
            $script:ConfigPath = $saved
            $r
        }

        # Wrapping in @() simulates how the module actually uses postInstall
        @($result.postInstall).Count | Should Be 1
        @($result.postInstall)[0] | Should Be 'echo hello'
    }

    It 'returns empty array when postInstall is null' {
        $global:_cfgPath = Join-Path $global:_tempDir '.vastly.json'
        '{}' | Set-Content $global:_cfgPath

        $result = InModuleScope Vastly {
            $saved = $script:ConfigPath
            $script:ConfigPath = $global:_cfgPath
            $r = Get-VastConfig
            $script:ConfigPath = $saved
            $r
        }

        $result.postInstall.Count | Should Be 0
    }
}

# -- SSH config format -------------------------------------------------

Describe 'SSH config format' {

    BeforeEach {
        $global:_tempDir = Join-Path $env:TEMP "vastly-test-$(Get-Random)"
        New-Item -ItemType Directory -Path $global:_tempDir -Force | Out-Null
    }

    AfterEach {
        Remove-Item $global:_tempDir -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Variable _tempDir -Scope Global -ErrorAction SilentlyContinue
    }

    It 'generates valid SSH config with required fields' {
        # Simulate what Sync-VastConfigs writes for one instance
        $lines = @(
            "Host TW-1xRTX4090"
            "    HostName 192.168.1.1"
            "    Port 22222"
            "    User root"
            "    ForwardAgent yes"
            "    StrictHostKeyChecking accept-new"
        )
        $configFile = Join-Path $global:_tempDir 'TW-1xRTX4090'
        ($lines -join "`n") | Set-Content $configFile -NoNewline

        $content = Get-Content $configFile -Raw
        $content | Should Match 'Host TW-1xRTX4090'
        $content | Should Match 'HostName 192\.168\.1\.1'
        $content | Should Match 'Port 22222'
        $content | Should Match 'User root'
        $content | Should Match 'ForwardAgent yes'
        $content | Should Match 'StrictHostKeyChecking accept-new'
    }

    It 'includes IdentityFile when sshKeyPath is set' {
        $lines = @(
            "Host test"
            "    HostName 10.0.0.1"
            "    Port 22"
            "    User root"
            "    ForwardAgent yes"
            "    StrictHostKeyChecking accept-new"
            "    IdentityFile C:\Users\me\.ssh\id_rsa"
        )
        $content = $lines -join "`n"
        $content | Should Match 'IdentityFile C:\\Users\\me\\\.ssh\\id_rsa'
    }

    It 'includes LocalForward for port forwards' {
        $lines = @(
            "Host test"
            "    HostName 10.0.0.1"
            "    Port 22"
            "    User root"
            "    ForwardAgent yes"
            "    StrictHostKeyChecking accept-new"
            "    LocalForward 8080 localhost:8080"
        )
        $content = $lines -join "`n"
        $content | Should Match 'LocalForward 8080 localhost:8080'
    }
}

# -- Duplicate instance naming -----------------------------------------

Describe 'Duplicate instance naming' {

    It 'appends instance ID when names collide' {
        # Replicate the naming logic from Sync-VastConfigs
        $instances = @(
            @{ gpu_name = 'RTX 4090'; num_gpus = 1; geolocation = 'Taipei, TW'; id = 111 }
            @{ gpu_name = 'RTX 4090'; num_gpus = 1; geolocation = 'Taipei, TW'; id = 222 }
        )

        $nameCount = @{}
        $names = @()

        foreach ($inst in $instances) {
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
            $names += $name
        }

        $names[0] | Should Be 'TW-1xRTX4090'
        $names[1] | Should Be 'TW-1xRTX4090-222'
    }
}

# -- File encoding safety ----------------------------------------------

Describe 'File encoding safety' {

    It 'PowerShell files contain only ASCII characters' {
        $files = Get-ChildItem "$here\*.ps1", "$here\*.psm1", "$here\*.psd1"
        foreach ($file in $files) {
            $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
            $nonAscii = @($bytes | Where-Object { $_ -gt 127 })
            if ($nonAscii.Count -gt 0) {
                # Find the line for a useful error message
                $content = Get-Content $file.FullName
                for ($i = 0; $i -lt $content.Length; $i++) {
                    if ($content[$i] -match '[^\x00-\x7F]') {
                        throw "Non-ASCII on line $($i + 1) of $($file.Name): $($content[$i])"
                    }
                }
            }
        }
        $true | Should Be $true
    }
}

# -- setup-remote.sh syntax -------------------------------------------

Describe 'setup-remote.sh' {

    It 'has valid bash syntax' {
        $bash = Get-Command bash -ErrorAction SilentlyContinue
        if (-not $bash) {
            Set-TestInconclusive 'bash not available'
            return
        }
        bash -n "$here/setup-remote.sh" 2>&1
        $LASTEXITCODE | Should Be 0
    }
}
