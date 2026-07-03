/* ═══════════════════════════════════════════════════════════════════════════
 * discovery.js — automatic backend discovery for the Pi Radio web interface
 * ═══════════════════════════════════════════════════════════════════════════
 *
 * PURPOSE
 *   When the web page loads, figure out — with zero manual configuration —
 *   where the backend and the Icecast stream live, using the page's own
 *   origin plus the backend's /api/discovery endpoint as the single source
 *   of truth. Works identically whether the page was reached via:
 *     raspberrypi.local · localhost · a LAN IP · a DHCP IP ·
 *     a Tailscale MagicDNS name · a Tailscale IP
 *
 * DESIGN RULES (deliberate — please keep them when editing)
 *   1. ISOLATED. This file defines exactly one global, window.PiDiscovery,
 *      and touches no DOM elements itself. The host page decides what to do
 *      with the result via callbacks. Nothing here can break the main UI.
 *   2. NEVER THROWS. Every network call and callback is wrapped; failure is
 *      reported through onFallback / onFailed, never as an exception.
 *   3. NO HARDCODED HOSTS. All URLs are built from window.location and the
 *      /api/discovery response.
 *   4. HARMLESS. Read-only GETs with retries; cannot interrupt playback,
 *      scheduling, or streaming.
 *
 * USAGE (see the init block in pi_radio_web.html)
 *   PiDiscovery.init({
 *     retries: 4,                 // attempts before falling back (default 4)
 *     retryDelayMs: 1200,         // base delay between attempts (default 1200)
 *     getManualBase: function(){  // OPTIONAL: return "http://host:port" from
 *       return '...';             // the user's manual settings, or '' if none
 *     },
 *     onConnected: function(info, mode) { ... },  // mode: 'auto' | 'manual'
 *     onFallback:  function(reason)     { ... },  // auto failed, trying manual
 *     onFailed:    function(reason)     { ... }   // nothing reachable
 *   });
 *
 * AFTER A SUCCESSFUL init, the host page may call:
 *   PiDiscovery.getInfo()         -> the raw /api/discovery object (or null)
 *   PiDiscovery.getBackendBase()  -> "http://host:8765"   (no trailing slash)
 *   PiDiscovery.getStreamUrl()    -> "http://host:8000/stream"
 *   PiDiscovery.getMode()         -> 'auto' | 'manual' | null
 *   PiDiscovery.describe()        -> human text for a status indicator, e.g.
 *                                    "Connected via Tailscale (pi.ts.net)"
 *
 * FUTURE EXPANSION
 *   Add new services (weather, recording, extra mounts, ...) to the backend's
 *   discovery_info() payload; they will appear in getInfo().features and
 *   getInfo() itself with NO changes required in this file or the HTML.
 * ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var state = {
    info: null,        // parsed /api/discovery payload once connected
    mode: null,        // 'auto' | 'manual'
    base: '',          // backend base URL in use, e.g. "http://pi.local:8765"
    lastError: ''      // last failure reason (for diagnostics)
  };

  /* ── helpers ─────────────────────────────────────────────────────────── */

  /** Base URL of the page we were served from, or '' when opened as file://
   *  (in which case automatic discovery is impossible by definition). */
  function originBase() {
    var loc = window.location;
    if (!loc || !loc.hostname || (loc.protocol !== 'http:' && loc.protocol !== 'https:')) {
      return '';
    }
    return loc.protocol + '//' + loc.host; // loc.host includes :port if any
  }

  /** GET url and parse JSON, resolving null on ANY failure (never rejects). */
  function fetchJson(url, timeoutMs) {
    return new Promise(function (resolve) {
      var done = false;
      var timer = setTimeout(function () {
        if (!done) { done = true; resolve(null); }
      }, timeoutMs || 4000);
      try {
        fetch(url, { cache: 'no-store' })
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (data) {
            if (!done) { done = true; clearTimeout(timer); resolve(data); }
          })
          .catch(function () {
            if (!done) { done = true; clearTimeout(timer); resolve(null); }
          });
      } catch (e) {
        if (!done) { done = true; clearTimeout(timer); resolve(null); }
      }
    });
  }

  /** Ask base + /api/discovery, with `retries` attempts and gentle backoff. */
  function tryDiscovery(base, retries, delayMs) {
    return new Promise(function (resolve) {
      var attempt = 0;
      function go() {
        attempt += 1;
        fetchJson(base + '/api/discovery').then(function (data) {
          if (data && data.ok && data.discovery) {
            resolve(data.discovery);
          } else if (attempt < retries) {
            // Linear backoff: 1x, 2x, 3x... keeps first retry quick while
            // giving a just-booting backend time to come up.
            setTimeout(go, delayMs * attempt);
          } else {
            resolve(null);
          }
        });
      }
      go();
    });
  }

  /** Never let a user callback take the module down. */
  function safeCall(fn) {
    if (typeof fn !== 'function') return;
    var args = Array.prototype.slice.call(arguments, 1);
    try { fn.apply(null, args); } catch (e) {
      if (window.console && console.warn) console.warn('PiDiscovery callback error:', e);
    }
  }

  /* ── public API ──────────────────────────────────────────────────────── */

  window.PiDiscovery = {

    init: function (opts) {
      opts = opts || {};
      var retries = Math.max(1, opts.retries || 4);
      var delayMs = Math.max(200, opts.retryDelayMs || 1200);
      var auto = originBase();

      var autoAttempt = auto
        ? tryDiscovery(auto, retries, delayMs)
        : Promise.resolve(null);   // file:// — skip straight to fallback

      autoAttempt.then(function (info) {
        if (info) {
          state.info = info; state.mode = 'auto'; state.base = auto;
          safeCall(opts.onConnected, info, 'auto');
          return;
        }
        // ── automatic discovery failed: silent fallback to manual config ──
        state.lastError = auto
          ? 'No response from ' + auto + '/api/discovery after ' + retries + ' attempt(s)'
          : 'Page opened as a local file — automatic discovery unavailable';
        safeCall(opts.onFallback, state.lastError);

        var manualBase = '';
        try { manualBase = (opts.getManualBase ? opts.getManualBase() : '') || ''; } catch (e) {}
        manualBase = String(manualBase).replace(/\/+$/, '');
        if (!manualBase) {
          safeCall(opts.onFailed, state.lastError + '; no manual host configured');
          return;
        }
        // One quick confirmation round against the manual host (2 tries).
        tryDiscovery(manualBase, 2, delayMs).then(function (manualInfo) {
          if (manualInfo) {
            state.info = manualInfo; state.mode = 'manual'; state.base = manualBase;
            safeCall(opts.onConnected, manualInfo, 'manual');
          } else {
            // Manual host didn't answer discovery either. Report failure but
            // leave the existing manual configuration alone — the classic UI
            // keeps working exactly as before this module existed.
            safeCall(opts.onFailed, 'Manual host ' + manualBase + ' did not answer discovery');
          }
        });
      });
    },

    getInfo: function () { return state.info; },
    getMode: function () { return state.mode; },
    getLastError: function () { return state.lastError; },

    getBackendBase: function () {
      if (!state.info) return state.base || '';
      var host = state.info.host || '';
      var port = state.info.backend_port || 8765;
      return host ? 'http://' + host + ':' + port : (state.base || '');
    },

    getStreamUrl: function () {
      if (!state.info) return '';
      if (state.info.stream_url) return state.info.stream_url;
      var host = state.info.host || '';
      return host
        ? 'http://' + host + ':' + (state.info.icecast_port || 8000) + (state.info.stream_mount || '/stream')
        : '';
    },

    /** Human-readable line for the status indicator. */
    describe: function () {
      if (!state.info) return 'Using manual host configuration';
      var host = state.info.host || 'backend';
      if (state.mode === 'manual') return 'Using manual host configuration (' + host + ')';
      switch (state.info.via) {
        case 'tailscale': return 'Connected via Tailscale (' + host + ')';
        case 'local':     return 'Connected to: ' + host + ' (this device)';
        case 'mdns':      return 'Connected to: ' + host;
        default:          return 'Connected to: ' + host;
      }
    }
  };
})();
