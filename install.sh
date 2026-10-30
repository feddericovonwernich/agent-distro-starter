#!/usr/bin/env bash
set -euo pipefail

accent= success= error= bold= reset=
if [[ -t 1 && -t 2 && ${TERM:-dumb} != dumb && ! ${NO_COLOR+x} ]]; then
    accent=$'\e[36m' success=$'\e[32m' error=$'\e[31m' bold=$'\e[1m' reset=$'\e[0m'
fi
fail() { printf '%sError:%s %s\n' "$error" "$reset" "$1" >&2; exit 1; }
section() { printf '\n%s%s%s\n\n' "$accent$bold" "$1" "$reset" >&2; }

[[ -t 0 && -r /dev/tty ]] || fail 'Interactive terminal required'
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || fail 'Linux x86_64 required'
printf '\n%sAgent Distro Starter%s\n' "$bold" "$reset" >&2
section '1 / 5  Choose your distro'
read -r -p "${bold}Distro name:${reset} " name </dev/tty
[[ $name =~ ^[a-z][a-z0-9-]{0,47}$ ]] || fail 'Use 1-48 lowercase letters, digits or hyphens, starting with a letter'
[[ ! -e $name && ! -L $name ]] || fail 'Destination exists'
read -r -p "${bold}Backend${reset} (tmux/herdr): " backend </dev/tty
[[ $backend == tmux || $backend == herdr ]] || fail 'Backend must be tmux or herdr'

missing=()
command -v git >/dev/null || missing+=(git)
if ! command -v python3 >/dev/null; then
    missing+=(python3)
elif ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
    fail 'Python 3.12+ required'
fi
[[ $backend != tmux ]] || command -v tmux >/dev/null || missing+=(tmux)
if ((${#missing[@]})); then
    printf 'Missing requirements: %s\n' "${missing[*]}" >&2
    read -r -p "${bold}Use sudo to install them?${reset} [y/N] " answer </dev/tty
    [[ $answer == y || $answer == Y || $answer == yes || $answer == YES ]] || exit 1
    command -v apt >/dev/null || fail 'APT required'
    sudo -v
    sudo apt update
    sudo apt install -y "${missing[@]}"
fi
command -v git >/dev/null || fail 'Git required'
command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' ||
    fail 'Python 3.12+ required'
[[ $backend != tmux ]] || command -v tmux >/dev/null || fail 'tmux required'

clone=$PWD/$name
owned=false
cleanup() { $owned && /bin/rm -rf -- "$clone"; }
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
/bin/mkdir -m 700 -- "$clone"
owned=true
section '2 / 5  Create your distro'
printf 'Cloning into %s\n\n' "$clone" >&2
git clone --quiet -- https://github.com/feddericovonwernich/agent-distro-starter.git "$clone"
(
    cd -- "$clone"
    PATH="$HOME/.local/bin:$PATH" bin/agd --home "$PWD" init --backend "$backend"
    section '3 / 5  Verify dependencies'
    PATH="$HOME/.local/bin:$PATH" bin/agd --home "$PWD" installer setup --backend "$backend"
)
# Optional provisioning may create Docker resources that need this home for recovery.
owned=false
(
    cd -- "$clone"
    export PATH="$HOME/.local/bin:$PATH"
    section '4 / 5  Configure observability'
    printf 'Optional local Langfuse + Collector stack (Docker required; no Docker installation or sudo).\n' >&2
    printf 'Pulls images and uses disk/memory. Captures sensitive prompts, instructions, and tool inputs/results.\n' >&2
    printf 'Services are loopback-only, not Langfuse Cloud. Local session recording works without export.\n\n' >&2
    answer=
    read -r -p "${bold}Enable local Langfuse telemetry?${reset} [y/N] " answer </dev/tty || answer=
    if [[ ${answer,,} == y || ${answer,,} == yes ]]; then
        if bin/agd --home "$PWD" observability enable; then
            printf 'Telemetry status shown above; stack health alone does not confirm trace delivery.\n' >&2
        else
            printf '%sWarning:%s Telemetry setup incomplete; distro and recovery files retained.\n' "$error" "$reset" >&2
            bin/agd --home "$PWD" observability status ||
                printf '%sWarning:%s Telemetry status unavailable; partial services may still exist.\n' "$error" "$reset" >&2
        fi
        printf '  Inspect: bin/agd observability status\n  Stop and disable: bin/agd observability disable\n' >&2
    else
        printf 'Telemetry: disabled (local session recording remains available).\n' >&2
    fi
    printf '  Enable or retry later: bin/agd observability enable\n' >&2
    section '5 / 5  Configure scheduled reviewer'
    bin/agd --home "$PWD" installer reviewer ||
        printf '%sWarning:%s Reviewer setup incomplete; distro and recovery records retained.\n  Retry: bin/agd installer reviewer\n  Inspect: bin/agd reviewer timer status\n  Disable: bin/agd reviewer timer disable\n' "$error" "$reset" >&2
)
printf '\n%sReady%s  %s\n' "$success$bold" "$reset" "$name" >&2
printf '  Location: %s\n  Backend:  %s\n\n' "$clone" "$backend" >&2
printf '  Start your central: %sbin/agd start%s\n' "$bold" "$reset" >&2
printf '  Exit this shell to return to your previous directory.\n\n' >&2
trap - EXIT HUP INT TERM
cd -- "$clone"
export PATH="$HOME/.local/bin:$PATH"
exec bin/agd --home "$PWD" installer shell
