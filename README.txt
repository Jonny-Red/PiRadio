Pi Radio Station Player — v41-patched-12

Fix found during audit of v11
------------------------------
After a chime, the show was resuming at full volume with no fade-in.
Commercial resumes faded in correctly (v11) but chime resumes did not.
Both now use the same resume_saved() call with fade_in=True,
fade_in_seconds from settings, and target_volume from settings.

Complete fade behaviour summary
--------------------------------
  Commercial break:
    show track ends → show fades OUT → commercial fades IN
    → last ad ends → show fades IN at saved position ✓

  Hourly chime (pause/resume mode):
    chime plays → chime ends → show fades IN at saved position ✓

  If fades disabled in settings: all transitions are instant cuts.

All checks passed:
  ✓ Both resume_saved() callers pass target_volume=settings.volume
  ✓ _reset_commercial_state clears resume_after_commercial
  ✓ Resume captured only once per break
  ✓ _extend only called in commercial-end branch
  ✓ /stop stamps last_finished_signature
  ✓ Chime path does not touch commercial resume state
  ✓ no_resume_idle handled in monitor
  ✓ fade-in runs on daemon thread (non-blocking)
