# Vastly

Connect to Vast.ai GPU instances from your terminal: sync SSH configs, set up your project remotely, and open your IDE in one command.

## Install

Requires Python 3.9+, Git, and [VS Code](https://code.visualstudio.com) or [Cursor](https://cursor.com) with the Remote-SSH extension.

You also need the [Vast.ai CLI](https://vast.ai/docs/cli/getting-started) installed and configured with your API key.

For private repos using SSH remotes, make sure your SSH agent is running and your key is loaded (`ssh-add -l` to check, `ssh-add` to load).

```sh
pip install vastly
```

This gives you two equivalent commands: `vastly` and `vst` (shorthand). All examples below use `vst`.

## Quick Start

```sh
cd your-project      # any local git repo
vst                  # syncs SSH, sets up remote, opens IDE
```

On first run, vastly syncs SSH configs from the API, clones your repo, auto-detects and installs dependencies, configures git identity, and opens your IDE. Subsequent runs skip setup and go straight to the IDE.

## Why Vastly

Without vastly, connecting to a Vast.ai instance means finding its IP and SSH port from the dashboard, writing an SSH config entry, cloning your project, installing deps, and opening your IDE to the right host.

```sh
# without vastly
vastai show instances                      # find instance ID, IP, port
vim ~/.ssh/config                          # add Host block with IP, port, user, key
code --remote ssh-remote+root@203.0.113.5  # hope you got the host right
cd /workspace && git clone ...             # manually clone your project
pip install -r requirements.txt            # manually install deps
```

```sh
# with vastly
vst
```

Instead of working with numeric IDs, vastly names instances by GPU and region -- `1xRTX4090-TW`, `2xA100-US`. You can also assign custom aliases with `vst name`.

Key features:

- **Auto-start** -- if no running instances, `vst` starts a stopped one and connects
- **Dependency auto-detection** -- detects uv, pip, or setup.py and installs accordingly
- **File transfer** -- `vst cp up .env` / `vst cp down results/` without remembering IPs
- **SSH agent forwarding** -- private repo access works automatically with SSH remotes
- **Per-project config** -- drop a `.vastly.json` in your repo root for project-specific settings

## Commands

```sh
vst                          # connect to your instance and open IDE
vst [name]                   # connect by name or alias
vst -f                       # re-run remote setup even if already done
vst -n                       # open IDE without cloning or installing
vst list                     # list all instances (running, stopped, etc.)
vst start [name]             # start a stopped instance, wait, then connect
vst start -n                 # start without connecting
vst stop [name | --all] [-y]  # stop an instance (or all)
vst destroy [name | --all] [-y]  # destroy an instance (irreversible)
vst ssh [name] [command...]  # SSH into an instance or run a remote command
vst cp up|down <path>        # copy files to/from remote
vst name <alias> [-i inst]   # assign a custom name to an instance
vst config                   # show current configuration
```

Use `-v` / `--verbose` with any command for debug output.

When you have multiple instances, commands that target a single instance prompt you to pick one. You can skip the prompt by passing a name or alias directly.

## Configuration

On first run, `vastly` creates `~/.vastly.json` with defaults:

```jsonc
{
  // "code" or "cursor"
  "ide": "code",

  // Path to SSH private key. null = use your SSH config or ssh-agent
  "sshKeyPath": null,

  // SSH user on remote instances
  "sshUser": "root",

  // Ports to forward to localhost. Set to [] to disable
  // Local ports auto-increment when multiple instances are running
  "portForwards": [
    { "local": 8080, "remote": 8080 }
  ],

  // Remote directory where projects are cloned
  "workspace": "/workspace",

  // Creates ~/.no_auto_tmux to prevent auto-tmux on Vast images
  "disableAutoTmux": false,

  // Which git remote to read the repo URL from
  "gitRemote": "origin",

  // Commands to run after dependency install
  // e.g. ["curl -fsSL https://claude.ai/install.sh | bash"]
  "postInstall": [],

  // Override auto-detected install method
  // null = auto-detect (uv.lock -> pyproject.toml -> requirements*.txt -> setup.py)
  // e.g. "uv sync", "pip install -e '.[dev]'", "conda env update -f environment.yml"
  "installCommand": null,

  // Files/directories to copy from local repo to remote after setup
  // Paths are relative to the repo root. Directories are copied recursively
  // e.g. [".claude/", ".env.template"]
  "copyFiles": []
}
```

### Per-project config

You can create a `.vastly.json` in your repo root to set project-specific configuration. Project config overrides the global config for these keys only:

- `postInstall`
- `installCommand`
- `workspace`
- `portForwards`
- `copyFiles`
- `gitRemote`

User-specific keys (`ide`, `sshKeyPath`, `sshUser`, `disableAutoTmux`) are always read from the global `~/.vastly.json` and ignored in project configs.

> **Note:** `postInstall` and `installCommand` run as shell commands on your remote instance during setup. Review `.vastly.json` before running `vst` in unfamiliar repositories, just as you would review a `Makefile` or `package.json` scripts.

```jsonc
// .vastly.json (in repo root)
{
  "postInstall": ["make setup"],
  "copyFiles": [".claude/"]
}
```

## How It Works

### 1. Sync

Calls the Vast.ai API and writes an SSH config for each running instance to `~/.ssh/vast.d/`. On first run, adds `Include vast.d/*` to `~/.ssh/config`.

Instances are named by GPU and region (e.g. `1xRTX4090-TW`, `2xA100-US`). Duplicates get the instance ID appended (`1xRTX4090-TW-12345`).

### 2. Select

With one running instance, it's selected automatically. With multiple, you're prompted to pick (or pass a name directly: `vst 1xRTX4090-TW`).

### 3. Setup (first run only)

For each selected instance, `vastly` checks for a marker file at `~/.vastly/setup/<repo>.json` on the instance. If the marker exists, it skips straight to opening the IDE.

On first run (no marker), `vastly` reads the remote URL from your local git repo, copies a setup script to the instance, and runs it. The setup script ([setup-remote.sh](src/vastly/data/setup-remote.sh)):

- Disables auto-tmux (if configured)
- Configures git identity from your local `git config`
- Adds the git host to SSH known hosts
- Clones your repo into the workspace
- Installs Python dependencies (auto-detected or configured)
- Runs any configured post-install commands
- Configures VS Code's Python interpreter and terminal environment -- sets the correct Python path for linting/autocomplete, activates your conda/venv in new terminals, and opens terminals in your project directory
- Writes a setup marker so setup is skipped next time

**Authentication** -- to clone private repos, the instance needs to authenticate with your git host (GitHub, GitLab, etc.):

- **SSH remotes** (`git@github.com:...`): Vastly enables SSH agent forwarding automatically. Your local SSH key is forwarded to the instance for the duration of the connection -- it is never copied. Make sure your key is loaded in your SSH agent.
- **HTTPS remotes** (`https://github.com/...`): Public repos clone without authentication. Private repos will fail because HTTPS credentials cannot be forwarded. Pushing also won't work from the instance. Switch to an SSH remote: `git remote set-url origin git@github.com:user/repo.git`.

### 4. Open

Launches your IDE via Remote-SSH at the project directory.

## Troubleshooting

**"Missing: vastai CLI"** -- `pip install vastai`, then `vastai set api-key <key>`.

**SSH connection timeout** -- Instance may still be booting. Setup retries 3 times. Run `vastai show instances` to check status.

**"Not in a git repo"** -- `vastly` reads the remote URL from your local repo. Run from inside a git repo, or use `vst --no-setup`.

**"Cannot access repo"** -- For SSH remotes, check that your SSH agent is running and your key is loaded (`ssh-add -l`). For HTTPS remotes with private repos, switch to an SSH remote (see Authentication above).
