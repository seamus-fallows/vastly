# Vastly

Manage Vast.ai GPU instances from your terminal -- sync SSH configs, provision remote dev environments, and open your IDE in one command.

## Prerequisites

- Python 3.9+
- [Vast.ai CLI](https://vast.ai/docs/cli/getting-started) (`pip install vastai`) with API key configured
- Git
- SSH (ships with Windows 10+, macOS, and Linux)
- VS Code or Cursor

## Install

```sh
pip install vastly
```

For development:

```sh
git clone https://github.com/seamusfallows/vastly.git
cd vastly
pip install -e .
```

## Quick Start

**Connect to an instance:**

```sh
cd your-project      # any local git repo
vst                  # syncs instances -> detects projects -> opens IDE
```

If the remote instance has no project yet, `vst` offers to clone your repo and install dependencies automatically.

**Target a specific instance:**

```sh
vst my-instance
```

**Skip setup (just open IDE):**

```sh
vst --no-setup
```

## Commands

| Command              | Description                                                    |
| -------------------- | -------------------------------------------------------------- |
| `vst [name]`         | Sync instances, detect projects, set up if needed, open IDE    |
| `vst --no-setup`     | Open IDE on an instance without cloning or installing anything |
| `vst --version`      | Show version                                                   |

To check instance status or stop instances, use the Vast.ai CLI directly:

```sh
vastai show instances
vastai stop instance <id>
vastai destroy instance <id>
```

## Configuration

Edit `~/.vastly.json` (created on first run):

```jsonc
{
  // IDE to open -- "code" or "cursor"
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

1. **Sync** -- Calls the Vast.ai API, writes SSH configs to `~/.ssh/vast.d/` (adds `Include vast.d/*` to `~/.ssh/config` on first run)
2. **Detect** -- Checks what projects exist in `/workspace` on the remote instance
3. **Setup** -- If no project found, clones your repo, installs Python deps, configures the shell
4. **Open** -- Launches your IDE via Remote-SSH

The setup step auto-detects your Python install method (`uv sync`, `pip install`, etc.) and runs any post-install commands you've configured. A marker at `~/.vastly/setup/<repo>.json` on the remote prevents redundant setup on subsequent connections.

## Updating

```sh
pip install --upgrade vastly
```

If you installed from source:

```sh
cd vastly
git pull
pip install -e .
```

Your config (`~/.vastly.json`) is never touched by install or update.

## Troubleshooting

**"Missing: vastai CLI"** -- Run `pip install vastai` and set your API key with `vastai set api-key <key>`.

**SSH connection timeout** -- The instance may still be booting. `vst` retries 3 times automatically. Run `vastai show instances` to confirm the instance is running.

**Port already in use** -- Another service is using the configured local port. Vastly auto-increments ports, but you can change the base port in `portForwards` or set it to `[]` to disable forwarding.

**"Not in a git repo"** -- `vst` needs to be run from inside a git repo so it knows which project to set up remotely. Use `vst --no-setup` to skip this requirement.
