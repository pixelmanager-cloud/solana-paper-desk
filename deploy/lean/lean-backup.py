#!/usr/bin/env python3
"""Nightly consistent backups of the lean desk stores (sqlite online-backup API; safe while services write).
Keeps KEEP_DAYS daily sets under /var/backups/lean/<UTC date>/ (root 0700), gzip-compressed. Secrets are NOT copied."""
import datetime, gzip, os, shutil, sqlite3, tarfile, time

DBS = {"lean": "/var/lib/solana-desk-lean/lean.sqlite", "paths": "/var/lib/solana-desk-lean/paths.sqlite",
       "discovery": "/var/lib/solana-desk/discovery/continuous.sqlite"}
ROOT, KEEP_DAYS = "/var/backups/lean", 7

def main():
    day = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    out = os.path.join(ROOT, day); tmp = out + ".partial"
    shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp, mode=0o700)
    report = {}
    for name, path in DBS.items():
        t0 = time.time(); raw = os.path.join(tmp, name + ".sqlite")
        src = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30); dst = sqlite3.connect(raw)
        src.backup(dst, pages=4096, sleep=0.05); dst.close(); src.close()
        chk = sqlite3.connect(raw); chk.execute("pragma journal_mode=DELETE")
        ok = chk.execute("pragma quick_check").fetchone()[0]; chk.close()
        for side in ("-wal", "-shm"):
            if os.path.exists(raw + side): os.remove(raw + side)
        with open(raw, "rb") as f, gzip.open(raw + ".gz", "wb", compresslevel=6) as g:
            shutil.copyfileobj(f, g, 1 << 20)
        os.remove(raw)
        report[name] = {"check": ok, "gz_mb": round(os.path.getsize(raw + ".gz") / 1e6, 1), "s": round(time.time() - t0, 1)}
    with tarfile.open(os.path.join(tmp, "etc-solana-paper.tgz"), "w:gz") as t:
        t.add("/etc/solana-paper", arcname="etc/solana-paper")
    shutil.rmtree(out, ignore_errors=True); os.rename(tmp, out)
    days = sorted(d for d in os.listdir(ROOT) if len(d) == 10 and d[4] == "-")
    for d in days[:-KEEP_DAYS]:
        shutil.rmtree(os.path.join(ROOT, d), ignore_errors=True)
    with open(os.path.join(ROOT, "last.json"), "w") as f:
        import json; json.dump({"day": day, "at": time.time(), "report": report}, f)
    print(report)

if __name__ == "__main__":
    os.makedirs(ROOT, mode=0o700, exist_ok=True); main()
