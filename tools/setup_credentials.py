"""Run in your own terminal. Prompts are hidden; API keys never enter shell history."""
import argparse
import getpass
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--path", default="var/provider-keys.json")
args = parser.parse_args()
path = Path(args.path)
path.parent.mkdir(parents=True, exist_ok=True)
data = json.loads(path.read_text()) if path.exists() else {}
for name in ("HELIUS_API_KEY", "JUPITER_API_KEY"):
    value = getpass.getpass(f"{name} (Enter keeps existing or skips): ").strip()
    if value:
        if any(c in value for c in "\r\n\0"):
            raise SystemExit("Invalid key format")
        data[name] = value
tmp = path.with_suffix(".tmp")
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as stream:
    json.dump(data, stream)
    stream.flush()
    os.fsync(stream.fileno())
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print(f"Saved API credentials to {path} with owner-only permissions. No wallet key is needed.")
