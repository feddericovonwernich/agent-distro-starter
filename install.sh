#!/usr/bin/env bash
set -euo pipefail

[[ -t 0 && -r /dev/tty ]] || { echo 'Interactive terminal required' >&2; exit 1; }
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || { echo 'Linux x86_64 required' >&2; exit 1; }
read -r -p 'Distro name: ' name </dev/tty
[[ $name =~ ^[a-z][a-z0-9-]{0,47}$ ]] || { echo 'Invalid distro name' >&2; exit 1; }
[[ ! -e $name && ! -L $name ]] || { echo 'Destination exists' >&2; exit 1; }
read -r -p 'Backend (tmux/herdr): ' backend </dev/tty
[[ $backend == tmux || $backend == herdr ]] || { echo 'Backend must be tmux or herdr' >&2; exit 1; }

missing=()
command -v git >/dev/null || missing+=(git)
if ! command -v python3 >/dev/null; then
    missing+=(python3)
elif ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
    echo 'Python 3.12+ required' >&2
    exit 1
fi
[[ $backend != tmux ]] || command -v tmux >/dev/null || missing+=(tmux)
if ((${#missing[@]})); then
    printf 'Missing requirements: %s\n' "${missing[*]}" >&2
    read -r -p 'Use sudo to install them? [y/N] ' answer </dev/tty
    [[ $answer == y || $answer == Y || $answer == yes || $answer == YES ]] || exit 1
    command -v apt >/dev/null || { echo 'APT required' >&2; exit 1; }
    sudo -v
    apt update
    apt install -y "${missing[@]}"
fi
command -v git >/dev/null || { echo 'Git required' >&2; exit 1; }
command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' ||
    { echo 'Python 3.12+ required' >&2; exit 1; }
[[ $backend != tmux ]] || command -v tmux >/dev/null || { echo 'tmux required' >&2; exit 1; }

clone=$PWD/$name
owned=false
cleanup() { $owned && /bin/rm -rf -- "$clone"; }
trap cleanup EXIT HUP INT TERM
git clone -- https://github.com/feddericovonwernich/agent-distro-starter.git "$clone"
owned=true
(
    cd -- "$clone"
    PATH="$HOME/.local/bin:$PATH" bin/agd --home "$PWD" init --backend "$backend"
    PATH="$HOME/.local/bin:$PATH" bin/agd --home "$PWD" installer setup --backend "$backend"
)
read -r -p 'OMP login provider (blank to skip): ' provider </dev/tty
if [[ -n $provider ]]; then
    (cd -- "$clone" && PATH="$HOME/.local/bin:$PATH" omp login "$provider")
fi
read -r -p 'Show selectable/configured OMP models? [y/N] ' models </dev/tty
if [[ $models == y || $models == Y || $models == yes || $models == YES ]]; then
    (cd -- "$clone" && PATH="$HOME/.local/bin:$PATH" omp models --json)
fi
owned=false
trap - EXIT HUP INT TERM
cd -- "$clone"
export PATH="$HOME/.local/bin:$PATH"
exec "${SHELL:-/bin/sh}" -i
