# Vastly

Manage Vast.ai GPU instances from your terminal — sync SSH configs, provision remote dev environments, and open your IDE in one command.

## Prerequisites

- [Vast.ai CLI](https://vast.ai/docs/cli/getting-started) (`pip install vastai`) with API key configured
- Git
- SSH (ships with Windows 10+)
- VS Code, Cursor, or Windsurf

## Install

```powershell
git clone https://github.com/seamusfallows/vastly.git
cd vastly
.\Install.ps1
```

Restart your terminal, or run `Import-Module "$env:USERPROFILE\.vastly\Vastly.psd1"` right away.

## Quick Start

**Connect to an instance:**

```powershell
cd your-project      # any local git repo
vst                  # syncs instances → detects projects → opens IDE
```

If the remote instance has no project yet, `vst` offers to clone your repo and install dependencies automatically.

**Check what's running:**

```powershell
vst-show
```

```text
NAME                      GPU                      $/HR     UPTIME
----                      ---                      ----     ------
TW-1xRTX4090              1x RTX 4090              $0.28       3.1h
US-2xA100                 2x A100                  $1.44       0.5h
```

**Tear down:**

```powershell
vst-stop               # select and destroy instances (with confirmation)
```

## Commands

| Command             | Description                                                 |
| ------------------- | ----------------------------------------------------------- |
| `vst [name]`        | Sync instances, detect projects, set up if needed, open IDE |
| `vst-update [name]` | Re-pull repo, re-install deps, re-run post-install          |
| `vst-show`          | Display live instance info (name, GPU, cost, uptime)        |
| `vst-stop [name]`   | Destroy instance(s) with confirmation                       |

All commands support tab-completion on instance names.

## Configuration

Edit `~/.vastly.json` (created on first run):

```jsonc
{
  // IDE to open — "code", "cursor", "windsurf", etc.
  "ide": "code",

  // SSH key path. null = infer from ~/.ssh/config or ssh-agent
  "sshKeyPath": null,

  // SSH user on remote instances
  "sshUser": "root",

  // Port forwarding: access remote services at localhost
  // Local ports auto-increment when multiple instances are running
  "portForwards": [
    { "local": 8080, "remote": 8080 }
  ],

  // Remote workspace root
  "workspace": "/workspace",

  // Disable auto-tmux on remote instances
  "disableAutoTmux": true,

  // Git remote to read repo URL from
  "gitRemote": "origin",

  // Commands to run after dependency install
  // Example: ["curl -fsSL https://claude.ai/install.sh | bash"]
  "postInstall": [],

  // Override auto-detected Python install method
  // Examples: "uv sync", "pip install -e '.[dev]'", "conda env update -f environment.yml"
  // null = auto-detect
  "installCommand": null
}
```

## How It Works

1. **Sync** — Calls the Vast.ai API, writes SSH configs to `~/.ssh/vast.d/`
2. **Detect** — Checks what projects exist in `/workspace` on the remote instance
3. **Setup** — If no project found, clones your repo, installs Python deps, configures the shell
4. **Open** — Launches your IDE via Remote-SSH

The setup step auto-detects your Python install method (`uv sync`, `pip install`, etc.) and runs any post-install commands you've configured. A `.vast-setup-done` marker prevents redundant setup on subsequent connections.

## Updating

```powershell
cd vastly
git pull
.\Install.ps1
```

The installer is idempotent — module files are overwritten, but your config is never touched.

## Troubleshooting

**"Missing: vastai CLI"** — Run `pip install vastai` and set your API key with `vastai set api-key <key>`.

**SSH connection timeout** — The instance may still be booting. `vst` retries 3 times automatically. Check `vst-show` to confirm the instance is running.

**Port already in use** — Another service is using the configured local port. Vastly auto-increments ports, but you can change the base port in `portForwards` or set it to `[]` to disable forwarding.

**"Not in a git repo"** — `vst` and `vst-update` need to be run from inside a git repo so they know which project to set up remotely.
