# OpenClaw Setup Guide

Complete setup guide for installing and configuring OpenClaw on a new machine.

## Quick Start

```bash
# Clone the repository
git clone https://github.com/openclaw/openclaw.git
cd openclaw

# Run the full setup script
./scripts/setup-openclaw-full.sh
```

This will:
1. ✅ Check prerequisites (python3, rsync, git)
2. ✅ Install/copy repo to `~/.openclaw/workspace/openclaw`
3. ✅ Set up automated backups (launchd only)
4. ✅ Configure backup jobs to run every 4 hours

## What Gets Installed

### Backup System

The setup script installs an automated backup system that:

- **Backs up** `~/.openclaw/` directory to `.openclaw-backups/<timestamp>/`
- **Redacts** sensitive data (API keys, tokens, credentials)
- **Runs** every 4 hours via launchd
- **Commits** changes to git automatically

### Scheduled Jobs

**Launchd Job:**
- Service: `com.openclaw.backup`
- Interval: 14400 seconds (4 hours)
- Plist: `~/Library/LaunchAgents/com.openclaw.backup.plist`

## Manual Installation

If you prefer manual setup:

### 1. Prerequisites

```bash
# macOS
brew install python3 rsync git

# Verify installations
python3 --version
rsync --version
git --version
```

### 2. Clone Repository

```bash
mkdir -p ~/.openclaw/workspace
cd ~/.openclaw/workspace
git clone https://github.com/openclaw/openclaw.git
cd openclaw
```

### 3. Install Backup Jobs

```bash
./scripts/install-openclaw-backup-jobs.sh
```

This installs launchd jobs for backup automation and removes legacy OpenClaw cron entries.

## Verification

### Test Backup

```bash
cd ~/.openclaw/workspace/openclaw
./scripts/run-openclaw-backup.sh
```

Check for backup:
```bash
ls -la ~/.openclaw/workspace/openclaw/.openclaw-backups/
```

### Check Logs

```bash
# Backup logs
tail -f ~/Library/Logs/openclaw-backup/openclaw-backup.log

# Launchd stdout
tail -f ~/Library/Logs/openclaw-backup/stdout.log

# Launchd stderr
tail -f ~/Library/Logs/openclaw-backup/stderr.log
```

### Verify Jobs

```bash
# OpenClaw reminders/schedules (gateway cron only)
openclaw cron status
openclaw cron list

# Check launchd
launchctl list | grep openclaw
launchctl print gui/$(id -u)/com.openclaw.backup
```

## Backup Details

### What Gets Backed Up

- ✅ All files in `~/.openclaw/`
- ✅ Configuration files
- ✅ Workspace projects
- ✅ Skills and plugins

### What Gets Excluded

- ❌ `.openclaw-backups/` (prevents recursion)
- ❌ Binary files (`.sqlite`, `.db`, `.ipynb`, `.log`)
- ❌ `.DS_Store` files
- ❌ Sensitive key files (`.pem`, `.key`, `.p12`, etc.)

### Redaction

The backup script automatically redacts:
- API keys and tokens
- Environment variables with secrets
- OAuth tokens (Slack, GitHub, OpenAI, etc.)
- URLs with credentials
- PyPI tokens

See `scripts/backup-openclaw-full.sh` for full redaction patterns.

## Troubleshooting

### Backup Fails with "File name too long"

This was caused by recursive backup (backing up `.openclaw-backups/` into itself). Fixed in the current version by excluding `.openclaw-backups/` directory.

### Backup Fails with "FileNotFoundError"

Some files (like browser cache) are transient and may disappear during backup. The script now handles this gracefully and skips missing files.

### Launchd Job Not Running

```bash
# Reload the job
launchctl bootout gui/$(id -u)/com.openclaw.backup
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.openclaw.backup.plist
launchctl enable gui/$(id -u)/com.openclaw.backup

# Manually trigger
launchctl kickstart -k gui/$(id -u)/com.openclaw.backup
```

### Gateway Cron Job Not Running

```bash
# Check gateway cron status and jobs
openclaw cron status
openclaw cron list

# Manually run the backup
~/.openclaw/workspace/openclaw/scripts/run-openclaw-backup.sh
```

## Migrating to a New Machine

### Export Current Setup

Your entire OpenClaw setup is already being backed up! To migrate:

1. **Backup Current Machine:**
   - All backups are in `~/.openclaw/workspace/openclaw/.openclaw-backups/`
   - Optionally push to GitHub (if using git remote)

2. **On New Machine:**
   ```bash
   # Clone repo
   git clone https://github.com/your-fork/openclaw.git
   cd openclaw

   # Run setup
   ./scripts/setup-openclaw-full.sh

   # Restore your configuration
   # (Copy your actual ~/.openclaw/ files from backup if needed)
   ```

3. **Restore Data:**
   - Copy the latest `.openclaw-backups/<timestamp>/` contents to `~/.openclaw/`
   - Or restore from your Dropbox/cloud backup

## Additional Scripts

### `backup-openclaw-full.sh`
Main backup script with redaction logic.

### `run-openclaw-backup.sh`
Wrapper script that runs backup and commits to git.

### `install-openclaw-backup-jobs.sh`
Installs launchd backup jobs.

### `openclaw-backup.plist`
Launchd configuration for automated backups.

## See Also

- [docs/openclaw-backup-jobs.md](docs/openclaw-backup-jobs.md) - Detailed backup documentation
- [scripts/](scripts/) - All setup and backup scripts
