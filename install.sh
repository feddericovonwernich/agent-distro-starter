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
section '1 / 3  Choose your distro'
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
trap cleanup EXIT HUP INT TERM
/bin/mkdir -m 700 -- "$clone"
owned=true
section '2 / 3  Create your distro'
printf 'Cloning into %s\n\n' "$clone" >&2
git clone --quiet -- https://github.com/feddericovonwernich/agent-distro-starter.git "$clone"
(
    cd -- "$clone"
    PATH="$HOME/.local/bin:$PATH" bin/agd --home "$PWD" init --backend "$backend"
    section '3 / 3  Verify dependencies'
    PATH="$HOME/.local/bin:$PATH" bin/agd --home "$PWD" installer setup --backend "$backend"
)
printf '\n%sReady%s  %s\n' "$success$bold" "$reset" "$name" >&2
printf '  Location: %s\n  Backend:  %s\n\n' "$clone" "$backend" >&2
printf '  Start your central: %sbin/agd start%s\n' "$bold" "$reset" >&2
printf '  Exit this shell to return to your previous directory.\n\n' >&2
owned=false
trap - EXIT HUP INT TERM
cd -- "$clone"
export PATH="$HOME/.local/bin:$PATH"
exec bin/agd --home "$PWD" installer shell
