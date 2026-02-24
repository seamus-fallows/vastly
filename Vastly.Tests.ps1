# Pester 3.4.0 compatible tests for the Vastly module.
# Run with: powershell -Command "Invoke-Pester -Path .\Vastly.Tests.ps1 -Verbose"
#
# Note: InModuleScope uses the module's script scope, so test variables
# must use $global: to be visible inside InModuleScope blocks.

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Import-Module "$here\Vastly.psd1" -Force

# ── Format-Uptime ───────────────────────────────────────────────────

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

# ── Get-VastConfig ──────────────────────────────────────────────────

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

# ── Test-PortAvailable / Find-AvailablePort ─────────────────────────

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

# ── Select-VastInstance ─────────────────────────────────────────────

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

# ── Instance naming logic ───────────────────────────────────────────

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

# ── URL conversion ──────────────────────────────────────────────────

Describe 'HTTPS to SSH URL conversion' {

    It 'converts GitHub HTTPS to SSH' {
        $result = InModuleScope Vastly {
            $url = 'https://github.com/user/repo.git'
            if ($url -match 'https://github\.com/(.+)') { $url = "git@github.com:$($Matches[1])" }
            $url
        }
        $result | Should Be 'git@github.com:user/repo.git'
    }

    It 'leaves SSH URLs unchanged' {
        $result = InModuleScope Vastly {
            $url = 'git@github.com:user/repo.git'
            if ($url -match 'https://github\.com/(.+)') { $url = "git@github.com:$($Matches[1])" }
            $url
        }
        $result | Should Be 'git@github.com:user/repo.git'
    }

    It 'leaves non-GitHub HTTPS URLs unchanged' {
        $result = InModuleScope Vastly {
            $url = 'https://gitlab.com/user/repo.git'
            if ($url -match 'https://github\.com/(.+)') { $url = "git@github.com:$($Matches[1])" }
            $url
        }
        $result | Should Be 'https://gitlab.com/user/repo.git'
    }

    It 'extracts repo name from SSH URL' {
        $result = InModuleScope Vastly {
            $url = 'git@github.com:user/my-project.git'
            ($url -split '/')[-1] -replace '\.git$', ''
        }
        $result | Should Be 'my-project'
    }

    It 'extracts repo name without .git suffix' {
        $result = InModuleScope Vastly {
            $url = 'https://github.com/user/repo'
            ($url -split '/')[-1] -replace '\.git$', ''
        }
        $result | Should Be 'repo'
    }
}

# ── Module exports ──────────────────────────────────────────────────

Describe 'Module exports' {

    It 'exports all four functions' {
        $module = Get-Module Vastly
        $fns = @($module.ExportedFunctions.Keys | Sort-Object)
        $fns.Count | Should Be 4
        $fns[0] | Should Be 'Connect-VastInstance'
        $fns[1] | Should Be 'Show-VastInstances'
        $fns[2] | Should Be 'Stop-VastInstance'
        $fns[3] | Should Be 'Update-VastInstance'
    }

    It 'exports all four aliases' {
        $module = Get-Module Vastly
        $als = @($module.ExportedAliases.Keys | Sort-Object)
        $als.Count | Should Be 4
        $als[0] | Should Be 'vst'
        $als[1] | Should Be 'vst-show'
        $als[2] | Should Be 'vst-stop'
        $als[3] | Should Be 'vst-update'
    }

    It 'vst alias points to Connect-VastInstance' {
        (Get-Alias vst).ReferencedCommand.Name | Should Be 'Connect-VastInstance'
    }

    It 'vst-update alias points to Update-VastInstance' {
        (Get-Alias vst-update).ReferencedCommand.Name | Should Be 'Update-VastInstance'
    }

    It 'vst-show alias points to Show-VastInstances' {
        (Get-Alias vst-show).ReferencedCommand.Name | Should Be 'Show-VastInstances'
    }

    It 'vst-stop alias points to Stop-VastInstance' {
        (Get-Alias vst-stop).ReferencedCommand.Name | Should Be 'Stop-VastInstance'
    }
}

# ── Install.ps1 validation ──────────────────────────────────────────

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

# ── Config template ─────────────────────────────────────────────────

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
