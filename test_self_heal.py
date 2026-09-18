"""Direct (non-pytest) exercise of _self_heal_media.

Builds a throwaway DB + DATA_DIR, inserts:
  - approved video with a structurally-valid file  -> must stay approved
  - approved video with a corrupt (truncated) file -> must flip to rejected
  - approved video with a missing file             -> must flip to rejected
  - approved image with a missing file             -> must flip to rejected
  - pending video with a missing file              -> must stay pending
Then runs the heal synchronously twice (idempotency).
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import app as appmod
from db import Database
import videos
import ai_images


def make_mp4(n=5000):
    return (b"\x00\x00\x00\x1c" + b"ftyp" + b"isom" + b"\x00" * 16 +
            b"\x00\x00\x00\x08" + b"moov" + bytes(n))


def make_png():
    import struct, zlib
    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\xff\x00\x00"
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def main():
    tmp = tempfile.mkdtemp(prefix="musefm-heal-")
    tdb_path = os.path.join(tmp, "t.db")
    tdata = os.path.join(tmp, "data")
    updir = os.path.join(tdata, "uploads")
    os.makedirs(updir, exist_ok=True)

    real_db, real_dir = appmod.db, appmod.DATA_DIR
    tdb = Database(tdb_path)
    videos.ensure_video_schema(tdb)
    ai_images.ensure_ai_schema(tdb)
    appmod.db = tdb
    appmod.DATA_DIR = tdata

    try:
        vgood, _ = videos.create_video_upload(
            tdb, "fm_t", "t", "good.mp4", make_mp4(), updir,
            ai_generated=True, status="approved")
        vcorrupt, _ = videos.create_video_upload(
            tdb, "fm_t", "t", "ok.mp4", make_mp4(), updir,
            ai_generated=True, status="approved")
        # corrupt the file after upload (simulates a truncated file on disk)
        vcorrupt_path = tdb._one(
            "SELECT stored_path s FROM video_uploads WHERE id=?",
            (vcorrupt,))["s"]
        with open(os.path.join(tdata, vcorrupt_path), "wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftyp" + b"\x00" * 5000)
        vmissing_id = tdb._exec(
            "INSERT INTO video_uploads (fm_id, handle, filename, stored_path,"
            " bytes, mime, ai_generated, created_at, status)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("fm_t", "t", "gone.mp4", "uploads/vid-999.mp4", 72,
             "video/mp4", 1, 1, "approved")).lastrowid
        imissing_id = tdb._exec(
            "INSERT INTO ai_uploads (fm_id, handle, filename, stored_path,"
            " bytes, mime, ai_generated, created_at, status)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("fm_t", "t", "gone.png", "uploads/img-999.png", 10,
             "image/png", 1, 1, "approved")).lastrowid
        vpending_id = tdb._exec(
            "INSERT INTO video_uploads (fm_id, handle, filename, stored_path,"
            " bytes, mime, ai_generated, created_at, status)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("fm_t", "t", "pend.mp4", "uploads/vid-998.mp4", 72,
             "video/mp4", 1, 1, "pending")).lastrowid

        appmod._self_heal_media()   # run 1
        appmod._self_heal_media()   # run 2: idempotency

        st = lambda table, i: tdb._one(
            "SELECT status s FROM %s WHERE id=?" % table, (i,))["s"]
        assert st("video_uploads", vgood) == "approved", "good video must stay approved"
        assert st("video_uploads", vcorrupt) == "rejected", "corrupt video must flip"
        assert st("video_uploads", vmissing_id) == "rejected", "missing video must flip"
        assert st("ai_uploads", imissing_id) == "rejected", "missing image must flip"
        assert st("video_uploads", vpending_id) == "pending", "pending must stay pending"
        print("SELF-HEAL OK: good stays approved; corrupt/missing -> rejected;"
              " pending untouched; idempotent")
        return 0
    finally:
        appmod.db, appmod.DATA_DIR = real_db, real_dir
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
