#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/create_ansible_user.sh [options] host1 host2 ...
  scripts/create_ansible_user.sh [options] --hosts-file hosts.txt

Options:
  --admin-user USER       Existing SSH user with sudo access. Defaults to $USER.
  --ansible-user USER     Account to create on each VM. Defaults to ansible.
  --public-key PATH       Public key to install. Defaults to ~/.ssh/ansible_ed25519.pub.
  --identity-file PATH    Existing private key for the admin SSH user.
  --skip-verify           Do not test SSH as the Ansible user after provisioning.
  --hosts-file PATH       File containing one host/IP per line. Blank lines and # comments are ignored.
  -h, --help              Show this help.

Examples:
  ssh-keygen -t ed25519 -f ~/.ssh/ansible_ed25519 -C ansible-homelab
  scripts/create_ansible_user.sh --admin-user ubuntu --public-key ~/.ssh/ansible_ed25519.pub vm1 vm2
  scripts/create_ansible_user.sh --admin-user ubuntu --hosts-file hosts.txt
USAGE
}

admin_user="${USER:-}"
ansible_user="ansible"
public_key_file="$HOME/.ssh/ansible_ed25519.pub"
identity_file=""
hosts_file=""
verify_ssh=true
hosts=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --admin-user)
      admin_user="${2:?missing value for --admin-user}"
      shift 2
      ;;
    --ansible-user)
      ansible_user="${2:?missing value for --ansible-user}"
      shift 2
      ;;
    --public-key)
      public_key_file="${2:?missing value for --public-key}"
      shift 2
      ;;
    --identity-file)
      identity_file="${2:?missing value for --identity-file}"
      shift 2
      ;;
    --skip-verify)
      verify_ssh=false
      shift
      ;;
    --hosts-file)
      hosts_file="${2:?missing value for --hosts-file}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      hosts+=("$@")
      break
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      hosts+=("$1")
      shift
      ;;
  esac
done

if [[ -z "$admin_user" ]]; then
  echo "Unable to determine admin user. Pass --admin-user." >&2
  exit 2
fi

if [[ ! -f "$public_key_file" ]]; then
  echo "Public key not found: $public_key_file" >&2
  echo "Create one with: ssh-keygen -t ed25519 -f ~/.ssh/ansible_ed25519 -C ansible-homelab" >&2
  exit 2
fi

if [[ -n "$hosts_file" ]]; then
  if [[ ! -f "$hosts_file" ]]; then
    echo "Hosts file not found: $hosts_file" >&2
    exit 2
  fi
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -n "$line" ]] && hosts+=("$line")
  done < "$hosts_file"
fi

if [[ ${#hosts[@]} -eq 0 ]]; then
  echo "No hosts supplied." >&2
  usage >&2
  exit 2
fi

public_key="$(<"$public_key_file")"
public_key_b64="$(printf '%s' "$public_key" | base64 | tr -d '\n')"
ansible_identity_file="${public_key_file%.pub}"
ssh_args=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
if [[ -n "$identity_file" ]]; then
  ssh_args+=(-i "$identity_file")
fi

for host in "${hosts[@]}"; do
  echo "Configuring $ansible_user on $host"
  ssh "${ssh_args[@]}" "$admin_user@$host" "bash -s" -- "$ansible_user" "$public_key_b64" <<'REMOTE'
set -euo pipefail

ansible_user="$1"
public_key="$(printf '%s' "$2" | base64 --decode)"
sudoers_file="/etc/sudoers.d/90-$ansible_user"

if ! id -u "$ansible_user" >/dev/null 2>&1; then
  sudo useradd --create-home --shell /bin/bash --groups sudo "$ansible_user"
else
  sudo usermod --append --groups sudo "$ansible_user"
fi

# useradd leaves the password field locked on some distros, and sshd can reject
# even public-key auth with "account is locked". Use an impossible password hash
# so password login stays disabled while the account remains valid for SSH keys.
sudo usermod --password '*' "$ansible_user"

home_dir="$(getent passwd "$ansible_user" | cut -d: -f6)"
if [[ -z "$home_dir" ]]; then
  echo "Unable to determine home directory for $ansible_user" >&2
  exit 1
fi

ssh_dir="$home_dir/.ssh"
authorized_keys="$ssh_dir/authorized_keys"

sudo install -d -m 700 -o "$ansible_user" -g "$ansible_user" "$ssh_dir"
sudo touch "$authorized_keys"
sudo chown "$ansible_user:$ansible_user" "$authorized_keys"
sudo chmod 600 "$authorized_keys"

# Clean up malformed key-type-only lines from older versions of this helper.
sudo sed -i \
  -e '/^ssh-ed25519$/d' \
  -e '/^ssh-rsa$/d' \
  -e '/^ecdsa-sha2-nistp[0-9][0-9]*$/d' \
  "$authorized_keys"

if ! sudo grep -qxF "$public_key" "$authorized_keys"; then
  printf '%s\n' "$public_key" | sudo tee -a "$authorized_keys" >/dev/null
fi

printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$ansible_user" | sudo tee "$sudoers_file" >/dev/null
sudo chmod 440 "$sudoers_file"
sudo visudo -cf "$sudoers_file" >/dev/null

echo "ok: $ansible_user created and configured at $home_dir"
REMOTE

  if [[ "$verify_ssh" == "true" ]]; then
    if [[ ! -f "$ansible_identity_file" ]]; then
      echo "warn: cannot verify SSH; private key not found: $ansible_identity_file" >&2
      continue
    fi
    ssh -i "$ansible_identity_file" \
      -o BatchMode=yes \
      -o StrictHostKeyChecking=accept-new \
      "$ansible_user@$host" "sudo -n true"
    echo "ok: verified $ansible_user@$host can authenticate and run passwordless sudo"
  fi
done

echo "Done. Test with: ssh -i ${public_key_file%.pub} $ansible_user@<host> sudo -n true"
