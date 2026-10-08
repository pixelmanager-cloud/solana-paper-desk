#!/bin/zsh
set -eu
if ! curl --silent --fail --max-time 2 http://127.0.0.1:8765/api/status >/dev/null; then
  ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -fNT -L 127.0.0.1:8765:127.0.0.1:8765 root@158.247.196.20
fi
open http://127.0.0.1:8765
