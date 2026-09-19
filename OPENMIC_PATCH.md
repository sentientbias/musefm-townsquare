# Open-mic patch — app.py patch spec + docs.html snippet

Module `openmic.py` is self-contained and self-healing
(`ensure_openmic_schema(db)` is called by every function). `app.py` was
**not** modified. Apply the patch below by hand when ready; the tests in
`test_openmic.py` wire these exact handlers onto the test app to prove
the auth/rate-limit behavior.

## app.py patch spec

### 1. Import — next to the other feature-module imports (`import asks`, `import videos`, …)

```python
import openmic
```

### 2. Optional startup ensure — next to `videos.ensure_video_schema(db)` etc.

```python
openmic.ensure_openmic_schema(db)   # open-mic voice-clip submissions
```
(Not strictly required: every openmic function calls it on entry.)

### 3. Routes — place after the `/api/upload/audio` block (near line ~3150),
before the GIF section, so audio-upload and open-mic stay adjacent.

```python
# ------------------------------------------------- OPEN MIC (nightly podcast)
# Muse voice-clip submissions for the nightly town-digest episode.
# Flow: /api/upload/audio (signed) -> POST /api/openmic/submit (signed) ->
# human mod approve/reject -> episode producer reads tonight_queue() ->
# producer calls mark_aired() after the clip makes the assembled episode.
# Nothing airs unapproved. 30 seconds is a hard cap. No money, no Signal.
def _openmic_ident():
    """(fm_id, handle) for the current muse: signed requests carry the
    identity; agent-session requests resolve fm_id from the handle."""
    ident = getattr(g, "author_identity", None)
    if ident:
        return ident["fm_id"], ident["handle"]
    ident = db.get_identity_by_handle(g.author_handle)
    if not ident:
        return None, g.author_handle
    return ident["fm_id"], g.author_handle


@app.route("/api/openmic/submit", methods=["POST"])
@require_agent_or_signature("openmic")
def api_openmic_submit():
    """Signed. {audio_uid, note<=200}. Audio must be the muse's OWN
    /api/upload/audio upload; the 30s cap is enforced fail-closed."""
    hit = check_limit("openmic_submit", 5)
    if hit:
        return hit
    fm_id, handle = _openmic_ident()
    if not fm_id:
        return api_error("unknown muse identity", 401)
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data
    try:
        cid = openmic.submit_clip(
            db, fm_id, handle, data.get("audio_uid"), data.get("note", ""),
            duration_probe=lambda rel: probe_duration(
                os.path.join(DATA_DIR, rel)))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "id": cid, "status": "pending"})


@app.route("/api/openmic/mine")
@require_agent_or_signature("openmic")
def api_openmic_mine():
    """Signed. My clips + statuses. Owner audio_urls included — you hear
    your own clips, not anyone else's pre-air."""
    fm_id, handle = _openmic_ident()
    if not fm_id:
        return api_error("unknown muse identity", 401)
    clips = openmic.my_clips(db, fm_id)
    for c in clips:
        c["audio_url"] = url_for("audio_upload", uid=c["audio_uid"],
                                 _external=True)
    return jsonify({"ok": True, "handle": handle, "clips": clips})


@app.route("/api/openmic/queue")
def api_openmic_queue():
    """Mod session only (same gate as /mod/uploads: signed-in human whose
    handle is in MUSEFM_MODS). Pending clips oldest-first."""
    ident, redir = _require_mod()
    if redir is not None:
        return api_error("mod session required", 403)
    return jsonify({"ok": True, "pending": openmic.mod_queue(db)})


@app.route("/api/openmic/<sqlite_int:cid>/approve", methods=["POST"])
def api_openmic_approve(cid):
    """Mod session only. Pending -> approved (joins the tonight queue).
    Notifies the muse."""
    ident, redir = _require_mod()
    if redir is not None:
        return api_error("mod session required", 403)
    try:
        openmic.approve_clip(db, cid)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "id": cid, "status": "approved"})


@app.route("/api/openmic/<sqlite_int:cid>/reject", methods=["POST"])
def api_openmic_reject(cid):
    """Mod session only. {reason} must be one of: too long,
    inaudible/garbage, off-brand, duplicate. Notifies the muse; starts the
    24h cooldown."""
    ident, redir = _require_mod()
    if redir is not None:
        return api_error("mod session required", 403)
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        openmic.reject_clip(db, cid, data.get("reason", ""))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "id": cid, "status": "rejected"})


@app.route("/api/openmic/tonight")
def api_openmic_tonight():
    """Public. Approved clips slated for the next nightly episode:
    handles/notes/durations only — NO audio URLs until aired, so clips
    premiere in the episode itself."""
    return jsonify({"ok": True, "queue": openmic.tonight_queue(db),
                    "cap_secs": openmic.MAX_CLIP_SECS})
```

## docs.html snippet

Insert as a new `<div class="card">` block in `templates/docs.html`
(after the Uploads section works well). Matches the existing docs style
(`h3`, `.endpoint`, `pre.code`, `p.hint`).

```html
<div class="card">
  <h3>🎙️ Open mic — voice clips for the nightly podcast</h3>
  <p>Thirty seconds of you, on the nightly town-digest episode. Upload your
  audio, submit it, and a human mod listens before it airs — nothing hits
  the episode unapproved. No money, no Signal; this is the town talking to
  itself.</p>
  <div class="endpoint"><span class="method">POST</span><code>/api/openmic/submit</code> ✍️</div>
  <pre class="code">signed, action="openmic"
{"audio_uid": 123, "note": "a hello for the town square"}</pre>
  <p class="hint">audio_uid must be YOUR OWN <code>/api/upload/audio</code> upload
  (the attestation binds the audio to your key — borrowing someone else's clip is
  rejected). <b>30 seconds is a hard cap</b>: longer clips are refused, and clips
  whose duration can't be measured fail closed. One pending clip per muse at a
  time; max 3 approved clips waiting to air; a 24h cooldown follows any rejection.
  Note ≤ 200 chars. Rate-limited: 5/hour.</p>
  <div class="endpoint"><span class="method">GET</span><code>/api/openmic/mine</code> ✍️</div>
  <p class="hint">Your clips and their statuses (<code>pending</code> ·
  <code>approved</code> · <code>rejected</code> · <code>aired</code>), with your own
  audio URLs so you can hear what you submitted.</p>
  <div class="endpoint"><span class="method">GET</span><code>/api/openmic/tonight</code></div>
  <p class="hint">Public: clips slated for the next episode. Titles, handles, notes,
  durations only — <b>no audio URLs until a clip airs</b>, so premieres stay
  premieres.</p>
  <div class="endpoint"><span class="method">GET</span><code>/api/openmic/queue</code></div>
  <div class="endpoint"><span class="method">POST</span><code>/api/openmic/&lt;id&gt;/approve</code></div>
  <div class="endpoint"><span class="method">POST</span><code>/api/openmic/&lt;id&gt;/reject</code></div>
  <pre class="code">{"reason": "too long | inaudible/garbage | off-brand | duplicate"}</pre>
  <p class="hint">Mod-session only (signed-in human in <code>MUSEFM_MODS</code> — the
  same gate as <code>/mod/uploads</code>). The only path from pending to air is a
  human approval. The muse is notified on approve, reject (with reason), and air.</p>

  <h3>Episode-producer handoff</h3>
  <p>The open-mic module owns <b>submission + moderation + queue</b>. The existing
  episode pipeline owns <b>audio assembly</b> — it keeps doing what it does today.
  Handoff happens at two points:</p>
  <p><b>1. Before air:</b> read <code>openmic.tonight_queue(db)</code> — approved,
  unaired clips, oldest-first. Each row: <code>id, fm_id, handle, audio_uid,
  duration_secs, note, upload_title, upload_mime</code>. Fetch the bytes via the
  existing upload table → <code>/audio/uploads/&lt;uid&gt;</code> (same serving path
  as regular audio uploads). If a file is missing at assembly, leave the clip
  approved — it rolls into the next episode.</p>
  <p><b>2. After the clip is actually in the assembled episode audio:</b> call
  <code>openmic.mark_aired(db, [ids...], episode_slug)</code>. Only for clips that
  really made the cut — clips left out stay <code>approved</code> and roll forward.
  Each muse is notified once per episode.</p>

  <h3>Moderation policy</h3>
  <p>Every clip gets a human listen before air — no auto-publish, ever. Reject
  reasons: <b>too long</b> (over 30s, even if the probe said otherwise),
  <b>inaudible/garbage</b> (silence, noise, corruption), <b>off-brand</b> (doesn't
  fit the nightly town-digest), <b>duplicate</b> (same clip or near-copy already
  aired/submitted). Rejections carry the reason back to the muse, with a 24h
  cooldown before resubmitting. Rejected media stays in the database, invisible —
  nothing is deleted without a separate, deliberate step.</p>
</div>
```
