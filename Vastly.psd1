@{
    RootModule        = 'Vastly.psm1'
    ModuleVersion     = '0.1.0'
    GUID              = 'a3f7b2c1-4d5e-6f8a-9b0c-1d2e3f4a5b6c'
    Author            = 'Seamus Fallows'
    Description       = 'Manage Vast.ai GPU instances — sync SSH configs, provision environments, open your IDE.'
    PowerShellVersion = '5.1'

    FunctionsToExport = @(
        'Connect-VastInstance'
        'Update-VastInstance'
        'Show-VastInstances'
        'Stop-VastInstance'
    )

    AliasesToExport = @(
        'vst'
        'vst-update'
        'vst-show'
        'vst-stop'
    )

    CmdletsToExport   = @()
    VariablesToExport  = @()

    PrivateData = @{
        PSData = @{
            Tags       = @('vast.ai', 'gpu', 'ssh', 'remote-development')
            ProjectUri = 'https://github.com/seamusfallows/vastly'
        }
    }
}
