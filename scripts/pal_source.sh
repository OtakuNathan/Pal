#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(dirname -- "$script_dir")"

export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"
python_bin="${PAL_PYTHON:-python3}"
exec "$python_bin" -m pal.main "$@"
