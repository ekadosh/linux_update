# Proxmox Ansible Update System

This project updates Debian-family Linux hosts through Ansible. It discovers
tagged Ubuntu QEMU VMs from the Proxmox API, can also include manually listed
non-Proxmox Debian/Ubuntu servers, creates pre-update snapshots for Proxmox VMs,
applies safe apt upgrades, and reboots each host when the OS reports that a
reboot is required.

The update playbook runs with `serial: 1`, so only one VM is being changed at a
time.

## Setup

1. Create the control-node environment:

   ```bash
   make bootstrap
   ```

2. Create your local environment file:

   ```bash
   cp .env.example .env
   ```

3. Edit `.env` with your Proxmox API endpoint, token, and SSH user.

4. In Proxmox, tag each VM that may be updated:

   ```text
   ansible-update
   ```

   To update a VM but suppress automatic reboots, also add:

   ```text
   no-reboot
   ```

5. Make sure each target has:

   - Debian or Ubuntu installed.
   - QEMU guest agent installed and running for Proxmox VMs.
   - SSH reachable from this control node.
   - The configured SSH user allowed to run passwordless sudo.

## Add Non-Proxmox Debian Hosts

Add external Debian or Ubuntu servers to `inventory/static_hosts.yml`. They use
the same `ANSIBLE_SSH_USER` and `ANSIBLE_PRIVATE_KEY_FILE` values from `.env`.

Example:

```yaml
all:
  children:
    linux_update_targets:
      children:
        external_debian:
          hosts:
            debian-fileserver:
              ansible_host: debian-fileserver.domain.com
              no_reboot: true
```

Validate this file with:

```bash
make static-inventory-check
```

Non-Proxmox hosts are updated with apt like the Proxmox VMs, but snapshot and
snapshot-pruning tasks are skipped because they do not have Proxmox metadata.
Set `no_reboot: true` on a static host to suppress automatic reboots.

## Create the Ansible VM User

Create a dedicated SSH key for Ansible:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/ansible_ed25519 -C ansible-homelab
```

Then use your existing SSH account to create the `ansible` user on each VM:

```bash
scripts/create_ansible_user.sh --admin-user <your-current-user> --public-key ~/.ssh/ansible_ed25519.pub vm1 vm2 vm3
```

If you have many hosts, put one hostname or IP per line in a file:

```bash
scripts/create_ansible_user.sh --admin-user <your-current-user> --hosts-file hosts.txt
```

If your current SSH access uses a non-default private key:

```bash
scripts/create_ansible_user.sh --admin-user <your-current-user> --identity-file ~/.ssh/current_admin_key --hosts-file hosts.txt
```

The script is idempotent. It creates or updates the user, installs the public
key, adds the user to `sudo`, and writes `/etc/sudoers.d/90-ansible` with
passwordless sudo. It also tests `ssh -i ~/.ssh/ansible_ed25519 ansible@host
sudo -n true` after each host unless you pass `--skip-verify`. Afterward, set
these values in `.env`:

```text
ANSIBLE_SSH_USER=ansible
ANSIBLE_PRIVATE_KEY_FILE=/Users/you/.ssh/ansible_ed25519
```

Test one VM directly:

```bash
ssh -i ~/.ssh/ansible_ed25519 ansible@vm1 sudo -n true
```

If SSH connects but hangs during key exchange from this runner, leave
`SSH_COMPATIBILITY_MODE=auto` in `.env`. Update runs first check connectivity
with normal SSH options, then retry the preflight with:

```text
-o ControlMaster=no -o KexAlgorithms=curve25519-sha256 -o HostKeyAlgorithms=ssh-ed25519 -o IPQoS=none
```

When the fallback succeeds, the playbook runs once with those compatibility
options. To force the workaround for every scheduled run, set:

```text
SSH_COMPATIBILITY_MODE=always
```

The SSH preflight has its own timeouts so a key-exchange stall does not leave
the run looking frozen:

```text
SSH_PREFLIGHT_CONNECT_TIMEOUT=10
SSH_PREFLIGHT_WALL_TIMEOUT=45
```

## Proxmox Permissions

Use a Proxmox API token with the narrowest permissions that work for your
cluster. For Proxmox VE 8, the practical role for `/vms` is usually:

```text
VM.Audit VM.Snapshot VM.Monitor
```

For Proxmox VE 9, guest-agent privileges are more granular; use the guest-agent
audit privilege instead of `VM.Monitor` if your cluster rejects `VM.Monitor`.

The inventory script accepts token auth through:

```text
PROXMOX_USER
PROXMOX_TOKEN_ID
PROXMOX_TOKEN_SECRET
```

For `PROXMOX_TOKEN_ID`, prefer only the token name, such as `ansible`. If you
paste a full `user@realm!token` value, the inventory script will use the part
after `!`.

## Commands

List discovered hosts:

```bash
make inventory
```

Check SSH connectivity:

```bash
make ping
```

Seed SSH host keys for all discovered VMs:

```bash
make known-hosts
```

Create the dedicated VM user with the helper script:

```bash
make create-user EXTRA_ARGS="--admin-user <your-current-user> --hosts-file hosts.txt"
```

Run a dry run:

```bash
make dry-run
```

Run updates:

```bash
make update
```

Apt tasks run noninteractively and have a one-hour timeout by default. Change
`apt_task_timeout` in `group_vars/all.yml` if a slow host needs more or less
time. Apt lock waits use `apt_lock_timeout`, which defaults to ten minutes. The
playbook runs `apt-get update` as a separate task so repository failures show
the underlying apt stdout/stderr in the run log.

Before upgrades, the playbook runs `apt-get clean`, checks free space under
`/var/cache/apt/archives`, and fails early if less than
`apt_min_archive_free_mb` is available. The default is 512 MB and can be
overridden globally in `group_vars/all.yml` or per host in inventory.

Limit to one host:

```bash
make update LIMIT=my-vm-name
```

Or pass raw Ansible arguments:

```bash
make update EXTRA_ARGS="--limit my-vm-name"
scripts/run_updates.sh --limit my-vm-name
```

Install the weekly Sunday 03:00 cron entry:

```bash
make install-cron
```

Logs are written to `logs/update-YYYYMMDD-HHMMSS.log`. Each scheduled run
re-runs the dynamic inventory and refreshes SSH `known_hosts` before applying
updates, so newly tagged Proxmox VMs are picked up automatically.

## Email Alerts

`scripts/run_updates.sh` sends an email after each run, including cron runs. It
uses the SMTP relay settings from `.env` and includes the run result plus the
tail of the Ansible log.

For your relay:

```text
ALERT_EMAIL_ENABLED=true
ALERT_EMAIL_TO=you@example.com
ALERT_EMAIL_FROM=ansible-updates@domain.com
SMTP_RELAY_HOST=smtp.domain.com
SMTP_RELAY_PORT=587
SMTP_RELAY_STARTTLS=false
```

If `ALERT_EMAIL_TO` is unset, the run still completes and email is skipped. If
email sending fails, the script prints `Email alert failed` but still exits with
the original Ansible status.

## Reboot Behavior

When Debian or Ubuntu creates `/var/run/reboot-required`, the playbook reboots
that host automatically after updates. If a Proxmox VM has the Proxmox tag
`no-reboot`, or a static host has `no_reboot: true`, Ansible does not reboot it
and writes a message to the run log instead.

## Snapshot Behavior

Each run creates snapshots named like:

```text
ansible-pre-update-YYYYMMDD-HHMMSS
```

Snapshots do not include RAM (`vmstate: false`). The playbook keeps the newest
three snapshots created by this automation and prunes only snapshots whose names
start with `ansible-pre-update-`.

## Notes

- This is for package updates, not Ubuntu release upgrades.
- Host key checking is enabled. If a VM is new, run `make known-hosts` before
  `make ping` or `make update`.
- The cron job runs from this project directory and uses `.env`, so keep that
  file readable only by the account running cron.
