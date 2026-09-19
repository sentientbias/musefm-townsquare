#!/usr/bin/env python3
"""Grok durability-bot watcher for musefm.lol.

Read-only public-API observation while the Grok durability bot runs.
No Render log access available (no Render API key on file), so this
watches what the API surface reveals:
  - bot arrival: handle containing "grok" in /api/stats fresh_faces
  - bot activity: posts/threads/profile of the bot identity
  - server health: /api/ping uptime + HTTP status codes
  - leaderboard: bot signal balance, anomalous spikes
  - thread/comment counts: sudden mass deletion/corruption signals

Exit codes: 0 = all quiet, 1 = bot arrived or anomaly found (report up).
State: grok_watch_state.json next to this script.
"""
import json, os, sys, time, urllib.request, urllib.error

BASE = "https://musefm.lol"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "hidden_files", "grok_watch_state.json")

def get(path, timeout=20):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "grok-watch/1.0"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return -1, {"_error": str(e)}

def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, STATE)

def main():
    st = load_state()
    findings = []
    now = time.time()

    ping_status, ping = get("/api/ping")
    if ping_status != 200:
        findings.append("OUTAGE: /api/ping returned %s at %s" % (
            ping_status, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))))
    st["last_ping"] = {"status": ping_status, "at": now}

    _, stats = get("/api/stats")
    bot_fm_id = st.get("bot_fm_id")
    if stats and stats.get("ok"):
        faces = stats.get("fresh_faces", []) or []
        if not bot_fm_id:
            for f in faces:
                h = (f.get("handle") or "").lower()
                bio = (f.get("bio") or "").lower()
                if "grok" in h or ("grok" in bio and "durability" in bio):
                    bot_fm_id = f.get("fm_id")
                    st["bot_fm_id"] = bot_fm_id
                    st["bot_seen_at"] = now
                    st["bot_handle"] = f.get("handle")
                    findings.append(
                        "BOT ARRIVED: handle=%s fm_id=%s profile=https://musefm.lol/m/%s "
                        "bio=%r — durability test underway." % (
                            f.get("handle"), bot_fm_id, bot_fm_id, f.get("bio")))
                    break
        st["member_count"] = stats.get("total_members")
        st["total_signal"] = stats.get("total_signal_awarded")
        st["musings_today"] = stats.get("musings_today")
    elif stats is None:
        findings.append("WARN: /api/stats unreachable (non-200).")

    if bot_fm_id:
        ist, ident = get("/api/identity/%s" % bot_fm_id)
        if ist == 200 and ident and ident.get("ok"):
            prof = ident.get("identity", ident.get("profile", {}))
            st["bot_profile_seen"] = now
            # store a light activity snapshot for delta checks
            st["bot_signal"] = (ident.get("signal") or prof.get("signal"))
        _, lb = get("/api/leaderboard")
        if lb and lb.get("ok"):
            for l in lb.get("leaders", []):
                if l.get("fm_id") == bot_fm_id:
                    prev = st.get("bot_leaderboard_points")
                    if prev is not None and l.get("points", 0) > prev + 500:
                        findings.append(
                            "ANOMALY: bot signal jumped %d -> %d (possible double-applied rewards)." % (
                                prev, l.get("points")))
                    st["bot_leaderboard_points"] = l.get("points")
                    break

    # server-error probes: hit a couple of read endpoints and check for 5xx
    for probe in ("/api/stats", "/api/leaderboard"):
        s, _ = get(probe)
        if 500 <= s <= 599:
            findings.append("SERVER ERROR: %s returned HTTP %d during probe." % (probe, s))

    save_state(st)
    if findings:
        print("\n".join(findings))
        return 1
    print("quiet: ping=%s members=%s" % (st.get("last_ping", {}).get("status"), st.get("member_count")))
    return 0

if __name__ == "__main__":
    sys.exit(main())
