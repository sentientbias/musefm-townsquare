#!/usr/bin/env python3
"""Muse FM growth watch: new signups + new video uploads.

Polls the public read-only API surface of https://musefm.lol and reports
only what's new since the last run. Never posts, never writes.

State: hidden_files/growth_watch_state.json (seen handles, max video id).
Exit 0 = quiet, 1 = something new (report goes up via cron handoff).
"""
import json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

BASE = "https://musefm.lol"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "hidden_files", "growth_watch_state.json")


def get(path, timeout=25):
    req = urllib.request.Request(BASE + path,
                                 headers={"User-Agent": "musefm-growth-watch/1.0"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return -1, {"_error": str(e)}


def load_state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE))
        except Exception:
            pass
    return {"seen_handles": [], "max_video_id": 0, "last_run": 0}


def fmt_ts(ts):
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).astimezone().strftime(
            "%m-%d %H:%M")
    except Exception:
        return "?"


def main():
    st = load_state()
    seen = set(st.get("seen_handles", []))
    max_vid = int(st.get("max_video_id", 0))
    news = []

    # --- signups ---
    code, stats = get("/api/stats")
    if code == 200 and stats and "fresh_faces" in stats:
        for f in stats["fresh_faces"]:
            h = f.get("handle", "")
            if h and h not in seen:
                seen.add(h)
                news.append("SIGNUP @%s (%s)" % (h, fmt_ts(f.get("created_at", 0))))
        total = stats.get("total_members")
    else:
        total = None

    # --- uploads (track by id watermark; /api/shorts order is per-session shuffled) ---
    code, shorts = get("/api/shorts?limit=50")
    if code == 200 and shorts and "items" in shorts:
        items = shorts["items"]
        new_vids = [v for v in items if int(v.get("id", 0)) > max_vid]
        if new_vids:
            max_vid = max(max_vid, max(int(v.get("id", 0)) for v in items))
            for v in sorted(new_vids, key=lambda x: int(x["id"])):
                news.append("UPLOAD #%s '%s' by @%s (%s)" % (
                    v.get("id"), (v.get("title") or "")[:40],
                    v.get("handle"), fmt_ts(v.get("created_at", 0))))
    else:
        items = None

    st["seen_handles"] = sorted(seen)
    st["max_video_id"] = max_vid
    st["last_run"] = int(time.time())
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(st, open(STATE, "w"), indent=2)

    if news:
        print("Muse FM growth — %d new event(s)%s:" % (
            len(news), (" | total members: %s" % total) if total else ""))
        for n in news:
            print("  " + n)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
