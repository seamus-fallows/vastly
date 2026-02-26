# Vastly

Connect to Vast.ai GPU instances from your terminal: sync SSH configs, set up your project remotely, and open your IDE in one command.

## Prerequisites

- Python 3.9+
- [Vast.ai CLI](https://vast.ai/docs/cli/getting-started) (`pip install vastai`) with API key configured
- Git
- [VS Code](https://code.visualstudio.com) or [Cursor](https://cursor.com) with the Remote-SSH extension

## Install

```sh
pip install vastly
```

This gives you two equivalent commands: `vastly` and `vst` (shorthand). All examples below use `vst`.

## Usage

```sh
cd your-project      # any local git repo
vst                  # checks setup -> opens IDE (sets up on first run)
```

```sh
vst 1xRTX4090-TW    # target a specific instance by name
vst --no-setup       # open IDE without cloning or installing anything
vst --force-setup    # re-run remote setup even if already done
vst --list           # list instances and exit
vst --version        # show version
```

## How It Works

### 1. Sync

Calls the Vast.ai API and writes an SSH config for each running instance to `~/.ssh/vast.d/`. On first run, adds `Include vast.d/*` to `~/.ssh/config`.

Instances are named by GPU and region (e.g. `1xRTX4090-TW`, `2xA100-US`). Duplicates get the instance ID appended (`1xRTX4090-TW-12345`).

### 2. Select

One instance is selected automatically. Multiple instances prompt you to pick one or select all. You can also pass the name directly: `vst 1xRTX4090-TW`.

### 3. Setup (first run only)

For each selected instance, `vastly` checks if the project has already been set up by looking for a marker file at `~/.vastly/setup/<repo>.json` on the instance. If the marker exists, it skips straight to opening the IDE.

On first run (no marker), `vastly` reads the remote URL from your local git repo, copies a setup script to the instance, and runs it.

The setup script ([setup-remote.sh](src/vastly/data/setup-remote.sh)):

- Disables auto-tmux (if configured)
- Configures git identity from your local `git config`
- Adds the git host to SSH known hosts (extracted from repo URL)
- Clones your repo into the workspace
- Installs Python dependencies (auto-detected)
- Runs any configured post-install commands
- Writes VS Code settings and patches `.bashrc`
- Writes a setup marker (`~/.vastly/setup/<repo>.json`) so setup is skipped next time

**Dependency auto-detection** (checked in order):

1. `uv.lock` or `[tool.uv]` in pyproject.toml -- `uv sync` (installs uv if needed)
2. `[project]` in pyproject.toml -- `pip install -e .`
3. `requirements*.txt` -- `pip install -r` for each file
4. `setup.py` -- `pip install -e .`

Override with `installCommand` in your config.

**Authentication** -- to clone private repos, the instance needs to authenticate with your git host (GitHub, GitLab, etc.). How this works depends on your remote URL:

- **SSH remotes** (`git@github.com:...`): Vastly enables SSH agent forwarding automatically. Your local SSH key is forwarded to the instance for the duration of the connection -- it is never copied. Make sure your key is loaded in your SSH agent (`ssh-add -l` to check, `ssh-add` to load).
- **HTTPS remotes** (`https://github.com/...`): Agent forwarding does not apply. Public repos clone without authentication. Private repos will fail because HTTPS credentials (tokens, Git Credential Manager) cannot be forwarded to remote instances. To fix this, switch to an SSH remote: `git remote set-url origin git@github.com:user/repo.git`.

### 4. Open

Launches your IDE via Remote-SSH at the project directory. If already open, focuses the existing window.

## Configuration

On first run, `vastly` creates `~/.vastly.json` with defaults:

```jsonc
{
  // "code" (VS Code) or "cursor"
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

  // Override auto-detected install method. null = auto-detect
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

```jsonc
// .vastly.json (in repo root)
{
  "postInstall": ["make setup"],
  "copyFiles": [".claude/"]
}
```

## Troubleshooting

**"Missing: vastai CLI"** -- `pip install vastai`, then `vastai set api-key <key>`.

**SSH connection timeout** -- Instance may still be booting. Setup retries 3 times. Run `vastai show instances` to check status.

**"Not in a git repo"** -- `vastly` reads the remote URL from your local repo. Run from inside a git repo, or use `--no-setup`.

**"Cannot access repo"** -- For SSH remotes, check that your SSH agent is running and your key is loaded (`ssh-add -l`). For HTTPS remotes with private repos, HTTPS credentials can't be forwarded -- switch to an SSH remote (see Authentication above).
