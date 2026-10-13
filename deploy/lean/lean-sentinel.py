#!/usr/bin/env python3
"""Lean desk sentinel (runs as root from a systemd timer every 5 min, independent of the coordinator's Mac).
Checks: discovery feed freshness, desk-lean active + health fresh + not halted. Self-heals by restarting
units and sends ONE Telegram alert per incident (state file). Never trades, never touches the stores."""
import json, os, sqlite3, subprocess, time, urllib.parse, urllib.request

DISC_DB = "/var/lib/solana-desk/discovery/continuous.sqlite"
HEALTH = "/var/lib/solana-desk-lean/health.json"
STATE = "/var/lib/lean-sentinel/state.json"
TG = "/etc/solana-desk/telegram.json"
STALE_DISC_S, STALE_HEALTH_S = 20 * 60, 180

def active(unit):
    return subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0

def restart(unit):
    subprocess.run(["systemctl", "restart", unit], timeout=60)

def tg(text):
    try:
        cfg = json.load(open(TG))
        data = urllib.parse.urlencode({"chat_id": cfg["chat_id"], "text": "[SENTINEL] " + text}).encode()
        urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % cfg["bot_token"], data=data, timeout=10).read()
    except Exception as e:  # never print the token or URL
        print("telegram failed:", type(e).__name__)

def main():
    now, problems, actions = time.time(), [], []
    try:
        c = sqlite3.connect("file:%s?mode=ro" % DISC_DB, uri=True, timeout=5)
        age = now - (c.execute("select max(received_at) from raw_events").fetchone()[0] or 0)
        c.close()
    except Exception as e:
        age = None
        problems.append("discovery db unreadable (%s)" % type(e).__name__)
    if not active("desk-continuous-discovery"):
        problems.append("discovery service down"); restart("desk-continuous-discovery"); actions.append("restarted discovery")
    elif age is not None and age > STALE_DISC_S:
        problems.append("discovery stale %d min" % (age // 60)); restart("desk-continuous-discovery"); actions.append("restarted discovery")
    if not active("desk-lean"):
        problems.append("desk-lean down"); restart("desk-lean"); actions.append("restarted desk-lean")
    else:
        try:
            h = json.load(open(HEALTH))
            if now - h["at"] > STALE_HEALTH_S:
                problems.append("health stale %ds" % (now - h["at"]))
            if h.get("halted"):
                problems.append("HALTED: %s" % str(h["halted"])[:120])
            if h.get("dead_loops"):
                problems.append("dead loops %s" % h["dead_loops"])
        except Exception as e:
            problems.append("health unreadable (%s)" % type(e).__name__)
    if not active("desk-lean-notify"):
        problems.append("notifier down"); restart("desk-lean-notify"); actions.append("restarted notifier")
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    try:
        prev = json.load(open(STATE))
    except Exception:
        prev = {"open": False}
    key = sorted(p.split(" ")[0] + p.split(" ")[1] for p in problems)
    if problems and (not prev.get("open") or prev.get("key") != key):
        tg("⚠️ " + "; ".join(problems) + (" → " + ", ".join(actions) if actions else ""))
    if not problems and prev.get("open"):
        tg("✅ recovered")
    json.dump({"open": bool(problems), "key": key, "at": now, "problems": problems}, open(STATE, "w"))
    print(json.dumps({"problems": problems, "actions": actions}))

if __name__ == "__main__":
    main()
