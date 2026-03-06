# Vastly

Connect to Vast.ai GPU instances from your terminal: sync SSH configs, set up your project remotely, and open your IDE in one command.

## Install

Requires the [Vast.ai CLI](https://vast.ai/docs/cli/getting-started) (`pip install vastai && vastai set api-key YOUR_KEY`) and [VS Code](https://code.visualstudio.com) or [Cursor](https://cursor.com) with the Remote-SSH extension.

```sh
pip install vastly
```

This gives you two equivalent commands: `vastly` and `vst` (shorthand).

## Quick Start

```sh
cd your-project      # any local git repo
vst                  # syncs SSH, sets up remote, opens IDE
```

## Why Vastly

Without vastly, you repeat this for every new instance:

```sh
# without vastly
# 1. Copy SSH command from Vast.ai dashboard
# 2. VSCode → Remote-SSH: Connect to Host → paste command
# 3. On the remote instance:
cd /workspace && git clone ...             # clone your project
pip install -r requirements.txt            # install deps
git config user.name ...                   # configure git identity
# set up git auth
# copy over .env, config files, etc. that aren't in git
# fix "Permission denied (publickey)" because you forgot -A
```

```sh
# with vastly
vst
```

`vst` does all of this for you -- syncs SSH configs with agent forwarding, clones your repo, installs deps, activates your Python environment, copies configured files not in git, and opens your IDE. On repeat runs, setup is skipped.

- **Named instances** -- `1xRTX4090-TW` instead of IPs (duplicates get the instance ID appended); pick from a list when you have multiple; assign custom aliases with `vst name`
- **Auto-start** -- if no running instances, `vst` starts a stopped one and connects
- **Environment auto-detection** -- activates conda/venv on Vast.ai images, auto-detects uv, pip, or setup.py for dependency install
- **SSH agent forwarding** -- authenticate with git hosts without copying keys to the instance
- **File transfer** -- `vst cp up .env` / `vst cp down results/` without remembering IPs
- **Per-project config** -- `.vastly.json` in your repo root for project-specific settings

## Commands

```sh
vst                          # connect to your instance and open IDE
vst [name]                   # connect by name or alias
vst --all                    # connect to all running instances
vst -f                       # re-run remote setup even if already done
vst -n                       # open IDE without cloning or installing
vst list                     # list all instances (running, stopped, etc.)
vst start [name | --all]     # start a stopped instance, wait, then connect
vst start -n                 # start without connecting
vst stop [name | --all] [-y]  # stop an instance (or all)
vst destroy [name | --all] [-y]  # destroy an instance (irreversible)
vst ssh [name] [command...]  # SSH into an instance or run a remote command
vst cp up|down <paths...>    # copy files to/from remote
vst name <alias> [-i inst] [--clear]  # assign a custom name to an instance
vst config                   # show current configuration
```

Use `-v` / `--verbose` with any command for debug output.

When you have multiple instances, commands that target a single instance prompt you to pick one. You can skip the prompt by passing a name or alias directly.

## Configuration

On first run, `vastly` creates `~/.vastly/config.json` with defaults:

```jsonc
{
  // "code" or "cursor" (auto-detected on first run; overridden when run inside an IDE terminal)
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
  // null = auto-detect (uv.lock/pyproject[tool.uv] -> pyproject[project] -> requirements*.txt -> setup.py)
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

User-specific keys (`ide`, `sshKeyPath`, `sshUser`, `disableAutoTmux`) are always read from the global `~/.vastly/config.json` and ignored in project configs.

> **Note:** `postInstall` and `installCommand` run as shell commands on your remote instance during setup. Review `.vastly.json` before running `vst` in unfamiliar repositories, just as you would review a `Makefile` or `package.json` scripts.

```jsonc
// .vastly.json (in repo root)
{
  "postInstall": ["make setup"],
  "copyFiles": [".claude/"]
}
```

## Authentication

To clone private repos or push to any repo, you need to authenticate with your git host:

- **SSH remotes** (`git@github.com:...`): Vastly enables SSH agent forwarding automatically. Your local SSH key is used for the duration of the connection -- never copied to the instance. Make sure your key is loaded in your SSH agent (`ssh-add -l` to check).
- **HTTPS remotes** (`https://github.com/...`): Public repos clone without authentication, but pushing requires a personal access token. You can configure one on the instance, or switch to an SSH remote: `git remote set-url origin git@github.com:user/repo.git`.

## Troubleshooting

**"Missing: vastai CLI"** -- `pip install vastai`, then `vastai set api-key <key>`.

**SSH connection timeout** -- Instance may still be booting. Setup retries 3 times. Run `vastai show instances` to check status.

**"Not in a git repo"** -- `vastly` reads the remote URL from your local repo. Run from inside a git repo, or use `vst --no-setup`.

**"Cannot access repo"** -- For SSH remotes, check that your SSH agent is running and your key is loaded (`ssh-add -l`). For HTTPS remotes, you may need a personal access token or to switch to SSH (see Authentication above).
