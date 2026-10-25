#!/usr/bin/env bash
set -euo pipefail

command -v git >/dev/null || { echo 'Git required' >&2; exit 1; }
command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' ||
    { echo 'Python 3.12+ required' >&2; exit 1; }
[[ -t 0 && -r /dev/tty ]] || { echo 'Interactive terminal required' >&2; exit 1; }
read -r -p 'Distro name: ' name </dev/tty
[[ $name =~ ^[a-z][a-z0-9-]{0,47}$ ]] || { echo 'Invalid distro name' >&2; exit 1; }
[[ ! -e $name && ! -L $name ]] || { echo 'Destination exists' >&2; exit 1; }

git clone -- https://github.com/feddericovonwernich/agent-distro-starter.git "$name"
(cd -- "./$name" && bin/agd --home "$PWD" init)
printf 'Distro ready: cd -- ./%q\n' "$name"
