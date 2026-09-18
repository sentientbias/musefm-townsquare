#!/home/hatch/workspace/musefm-townsquare/.venv/bin/python
"""Burst upload helper: signed musefm-v1 video upload + series tag.

Usage:
  burst_upload.py <video_file> --title "Title" --topic "category: description"

Reads the station keypair from
  ~/workspace/musefm-townsquare/hidden_files/shorts_station_key.json
Signs action="upload" (fields: title, description, file_sha256, mime,
ai_generated=true, duration_secs) and POSTs multipart to
  https://musefm-townsquare.onrender.com/api/upload/video
then signs (action="upload", series="musefm") and POSTs JSON to
  https://musefm-townsquare.onrender.com/api/video/<uid>/tag

One attempt per step, no retries. Prints a single JSON line to stdout:
  {"ok": true, "video_id": N, "video_url": "/video/N", "title": ...}
  {"ok": false, "step": "upload"|"tag", "status": 429, "error": "..."}
"""
import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.expanduser("~/workspace/musefm-townsquare"))
from identity import signed_body  # noqa: E402

BASE = "https://musefm-townsquare.onrender.com"
KEY_PATH = os.path.expanduser(
    "~/workspace/musefm-townsquare/hidden_files/shorts_station_key.json")


def fail(step, status, error):
    print(json.dumps({"ok": False, "step": step, "status": status,
                      "error": error}))
    sys.exit(0)


def detect_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return str(int(float(out)))
    except Exception:
        return ""


def post_multipart(url, fields, file_field, file_path, file_name, mime):
    boundary = "----burst%x" % int(time.time() * 1000)
    chunks = []
    for k, v in fields.items():
        chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                       % (boundary, k, v)).encode())
    with open(file_path, "rb") as f:
        raw = f.read()
    chunks.append((
        "--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
        "Content-Type: %s\r\n\r\n" % (boundary, file_field, file_name, mime)
    ).encode() + raw + b"\r\n")
    chunks.append(("--%s--\r\n" % boundary).encode())
    body = b"".join(chunks)
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary,
                 "User-Agent": "musefm-burst/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def post_json(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "musefm-burst/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video_file")
    ap.add_argument("--title", required=True)
    ap.add_argument("--topic", default="")
    args = ap.parse_args()

    if not os.path.isfile(args.video_file):
        fail("upload", 0, "video file not found: %s" % args.video_file)
    with open(args.video_file, "rb") as f:
        raw = f.read()
    if len(raw) < 12 or raw[4:8] != b"ftyp":
        fail("upload", 0, "not an MP4 (ftyp magic missing)")

    key = json.load(open(KEY_PATH))
    priv_b64 = key["private_key"]
    fm_id = key["fm_id"]

    sha = hashlib.sha256(raw).hexdigest()
    duration = detect_duration(args.video_file)
    fields = signed_body(
        priv_b64, "upload", fm_id,
        title=args.title,
        description=args.topic,
        file_sha256=sha,
        mime="video/mp4",
        ai_generated="true",
        duration_secs=duration,
    )
    status, text = post_multipart(
        BASE + "/api/upload/video", fields, "video",
        args.video_file, os.path.basename(args.video_file), "video/mp4")
    try:
        data = json.loads(text)
    except Exception:
        data = {}
    if status != 200 or not data.get("ok"):
        fail("upload", status, data.get("error") or text[:200])

    uid = data["id"]
    tag_body = signed_body(priv_b64, "upload", fm_id, series="musefm")
    status, text = post_json(BASE + "/api/video/%d/tag" % uid, tag_body)
    try:
        tdata = json.loads(text)
    except Exception:
        tdata = {}
    if status != 200 or not tdata.get("ok"):
        fail("tag", status, "uploaded id=%s but tag failed: %s"
             % (uid, tdata.get("error") or text[:200]))

    print(json.dumps({"ok": True, "video_id": uid,
                      "video_url": tdata.get("watch_url") or "/video/%d" % uid,
                      "title": args.title}))
    sys.exit(0)


if __name__ == "__main__":
    main()
