Pi Radio Station Player — v41-patched-32

Fix in patched-32 — schedule displays follow the PI'S clock
------------------------------------------------------------
The dial needle, "On now" highlight, Coming Up list (new view) and the
timeline now-marker (classic view) all used the VIEWING DEVICE's clock —
so opening the page on a computer an hour ahead showed the wrong program
even though the Pi was playing the right one. The backend now publishes
its own local time in the status feed and both pages drive all schedule
displays from it, ticking it forward between polls. When the viewing
device's clock disagrees with the radio's, the new view's clock label
says so ("1:10 PM at the radio"). Bonus fix: the classic view's "Test
chime" sent the DEVICE's hour, striking the wrong number of dings from
another timezone — it now lets the Pi use its own hour.

Version badge: v41.32

Pi Radio Station Player — v41-patched-31

Fix in patched-31
------------------
DESKTOP APP NO LONGER SAYS "Refreshing status…" FOREVER. The 5-second
background poll wrote "Refreshing status…" into the busy line on every
cycle, and the reset only cleared labels starting with "Working" — so
the line was set every 5 seconds and cleared never. Routine polls are
now silent (the line reads "Ready"), the reset clears any transient
label, and real actions (Play, Save, scans) still show their progress
messages as before.

Version badge: v41.31

Pi Radio Station Player — v41-patched-30

Fixes in patched-30 — status refresh smoothed out
--------------------------------------------------
1. The Now Playing progress bar glides second by second instead of
   jumping every 5 seconds (it advances locally between status polls).
2. The Coming Up list no longer rebuilds every second (it only redraws
   when the on-now block actually changes) — no more flicker.
3. Addresses, settings summaries, and the log only redraw when their
   content changes, so nothing shifts under your finger mid-tap.
4. One missed status poll (a wifi blink) no longer flashes "Cannot
   reach the radio" — the page stays calm unless two polls in a row
   fail, and recovers on the next good one.

Version badge: v41.30

Pi Radio Station Player — v41-patched-29

Fixes in patched-29 — full settings parity audit
-------------------------------------------------
Audited every new-view settings card against the classic view. Found and
fixed three mismatches:
1. FADE CLOBBER FIXED (important): the new view had a single "Fade
   length" and saved it into BOTH fade-out and fade-in, silently
   overwriting distinct values set in classic (e.g. 3 s out / 2 s in).
   The Volume & sound card now has the same two separate fields.
2. CHIME SUMMARY TOLD A LIE: it always said "resumes the show after",
   even when classic's "Interrupt instead of pause/resume" was ticked.
   The switch is now on the new view's chime card and the summary states
   whichever behavior is actually configured.
3. AUTOPLAY ON START was missing from the new view — now on the Program
   schedule card ("Start playing automatically when the radio boots").
Still classic-only by design (and labeled as such): program block
editing, fill source/folders, quiet hours, guard rails, per-hour chime
files, network path.

Version badge: v41.29

Pi Radio Station Player — v41-patched-28

Fix in patched-28
------------------
NEW VIEW AND CLASSIC VIEW NOW AGREE ON COMMERCIALS. The new view's
Commercials card only knew breaks-per-hour and a fixed spot count, so a
station configured in classic with end-of-track breaks and a random spot
range showed up as "0 breaks/hour · 0 spots each" — the two pages
contradicted each other. The card now carries the full break model:
- the three "when to play a break" switches (after every episode / when
  a show block ends / between shows), matching classic exactly
- timed breaks per hour and "scheduled shows only"
- spots per break as fixed OR random min-max, with the same
  "random range wins" rule as the backend
- an honest summary line (e.g. "after every episode · 1-3 spots
  (random) · 386 on file"), including a warning when no trigger is set
- Test a break now rolls the random spot count when one is configured,
  exactly like a real break (previously tests always used the fixed
  number).
Quiet hours and guard rails remain classic-view editors for now.

Version badge: v41.28

Pi Radio Station Player — v41-patched-27

New in patched-27 — TWO INTERFACES, ONE BUTTON APART
-----------------------------------------------------
1. THE NEW INTERFACE is now the main web page: amber "dial face" Now
   Playing card (show, episode X of Y, live progress bar), big Listen
   and Stop buttons, the broadcast-day dial strip with a live needle,
   a program guide, plain-language settings cards with a Save bar, the
   address card, and the station log — with bottom navigation built for
   phones. Stop cuts both the radio and the in-browser stream player.
2. THE CLASSIC INTERFACE is fully preserved at /classic — every editor
   (program blocks, quiet hours, per-hour chime files, fill folders,
   detailed break rules) is unchanged. Each page has a one-tap switch
   to the other ("Classic view" / "New view").
3. Backend: three read-only status additions power the new page (track
   position/length, episode index within the show, pending-break flag);
   the classic page ignores them. No playback logic was changed.

Version badge: v41.27

Pi Radio Station Player — v41-patched-26

Fix in patched-26
------------------
MAIN WINDOW HEIGHT TRIMMED. The desktop app's main window was widened in
patched-22 for the web-address line but kept too tall, leaving a large
empty band under the buttons. Now 820x420 — wide enough for the address
on one line, only as tall as the content.

Version badge: v41.26

Pi Radio Station Player — v41-patched-25

Fix in patched-25
------------------
TEST CHIME CAN NO LONGER FREEZE THE RADIO. The chime was marked "in
progress" BEFORE it started playing; if it then failed to start (missing
or unreadable chime file, VLC error), the in-progress flag stuck forever
— and that flag defers commercial breaks and queues schedule starts, so
after one bad Test Chime the radio quietly stopped doing anything until
a restart. Two layers of protection now:
1. A failed chime start cleans up after itself and resumes the
   interrupted track at the exact position (the error still shows in the
   web UI so you know the chime file needs attention).
2. A chime watchdog in the monitor: if a chime is marked active for 30
   seconds with no audio playing, it force-finishes — clearing the flag,
   resuming any interrupted track, and releasing any queued schedule
   item or commercial break.

Version badge: v41.25

Pi Radio Station Player — v41-patched-24

Fix in patched-24
------------------
PLAY RANDOM BUTTON NO LONGER LIGHTS UP FOR EVERYTHING. It was used as a
general "audio is playing" lamp, so scheduled shows, chimes, and
commercials all highlighted it as if random playback were active. It now
highlights only when random playback is actually what's running.

Version badge: v41.24

Pi Radio Station Player — v41-patched-23

Changes in patched-23
----------------------
1. STOP ALSO SILENCES THE STREAM PLAYER IN YOUR BROWSER. Pressing Stop
   halted the Pi instantly, but a phone listening to the stream kept
   playing its 10-30 seconds of already-buffered audio — which made Stop
   feel like it "didn't really work". The web Stop button now also cuts
   the in-browser Listen player immediately, and the Listen card explains
   the buffer for anything still playing through a separate media app.
2. "OPEN IN APP" BUTTON REMOVED (didn't behave reliably across phones).
   The in-browser Listen button remains, and the stream address on the
   Main tab can still be added to any radio app by hand.

Version badge: v41.23

Pi Radio Station Player — v41-patched-22

Fixes in patched-22
--------------------
1. "PLAY SCHEDULE" NO LONGER STARTS RANDOM FILL. The button played
   whatever the schedule said for the current minute — and outside your
   program hours that's the Random Fill window, so pressing Play Schedule
   started random music. It now always starts your program blocks (the
   current one if you're inside its hours, otherwise block 1). Random
   fill only enters via the scheduler on its own.
2. NO MORE REPEATED PRESSES AFTER A REBOOT. USB drives often finish
   mounting AFTER the radio auto-starts on boot, so the media caches
   loaded empty and every Play press failed until the drive appeared. A
   drive watcher now checks for the configured folders for the first 5
   minutes, reloads the caches the moment they appear, freshens them in
   the background, and starts the schedule if playback should be running.
3. DESKTOP APP IS BIGGER: default window 820x560 (was 560x360) so the
   address line, status, and buttons are all visible without resizing.

Version badge: v41.22

Pi Radio Station Player — v41-patched-21

Fixes in patched-21
--------------------
1. STOP NOW STICKS ON THE FIRST PRESS. If a track transition was in
   flight at the moment Stop was pressed (track ending, break finishing,
   schedule handoff), the transition restarted playback right after the
   stop — so Stop had to be pressed repeatedly until it landed outside a
   transition. /stop now opens a 2-second quiet window during which every
   automatic start (handoff, continue, catch-up, clock start) is
   suppressed. Pressing any Play/Test button closes the window instantly,
   so nothing the USER asks for is ever delayed.
2. ONLY ONE BACKEND CAN RUN AT A TIME. If the systemd auto-start service
   AND a manual ./start_all.sh both launch the backend, the two instances
   play the schedule over each other and Stop only reaches one — another
   way Stop "didn't work". A second instance now detects the first and
   refuses to start, with instructions on stopping the running one.

Version badge: v41.21

Pi Radio Station Player — v41-patched-20

New in patched-20
------------------
1. ROOT CAUSE FIX — THE PAGE CAN NO LONGER GO STALE. The backend now
   serves the web page with no-cache headers. Phones were caching old
   copies indefinitely, which made new features (Listen button, address
   list, status auto-refresh) seem "removed" or broken after updates.
   From this build on, every page load is the current page. The version
   badge (top of page) now reads v41.20 so you can CONFIRM you're on the
   new build at a glance.
2. "LISTEN ON THIS DEVICE" CARD on the Main tab: big "Listen in browser"
   and "Open in media app" buttons front and center (the top-bar buttons
   remain too).
3. "RADIO WEBSITE ADDRESS" CARD on the Main tab with tappable links, the
   address line on the desktop app main screen, the footer line, and the
   startup log listing — the address is everywhere now.
4. Status line showed "Backend on ?" — now shows the real port.

Pi Radio Station Player — v41-patched-19

New in patched-19
------------------
1. WEBSITE ADDRESS IS NOW LISTED. At startup the backend works out every
   real address it answers on — hostname.local, each LAN IP, and the
   Tailscale IP if present — and prints them in the log ("Web interface
   addresses: ..."). The web page also shows them in a "This radio: ..."
   line under the status, so anyone on the page can find and share the
   address.
   The address is shown PROMINENTLY: on the desktop app main screen in
   blue right under Now Playing, and on the web page Main tab in its own
   "Radio Website Address" card with tappable links.
2. PACKAGING FIX: pi_stream.py now ships with its executable bit set,
   and the installer chmods all runnable files after extraction — so
   ./pi_stream.py works straight from an unzip.
3. STATUS NO LONGER FREEZES ON PHONES. Mobile browsers can freeze an
   in-flight status request when the phone locks; the auto-refresh chain
   then died silently and the page showed stale status until "Refresh
   Status" was pressed. A watchdog now restarts polling if it stalls, and
   the page refreshes instantly when the tab becomes visible again.

Pi Radio Station Player — v41-patched-18

New in patched-18
------------------
"OPEN IN APP" BUTTON next to Listen. Hands the live stream to a media
app on the device via Icecast's stream.m3u playlist link — on phones the
OS offers installed players (VLC app, radio apps, ...), which keep
playing with the screen locked, unlike the in-browser Listen player.

Pi Radio Station Player — v41-patched-17

New in patched-17
------------------
LISTEN BUTTON IN THE WEB INTERFACE. A "Listen" button in the connection
bar plays the live Icecast stream directly in the browser — phones,
tablets, and laptops need no app at all. The stream address comes from
the discovery endpoint (so it works over LAN, .local, and Tailscale
alike); if streaming isn't running on the Pi you get a clear message
instead of silence. Expect the usual Icecast delay of roughly 10-30
seconds behind the Pi's speakers — that's the stream buffer, not a bug.

Pi Radio Station Player — v41-patched-16

Fix in patched-16
------------------
NO MORE CHIME AT STARTUP. The scheduler checked the clock immediately on
its first tick with an empty "already chimed this hour" key, so a backend
starting anywhere inside minute :00 (e.g. auto-start on boot at 8:00)
played the hourly chime the moment it came up — and one starting on a
commercial-target minute instantly queued a break. Both keys are now
seeded with the startup moment, so the first chime/break happens at the
NEXT scheduled time. The "Test Hour Chime" button is unaffected.

Pi Radio Station Player — v41-patched-15

New in patched-15
------------------
AUTOMATIC BACKEND DISCOVERY (new, isolated module)
- New GET /api/discovery endpoint: single source of truth for client
  config — reports host (as the client reached it), backend port, Icecast
  port + stream mount (read from the darkice config when present),
  version, and a features list for future expansion.
- New discovery.js module (served at /discovery.js, kept separate from
  the main UI script). On page load it auto-discovers the backend with
  retries, whether you connect via raspberrypi.local, localhost, LAN IP,
  DHCP IP, Tailscale MagicDNS, or Tailscale IP. If discovery fails it
  silently falls back to the existing manual Host/IP inputs.
- Small "Connected to: ..." / "Connected via Tailscale" / "Using manual
  host configuration" indicator in the connection bar (hover it to see
  the live stream URL). No layout, control, or behavior changes beyond
  this; playback/streaming/scheduling untouched.

Fixes in patched-15 (deep audit)
---------------------------------
1. SCAN QUEUE UNJAMMED: one failed scan (e.g. USB drive not mounted)
   used to block that scan type forever — every later Rescan silently
   did nothing until the backend restarted. Failed jobs no longer block
   re-queueing.
2. DESKTOP APP FREEZE FIX: the Now Playing window ran its 2-second
   status fetch ON the Tk UI thread; a slow/down backend froze the whole
   app in 5s stutters. The fetch now truly runs in the background.
3. INSTALLER REFRESHED: install.sh's embedded program payload was stale
   (pre-patch v41) — re-running the installer would have silently rolled
   back every fix. The payload now contains the current files, including
   discovery.js and the pi_stream streaming files.

Pi Radio Station Player — v41-patched-14

Fix in patched-14
------------------
TEST COMMERCIAL BREAK NOW RESUMES THE PROGRAM. The test endpoint used to
stop playback and clear the active segment BEFORE starting the break, so
after a mid-show test the program never came back. It now behaves exactly
like a real break: captures the current track and position, fades out,
plays the spots, extends the show's end time, and resumes where it left
off. (If pressed while a break is already playing, it restarts the break
cleanly instead of trying to resume a half-finished spot.)

Pi Radio Station Player — v41-patched-13

Fixes in patched-13 (audit)
----------------------------
1. DEAD AIR FIX: if a queued per-hour commercial break was blocked by a
   rule (quiet hour, min gap, scheduled-only, max breaks), the monitor
   ignored the result and nothing started the next track. It now falls
   through to the next track / schedule handoff like every other path.
2. SILENCE WATCHDOG FIX: the 30s watchdog was unreachable (it only ran
   on the first silent poll). It now checks every silent poll, skips
   normal commercial/chime transitions, and stays quiet when silence is
   intentional (schedule "stop" mode, chime fired from idle).
3. CHIME VOLUME FIX: chimes now force the configured volume before
   playing — previously a chime landing during an end-of-track fade
   could play at near-zero volume and be inaudible.
4. VOLUME STOMP FIX: saving settings no longer pushes the volume to VLC
   unless the volume value actually changed (was snapping mid-fade).
5. ATOMIC SAVES: settings and cache files are written via temp file +
   os.replace, so a power cut mid-write can't corrupt them and silently
   reset settings to defaults.
6. RESUME RACE FIX: resume_saved() now carries a play-generation token;
   if another play/stop happens during its 3s wait, it aborts instead
   of seeking/fading the wrong track.
7. run_radio.py re-synced with radio_backend.py (it was missing the
   "Autoplay on start" feature, and it's the recommended launcher).

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
