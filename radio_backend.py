#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import queue
import random
import socket
import struct
import tempfile
import threading
import time
import wave
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:
    import vlc
except Exception:
    vlc = None

from shared_radio import (
    APP_VERSION,
    DEFAULT_HOST,
    DEFAULT_PORT,
    PORT_FILE,
    BACKEND_PID_FILE,
    AppSettings,
    SUPPORTED_EXTENSIONS,
    LIBRARY_CACHE_FILE,
    SHOW_CACHE_FILE,
    FILL_CACHE_FILE,
    COMMERCIAL_CACHE_FILE,
    LOG_FILE,
)


class SimplePlayer:
    def __init__(self, logger):
        self.logger = logger
        self.lock = threading.RLock()
        self.instance = vlc.Instance() if vlc else None
        self.player = self.instance.media_player_new() if self.instance else None
        self.current_path = ''
        self.current_kind = ''
        self.last_play_started_at = 0.0  # wall time of most recent play() call

    def set_volume(self, volume: int):
        with self.lock:
            if self.player:
                try:
                    self.player.audio_set_volume(int(volume))
                except Exception as exc:
                    self.logger(f'set_volume({volume}) failed: {exc}')

    def get_volume(self) -> int:
        with self.lock:
            if self.player:
                try:
                    return max(0, int(self.player.audio_get_volume()))
                except Exception as exc:
                    self.logger(f'get_volume() failed: {exc}')
                    return 0
            return 0

    def fade_to(self, target_volume: int, duration: float, steps: int = 20):
        target_volume = max(0, min(100, int(target_volume)))
        try:
            duration = float(duration)
        except Exception:
            duration = 0.0
        if duration <= 0 or steps <= 0:
            self.set_volume(target_volume)
            return
        start_volume = self.get_volume()
        if start_volume == target_volume:
            return
        sleep_for = max(0.01, duration / steps)
        delta = (target_volume - start_volume) / float(steps)
        for idx in range(steps):
            new_volume = int(round(start_volume + (delta * (idx + 1))))
            self.set_volume(new_volume)
            time.sleep(sleep_for)
        self.set_volume(target_volume)

    def play(self, path: str, kind: str = 'main'):
        is_url = str(path).startswith(('http://', 'https://', 'rtsp://', 'rtmp://', 'mms://'))
        if not is_url and not Path(path).exists():
            raise FileNotFoundError(path)
        with self.lock:
            if not self.player:
                raise RuntimeError('python-vlc / VLC is not available on this Pi.')
            media = self.instance.media_new(path)
            self.player.set_media(media)
            self.player.play()
            self.current_path = path
            self.current_kind = kind
            self.last_play_started_at = time.time()
        self.logger(f'Playing {kind}: {path}')

    def stop(self):
        with self.lock:
            if self.player:
                self.player.stop()
            self.current_path = ''
            self.current_kind = ''
            self.last_play_started_at = 0.0
        self.logger('Playback stopped.')

    def pause(self):
        with self.lock:
            if self.player:
                try:
                    self.player.pause()
                except Exception as exc:
                    self.logger(f'pause() failed: {exc}')

    def get_vlc_state(self):
        """Return the raw VLC state, or None if unavailable."""
        with self.lock:
            if self.player and vlc:
                try:
                    return self.player.get_state()
                except Exception:
                    pass
        return None

    def is_playing(self) -> bool:
        """Return True if VLC is actively playing OR is in a transient state
        (Opening / Buffering) that means audio is expected imminently.
        Also returns True for a short grace period (2s) after play() is called,
        covering the brief Stopped/NothingSpecial window before VLC transitions
        to Opening — which on a Pi reading from USB can exceed the debounce
        threshold and cause the monitor to fire a spurious end-of-track."""
        with self.lock:
            if not self.player:
                return False
            # Grace period: if play() was called within the last 2 seconds,
            # treat the player as active regardless of VLC's reported state.
            # This prevents rapid-fire track advances when VLC is slow to start.
            if time.time() - self.last_play_started_at < 2.0:
                return True
            try:
                if vlc:
                    state = self.player.get_state()
                    active_states = {
                        vlc.State.Opening,
                        vlc.State.Buffering,
                        vlc.State.Playing,
                    }
                    return state in active_states
                return bool(self.player.is_playing())
            except Exception as exc:
                self.logger(f'is_playing() failed: {exc}')
                return False

    def is_error(self) -> bool:
        """Return True if VLC is in an error state (bad file, network timeout, etc)."""
        with self.lock:
            if self.player and vlc:
                try:
                    return self.player.get_state() == vlc.State.Error
                except Exception:
                    pass
        return False

    def current_time_ms(self) -> int:
        with self.lock:
            if self.player:
                try:
                    return max(0, int(self.player.get_time()))
                except Exception as exc:
                    self.logger(f'current_time_ms() failed: {exc}')
                    return 0
            return 0

    def total_length_ms(self) -> int:
        with self.lock:
            if self.player:
                try:
                    return max(0, int(self.player.get_length()))
                except Exception as exc:
                    self.logger(f'total_length_ms() failed: {exc}')
                    return 0
            return 0

    def resume_saved(self, path: str, kind: str, time_ms: int, fade_in: bool = False, fade_in_seconds: float = 2.0, target_volume: int = 80):
        """Play path, seek to time_ms, and optionally fade in from silence.

        Everything after the initial play() call runs on a daemon thread so
        this method returns immediately and never blocks the playback monitor.
        """
        if fade_in:
            self.set_volume(0)
        self.play(path, kind)
        time_ms = max(0, int(time_ms))
        self.logger(f'Resuming {kind}: {path} @ {time_ms} ms')

        def _do_seek_and_fade():
            sought = threading.Event()

            def _on_playing(event):
                if not sought.is_set():
                    sought.set()

            registered = False
            try:
                if self.player:
                    em = self.player.event_manager()
                    import vlc as _vlc
                    em.event_attach(_vlc.EventType.MediaPlayerPlaying, _on_playing)
                    registered = True
            except Exception:
                pass

            # Wait for VLC to confirm playback has started (up to 3s),
            # then seek to the saved position.
            sought.wait(timeout=3.0)

            if registered:
                try:
                    em.event_detach(_vlc.EventType.MediaPlayerPlaying, _on_playing)
                except Exception:
                    pass

            # Only seek if we actually have a non-zero position to restore.
            if time_ms > 0:
                # Try a few times — VLC sometimes needs a moment after the
                # Playing event before set_time() sticks.
                for attempt in range(3):
                    try:
                        if self.player:
                            self.player.set_time(time_ms)
                            break
                    except Exception:
                        pass
                    time.sleep(0.1)

            if fade_in and fade_in_seconds > 0:
                self.fade_to(max(0, int(target_volume)), float(fade_in_seconds))
            elif fade_in:
                self.set_volume(max(0, int(target_volume)))

        threading.Thread(target=_do_seek_and_fade, daemon=True).start()


def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default


def save_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2))


class BackendState:
    def __init__(self):
        self.settings = AppSettings.load()
        self.player = SimplePlayer(self.log)
        self.player.set_volume(self.settings.volume)
        self.lock = threading.RLock()
        self.shutdown_event = threading.Event()
        self.logs: list[str] = []
        self.library_files: list[str] = []
        self.show_folders: list[str] = []
        self.fill_files: list[str] = []
        self.commercial_files: list[str] = []
        self.library_cache_meta = {}
        self.show_cache_meta = {}
        self.fill_cache_meta = {}
        self.commercial_cache_meta = {}
        self.pending_scan = None
        self.pending_fill_scan = None
        self.pending_show_scan = None
        self.pending_commercial_scan = None
        self.last_schedule_key = None
        self.last_chime_key = None
        self.resume_after_chime = None
        self.chime_active = False
        self.current_chime_kind = ''
        self.current_chime_started_at = 0.0
        self.current_segment_label = ''
        self.active_segment = None
        self.last_finished_signature = None
        self.pending_schedule_item = None
        self.pending_commercial_break = 0
        self.commercial_break_remaining = 0
        self.commercial_sequence_index = 0
        self.last_commercial_break_key = None
        self.resume_after_commercial = None
        self.commercial_break_started_at = 0.0
        self._between_show_next_item = None
        self._pending_commercial_trigger = ''   # trigger tag set when a per_hour break is queued
        self.commercial_break_timeout_seconds = 180.0
        self.commercial_session_id = 0
        self.last_playing_at = time.time()   # timestamp of last confirmed playback
        # Commercial rules tracking
        self.last_break_ended_at = 0.0        # wall time when the last break finished
        self.show_started_at = 0.0            # wall time when the current show block started
        self.breaks_this_show = 0             # how many breaks have fired in the current show block
        self.silence_watchdog_seconds = 30.0  # how long to tolerate silence before self-healing
        self.started_at = time.time()
        self.last_worker_heartbeat = self.started_at
        self.last_scheduler_heartbeat = self.started_at
        self.last_playback_heartbeat = self.started_at
        self.last_status_heartbeat = self.started_at
        self.worker_q: queue.Queue[tuple[str, dict]] = queue.Queue()
        self.worker_thread = threading.Thread(target=self.worker_loop, daemon=True)
        self.worker_thread.start()
        self.scheduler_thread = threading.Thread(target=self.scheduler_loop, daemon=True)
        self.scheduler_thread.start()
        self.playback_monitor_thread = threading.Thread(target=self.playback_monitor_loop, daemon=True)
        self.playback_monitor_thread.start()
        self.ui_revisions = {
            'logs': 0,
            'shows': 0,
            'settings': 0,
            'fill': 0,
            'library': 0,
            'commercials': 0,
        }
        self.fade_triggered_for = ''
        self.temp_generated_media: set[str] = set()
        self.load_caches()
        if not vlc:
            self.log('WARNING: python-vlc / VLC is not available. Playback commands will fail until VLC is installed.')
        if self.settings.commercials_enabled and not str(self.settings.commercials_folder or '').strip():
            self.settings.commercials_enabled = False
            self.log('Commercials were enabled without a folder. Auto-disabled for this session only (folder not set).')
        self.log(f'Backend started. Version {APP_VERSION}')
        self.log(f'VLC available: {bool(vlc)}')
        # Give threads a moment to start, then catch up if we're mid-schedule.
        threading.Thread(target=lambda: (time.sleep(1.5), self._catchup_schedule('startup')), daemon=True).start()

    def bump(self, *names: str):
        with self.lock:
            for name in names:
                self.ui_revisions[name] = int(self.ui_revisions.get(name, 0)) + 1

    def log(self, msg: str):
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        line = f'[{stamp}] {msg}'
        print(line, flush=True)
        with self.lock:
            self.logs.append(line)
            self.logs = self.logs[-300:]
        self.bump('logs')
        try:
            with LOG_FILE.open('a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass

    def load_caches(self):
        lib = load_json(LIBRARY_CACHE_FILE, {})
        show = load_json(SHOW_CACHE_FILE, {})
        fill = load_json(FILL_CACHE_FILE, {})
        commercials = load_json(COMMERCIAL_CACHE_FILE, {})
        with self.lock:
            self.library_files = [p for p in lib.get('files', []) if Path(p).exists()]
            self.library_cache_meta = lib.get('meta', {})
            self.show_folders = [p for p in show.get('folders', []) if Path(p).exists()]
            self.show_cache_meta = show.get('meta', {})
            self.fill_files = [p for p in fill.get('files', []) if Path(p).exists()]
            self.fill_cache_meta = fill.get('meta', {})
            self.commercial_files = [p for p in commercials.get('files', []) if Path(p).exists()]
            self.commercial_cache_meta = commercials.get('meta', {})
        if self.library_files:
            self.log(f'Loaded cached main library: {len(self.library_files)} file(s).')
        if self.show_folders:
            self.log(f'Loaded cached show folders: {len(self.show_folders)} folder(s).')
        if self.fill_files:
            self.log(f'Loaded cached fill library: {len(self.fill_files)} file(s).')
        if self.commercial_files:
            self.log(f'Loaded cached commercials: {len(self.commercial_files)} file(s).')
        self.bump('library', 'shows', 'fill', 'commercials')

    def _clear_cache_file(self, path: Path):
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass

    def invalidate_library_cache(self):
        with self.lock:
            self.library_files = []
            self.library_cache_meta = {}
            if self.pending_scan and self.pending_scan.get('status') != 'running':
                self.pending_scan = None
        self._clear_cache_file(LIBRARY_CACHE_FILE)
        self.bump('library')

    def invalidate_show_cache(self):
        with self.lock:
            self.show_folders = []
            self.show_cache_meta = {}
            if self.pending_show_scan and self.pending_show_scan.get('status') != 'running':
                self.pending_show_scan = None
        self._clear_cache_file(SHOW_CACHE_FILE)
        self.bump('shows')

    def invalidate_fill_cache(self):
        with self.lock:
            self.fill_files = []
            self.fill_cache_meta = {}
            if self.pending_fill_scan and self.pending_fill_scan.get('status') != 'running':
                self.pending_fill_scan = None
        self._clear_cache_file(FILL_CACHE_FILE)
        self.bump('fill')

    def invalidate_commercial_cache(self):
        with self.lock:
            self.commercial_files = []
            self.commercial_cache_meta = {}
            self.commercial_sequence_index = 0
            if self.pending_commercial_scan and self.pending_commercial_scan.get('status') != 'running':
                self.pending_commercial_scan = None
        self._clear_cache_file(COMMERCIAL_CACHE_FILE)
        self.bump('commercials')

    def save_settings_and_refresh_caches(self, payload: dict):
        with self.lock:
            before = self.settings.to_dict()
        for key in [
            'media_folder', 'parent_library_folder', 'custom_network_path', 'volume',
            'fade_enabled', 'fade_out_seconds', 'fade_in_seconds',
            'auto_resume_random', 'scheduler_enabled', 'duration_start_hour',
            'duration_start_minute', 'program_blocks', 'schedule_fill_mode', 'fill_source_mode', 'fill_folders', 'fill_include_subfolders',
            'commercials_enabled', 'commercials_folder', 'commercials_mode', 'commercials_per_hour', 'commercials_per_break', 'commercials_prefix', 'commercials_between_shows',
            'commercials_end_of_show', 'commercials_end_of_track', 'commercials_min_gap_minutes', 'commercials_min_show_runtime_minutes', 'commercials_max_breaks_per_show', 'commercials_spots_min', 'commercials_spots_max', 'commercials_quiet_hours', 'commercials_scheduled_only',
            'hourly_chimes_enabled', 'chime_mode', 'interrupt_hourly', 'chimes_folder', 'hourly_audio_paths'
        ]:
            if key in payload:
                setattr(self.settings, key, payload[key])
        self.settings.normalize()
        after = self.settings.to_dict()
        rescans = []
        if before.get('media_folder') != after.get('media_folder'):
            self.invalidate_library_cache()
            if after.get('media_folder'):
                rescans.append(('library', self.enqueue_scan_library))
        if before.get('parent_library_folder') != after.get('parent_library_folder'):
            self.invalidate_show_cache()
            if after.get('parent_library_folder'):
                rescans.append(('shows', self.enqueue_scan_show_folders))
        if (before.get('fill_folders') != after.get('fill_folders') or
            before.get('fill_include_subfolders') != after.get('fill_include_subfolders') or
            before.get('fill_source_mode') != after.get('fill_source_mode')):
            self.invalidate_fill_cache()
            if after.get('fill_source_mode') == 'selected_folders' and after.get('fill_folders'):
                rescans.append(('fill', self.enqueue_scan_fill_library))
        if (before.get('commercials_folder') != after.get('commercials_folder') or
            before.get('commercials_enabled') != after.get('commercials_enabled')):
            self.invalidate_commercial_cache()
            if after.get('commercials_enabled') and after.get('commercials_folder'):
                rescans.append(('commercials', self.enqueue_scan_commercials))
        self.settings.save()
        self.player.set_volume(self.settings.volume)
        self.bump('settings', 'fill')
        for _name, fn in rescans:
            try:
                fn(force=False)
            except Exception as exc:
                self.log(f'Could not queue refresh scan: {exc}')
        # If the new settings put us inside a scheduled window and nothing is
        # playing, start the appropriate block immediately rather than waiting
        # for the next exact minute boundary.
        threading.Thread(target=lambda: (time.sleep(0.2), self._catchup_schedule('settings saved')), daemon=True).start()
        return {'rescans': [name for name, _ in rescans]}

    def folder_signature(self, folder: str) -> dict:
        p = Path(folder)
        if not p.exists():
            return {'folder': folder, 'exists': False}
        newest = p.stat().st_mtime
        count = 0
        for root, dirs, files in os.walk(folder):
            try:
                newest = max(newest, Path(root).stat().st_mtime)
            except Exception:
                pass
            count += len(files) + len(dirs)
        return {'folder': folder, 'exists': True, 'mtime': int(newest), 'count': count}

    def multi_folder_signature(self, folders: list[str], include_subfolders: bool = True) -> dict:
        clean = []
        for folder in folders or []:
            s = str(folder or '').strip()
            if s and s not in clean:
                clean.append(s)
        items = []
        for folder in clean:
            p = Path(folder)
            if not p.exists():
                items.append({'folder': folder, 'exists': False})
                continue
            newest = p.stat().st_mtime
            count = 0
            if include_subfolders:
                for root, dirs, files in os.walk(folder):
                    try:
                        newest = max(newest, Path(root).stat().st_mtime)
                    except Exception:
                        pass
                    count += len(files) + len(dirs)
            else:
                try:
                    for child in p.iterdir():
                        count += 1
                        try:
                            newest = max(newest, child.stat().st_mtime)
                        except Exception:
                            pass
                except Exception:
                    pass
            items.append({'folder': folder, 'exists': True, 'mtime': int(newest), 'count': count})
        return {'folders': items, 'include_subfolders': bool(include_subfolders)}

    def _scan_audio_paths(self, folders: list[str], include_subfolders: bool = True) -> list[str]:
        found = []
        seen = set()
        for folder in folders or []:
            p = Path(folder)
            if not p.exists() or not p.is_dir():
                continue
            if include_subfolders:
                for root, _, files in os.walk(folder):
                    for name in files:
                        full = str(Path(root) / name)
                        if Path(name).suffix.lower() in SUPPORTED_EXTENSIONS and full not in seen:
                            found.append(full)
                            seen.add(full)
            else:
                try:
                    for child in p.iterdir():
                        full = str(child)
                        if child.is_file() and child.suffix.lower() in SUPPORTED_EXTENSIONS and full not in seen:
                            found.append(full)
                            seen.add(full)
                except Exception:
                    pass
        return sorted(found)

    def ensure_library_loaded(self):
        with self.lock:
            have_files = bool(self.library_files)
            pending = self.pending_scan is not None
        if have_files or pending:
            return
        self.enqueue_scan_library(force=False)

    def ensure_show_folders_loaded(self):
        with self.lock:
            have_folders = bool(self.show_folders)
            pending = self.pending_show_scan is not None
        if have_folders or pending:
            return
        self.enqueue_scan_show_folders(force=False)

    def ensure_fill_loaded(self):
        with self.lock:
            source_mode = self.settings.fill_source_mode
            have_fill = bool(self.fill_files)
            pending = self.pending_fill_scan is not None
            fill_folders = list(self.settings.fill_folders or [])
        if source_mode != 'selected_folders' or not fill_folders:
            return
        if have_fill or pending:
            return
        self.enqueue_scan_fill_library(force=False)

    def ensure_commercials_loaded(self):
        with self.lock:
            enabled = bool(self.settings.commercials_enabled)
            folder = str(self.settings.commercials_folder or '').strip()
            have_files = bool(self.commercial_files)
            pending = self.pending_commercial_scan is not None
        if not enabled or not folder:
            return
        if have_files or pending:
            return
        self.enqueue_scan_commercials(force=False)

    def enqueue_scan_library(self, force: bool = False):
        with self.lock:
            if self.pending_scan:
                return self.pending_scan
            self.pending_scan = {'status': 'queued', 'started': None, 'finished': None, 'force': force, 'count': len(self.library_files)}
        self.worker_q.put(('scan_library', {'force': force}))
        return self.pending_scan

    def enqueue_scan_show_folders(self, force: bool = False):
        with self.lock:
            if self.pending_show_scan:
                return self.pending_show_scan
            self.pending_show_scan = {'status': 'queued', 'started': None, 'finished': None, 'force': force, 'count': len(self.show_folders)}
        self.worker_q.put(('scan_show_folders', {'force': force}))
        return self.pending_show_scan

    def enqueue_scan_fill_library(self, force: bool = False):
        with self.lock:
            if self.pending_fill_scan:
                return self.pending_fill_scan
            self.pending_fill_scan = {'status': 'queued', 'started': None, 'finished': None, 'force': force, 'count': len(self.fill_files)}
        self.worker_q.put(('scan_fill_library', {'force': force}))
        return self.pending_fill_scan

    def enqueue_scan_commercials(self, force: bool = False):
        with self.lock:
            if self.pending_commercial_scan:
                return self.pending_commercial_scan
            self.pending_commercial_scan = {'status': 'queued', 'started': None, 'finished': None, 'force': force, 'count': len(self.commercial_files)}
        self.worker_q.put(('scan_commercials', {'force': force}))
        return self.pending_commercial_scan

    def worker_loop(self):
        while not self.shutdown_event.is_set():
            self.last_worker_heartbeat = time.time()
            try:
                task, payload = self.worker_q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if task == 'scan_library':
                    self._scan_library_worker(bool(payload.get('force', False)))
                elif task == 'scan_show_folders':
                    self._scan_show_worker(bool(payload.get('force', False)))
                elif task == 'scan_fill_library':
                    self._scan_fill_worker(bool(payload.get('force', False)))
                elif task == 'scan_commercials':
                    self._scan_commercials_worker(bool(payload.get('force', False)))
            except Exception as exc:
                self.log(f'Worker task {task} failed: {exc}')
                with self.lock:
                    if task == 'scan_library' and self.pending_scan:
                        self.pending_scan.update({'status': 'failed', 'finished': int(time.time()), 'error': str(exc)})
                    elif task == 'scan_show_folders' and self.pending_show_scan:
                        self.pending_show_scan.update({'status': 'failed', 'finished': int(time.time()), 'error': str(exc)})
                    elif task == 'scan_fill_library' and self.pending_fill_scan:
                        self.pending_fill_scan.update({'status': 'failed', 'finished': int(time.time()), 'error': str(exc)})
                    elif task == 'scan_commercials' and self.pending_commercial_scan:
                        self.pending_commercial_scan.update({'status': 'failed', 'finished': int(time.time()), 'error': str(exc)})
            finally:
                self.worker_q.task_done()



    def _scan_library_worker(self, force: bool):
        folder = self.settings.media_folder
        if not folder or not os.path.isdir(folder):
            raise FileNotFoundError('Main library folder not found.')
        with self.lock:
            if self.pending_scan:
                self.pending_scan['status'] = 'running'
                self.pending_scan['started'] = int(time.time())
        signature = self.folder_signature(folder)
        with self.lock:
            cached_sig = dict(self.library_cache_meta)
            cached_files = list(self.library_files)
        if not force and cached_files and cached_sig == signature:
            with self.lock:
                if self.pending_scan:
                    self.pending_scan.update({'status': 'done', 'finished': int(time.time()), 'count': len(cached_files), 'cached': True})
                    self.pending_scan = None
            self.log('Main library scan skipped; cache still valid.')
            return
        found = self._scan_audio_paths([folder], include_subfolders=True)
        with self.lock:
            self.library_files = found
            self.library_cache_meta = signature
            save_json(LIBRARY_CACHE_FILE, {'meta': signature, 'files': found})
            if self.pending_scan:
                self.pending_scan.update({'status': 'done', 'finished': int(time.time()), 'count': len(found), 'cached': False})
                self.pending_scan = None
        self.bump('library')
        self.log(f'Scanned main library: {len(found)} audio file(s).')

    def _scan_show_worker(self, force: bool):
        parent = self.settings.parent_library_folder
        if not parent or not os.path.isdir(parent):
            raise FileNotFoundError('Show library folder not found.')
        with self.lock:
            if self.pending_show_scan:
                self.pending_show_scan['status'] = 'running'
                self.pending_show_scan['started'] = int(time.time())
        signature = self.folder_signature(parent)
        with self.lock:
            cached_sig = dict(self.show_cache_meta)
            cached_folders = list(self.show_folders)
        if not force and cached_folders and cached_sig == signature:
            with self.lock:
                if self.pending_show_scan:
                    self.pending_show_scan.update({'status': 'done', 'finished': int(time.time()), 'count': len(cached_folders), 'cached': True})
                    self.pending_show_scan = None
            self.log('Show-folder scan skipped; cache still valid.')
            return
        folders = []
        for child in sorted(Path(parent).iterdir()):
            if child.is_dir():
                has_audio = any(p.suffix.lower() in SUPPORTED_EXTENSIONS for p in child.rglob('*') if p.is_file())
                if has_audio:
                    folders.append(str(child))
        with self.lock:
            self.show_folders = folders
            self.show_cache_meta = signature
            save_json(SHOW_CACHE_FILE, {'meta': signature, 'folders': folders})
            if self.pending_show_scan:
                self.pending_show_scan.update({'status': 'done', 'finished': int(time.time()), 'count': len(folders), 'cached': False})
                self.pending_show_scan = None
        self.bump('shows')
        self.log(f'Scanned show folders: {len(folders)} usable folder(s).')

    def _scan_fill_worker(self, force: bool):
        folders = list(self.settings.fill_folders or [])
        include_subfolders = bool(self.settings.fill_include_subfolders)
        if not folders:
            raise FileNotFoundError('No fill folders are configured.')
        with self.lock:
            if self.pending_fill_scan:
                self.pending_fill_scan['status'] = 'running'
                self.pending_fill_scan['started'] = int(time.time())
        signature = self.multi_folder_signature(folders, include_subfolders)
        with self.lock:
            cached_sig = dict(self.fill_cache_meta)
            cached_files = list(self.fill_files)
        if not force and cached_files and cached_sig == signature:
            with self.lock:
                if self.pending_fill_scan:
                    self.pending_fill_scan.update({'status': 'done', 'finished': int(time.time()), 'count': len(cached_files), 'cached': True})
                    self.pending_fill_scan = None
            self.log('Fill-folder scan skipped; cache still valid.')
            return
        found = self._scan_audio_paths(folders, include_subfolders=include_subfolders)
        with self.lock:
            self.fill_files = found
            self.fill_cache_meta = signature
            save_json(FILL_CACHE_FILE, {'meta': signature, 'files': found})
            if self.pending_fill_scan:
                self.pending_fill_scan.update({'status': 'done', 'finished': int(time.time()), 'count': len(found), 'cached': False})
                self.pending_fill_scan = None
        self.bump('fill')
        self.log(f'Scanned fill folders: {len(found)} audio file(s).')

    def _scan_commercials_worker(self, force: bool):
        folder = str(self.settings.commercials_folder or '').strip()
        if not folder or not os.path.isdir(folder):
            raise FileNotFoundError('Commercials folder not found.')
        with self.lock:
            if self.pending_commercial_scan:
                self.pending_commercial_scan['status'] = 'running'
                self.pending_commercial_scan['started'] = int(time.time())
        signature = self.folder_signature(folder)
        with self.lock:
            cached_sig = dict(self.commercial_cache_meta)
            cached_files = list(self.commercial_files)
        if not force and cached_files and cached_sig == signature:
            with self.lock:
                if self.pending_commercial_scan:
                    self.pending_commercial_scan.update({'status': 'done', 'finished': int(time.time()), 'count': len(cached_files), 'cached': True})
                    self.pending_commercial_scan = None
            self.log('Commercial scan skipped; cache still valid.')
            return
        found = self._scan_audio_paths([folder], include_subfolders=True)
        with self.lock:
            self.commercial_files = found
            self.commercial_cache_meta = signature
            save_json(COMMERCIAL_CACHE_FILE, {'meta': signature, 'files': found})
            if self.pending_commercial_scan:
                self.pending_commercial_scan.update({'status': 'done', 'finished': int(time.time()), 'count': len(found), 'cached': False})
                self.pending_commercial_scan = None
        self.bump('commercials')
        self.log(f'Scanned commercials: {len(found)} audio file(s).')

    def _commercial_sort_key(self, path: str):
        stem = Path(path).stem.lower()
        prefix = (self.settings.commercials_prefix or 'o').lower()
        num = 10**9
        if stem.startswith(prefix):
            tail = stem[len(prefix):]
            digits = ''
            for ch in tail:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            if digits:
                try:
                    num = int(digits)
                except Exception:
                    num = 10**9
        return (num, stem, path.lower())

    def _pick_commercial(self) -> str:
        self.ensure_commercials_loaded()
        with self.lock:
            files = list(self.commercial_files)
            mode = self.settings.commercials_mode
            idx = int(self.commercial_sequence_index)
        if not files:
            raise RuntimeError('No commercial audio files found yet. Run the commercial scan and wait for it to finish.')
        if mode == 'ordered_label':
            ordered = sorted(files, key=self._commercial_sort_key)
            path = ordered[idx % len(ordered)]
            with self.lock:
                self.commercial_sequence_index = (idx + 1) % max(1, len(ordered))
            return path
        return random.choice(files)

    def _reset_commercial_state(self, clear_pending: bool = True):
        with self.lock:
            if clear_pending:
                self.pending_commercial_break = 0
                self._pending_commercial_trigger = ''
                self._between_show_next_item = None
            self.commercial_break_remaining = 0
            self.commercial_break_started_at = 0.0
            self.resume_after_commercial = None
            self.commercial_session_id += 1

    def _commercial_break_timed_out(self) -> bool:
        with self.lock:
            started = float(self.commercial_break_started_at or 0.0)
            remaining = int(self.commercial_break_remaining or 0)
            # If already cleared by another path, don't fire the watchdog.
            if started <= 0:
                return False
            elapsed = time.time() - started
            if elapsed <= float(self.commercial_break_timeout_seconds):
                return False
        self.log(f'Commercial break watchdog fired after {int(elapsed)} second(s); clearing remaining={remaining}. Resuming audio.')
        self._reset_commercial_state(clear_pending=True)
        # Watchdog cleared a stuck commercial break — restart audio so the
        # player doesn't go silent. Try to resume the interrupted track first,
        # then fall back to the active segment / scheduler.
        try:
            self._resume_after_commercial_or_continue()
        except Exception as exc:
            self.log(f'Watchdog recovery failed: {exc}')
        return True

    def _pick_spot_count(self) -> int:
        """Return how many spots to play in this break, respecting random min/max if set."""
        s = self.settings
        lo = int(s.commercials_spots_min or 0)
        hi = int(s.commercials_spots_max or 0)
        fixed = max(1, int(s.commercials_per_break or 1))
        if lo > 0 and hi >= lo:
            return random.randint(lo, hi)
        return fixed

    def _commercial_break_allowed(self, trigger: str = '') -> bool:
        """Central gate that enforces all commercial break rules.

        trigger can be: 'per_hour', 'between_shows', 'end_of_show',
                        'end_of_track', or '' (manual/test).
        Returns True if the break is permitted right now.
        """
        s = self.settings
        now = time.time()

        # Read shared mutable state atomically to avoid data races with the
        # playback monitor and _start_show_segment which write these values.
        with self.lock:
            last_break_ended_at = float(self.last_break_ended_at or 0.0)
            show_started_at = float(self.show_started_at or 0.0)
            breaks_this_show = int(self.breaks_this_show or 0)

        # Scheduled shows only — suppress during random fill and random playback
        if s.commercials_scheduled_only:
            current_kind = self.player.current_kind or ''
            if not current_kind.startswith('scheduled:'):
                self.log(f'Commercial break suppressed — scheduled shows only mode, current kind is {current_kind!r}.')
                return False

        # Quiet hours — suppress if current hour is in the quiet list
        current_hour = time.localtime().tm_hour
        if s.commercials_quiet_hours and current_hour in s.commercials_quiet_hours:
            self.log(f'Commercial break suppressed — quiet hour {current_hour:02d}:xx.')
            return False

        # Minimum gap between breaks.
        # end_of_track and end_of_show are explicit per-track/per-show triggers —
        # applying a time-based gap guard to them would suppress every second
        # break (the gap since the last break is always shorter than the track
        # duration). These triggers bypass the gap check intentionally.
        min_gap = int(s.commercials_min_gap_minutes or 0)
        if min_gap > 0 and last_break_ended_at > 0 and trigger not in ('end_of_track', 'end_of_show'):
            elapsed = (now - last_break_ended_at) / 60.0
            if elapsed < min_gap:
                self.log(f'Commercial break suppressed — minimum gap {min_gap}m not reached ({elapsed:.1f}m since last break).')
                return False

        # Minimum show runtime before first break.
        # end_of_track bypasses this too — if the user wants a break after every
        # track, the show-age check would suppress the very first break entirely.
        min_runtime = int(s.commercials_min_show_runtime_minutes or 0)
        if min_runtime > 0 and show_started_at > 0 and trigger != 'end_of_track':
            show_age = (now - show_started_at) / 60.0
            if show_age < min_runtime:
                self.log(f'Commercial break suppressed — show minimum runtime {min_runtime}m not reached ({show_age:.1f}m into show).')
                return False

        # Maximum breaks per show block
        max_breaks = int(s.commercials_max_breaks_per_show or 0)
        if max_breaks > 0 and breaks_this_show >= max_breaks:
            self.log(f'Commercial break suppressed — max {max_breaks} break(s) per show already reached ({breaks_this_show}).')
            return False

        return True

    def _start_commercial_break(self, count: int | None = None, trigger: str = ''):
        if self._is_chime_active():
            return False
        if count is None:
            with self.lock:
                count = int(self.pending_commercial_break or 0)
        count = max(0, int(count or 0))
        if count <= 0:
            self._reset_commercial_state(clear_pending=True)
            return False

        # Enforce break rules (skip for manual/test trigger where trigger=='')
        if trigger and not self._commercial_break_allowed(trigger):
            self._reset_commercial_state(clear_pending=True)
            return False

        # Capture the current track so we can resume it after the break.
        # Only capture once — if _start_commercial_break is called again for
        # subsequent spots in the same break, resume_after_commercial is
        # already set and we leave it alone.
        with self.lock:
            already_have_resume = self.resume_after_commercial is not None

        if not already_have_resume:
            resume_state = self._capture_resume_state()
            with self.lock:
                self.resume_after_commercial = resume_state

        # Fade out the currently playing track before cutting to the ad.
        # This runs synchronously but is short (fade_out_seconds).
        if self.settings.fade_enabled and self.player.is_playing():
            self.player.fade_to(0, float(self.settings.fade_out_seconds))

        path = self._pick_commercial()
        remaining = max(0, count - 1)
        self._play_path(path, 'commercial')
        with self.lock:
            self.pending_commercial_break = 0
            self.commercial_break_remaining = remaining
            self.commercial_break_started_at = time.time()
            self.commercial_session_id += 1
            self.breaks_this_show += 1
            seg_label = (self.active_segment or {}).get('label', '')
        if seg_label:
            self.log(f'Starting commercial break ({count} spot(s)) — will return to "{seg_label}" after.')
        else:
            self.log(f'Starting commercial break with {count} spot(s).')
        return True

    def _extend_segment_for_commercial_break(self):
        """Call this once when a commercial break finishes to give the
        interrupted show back the time the ads consumed."""
        break_duration = 0.0
        label = ''
        with self.lock:
            started = float(self.commercial_break_started_at or 0.0)
            if not started or not self.active_segment:
                return
            break_duration = time.time() - started
            if break_duration > 0 and self.active_segment.get('end_epoch'):
                self.active_segment['end_epoch'] += break_duration
                label = self.active_segment.get('label', '')
        if break_duration > 0 and label:
            self.log(f'Extended "{label}" by {int(break_duration)}s to account for commercial break.')

    def _tracks_from_folder(self, folder: str) -> list[str]:
        tracks = []
        if os.path.isdir(folder):
            for root, _, files in os.walk(folder):
                for name in files:
                    if Path(name).suffix.lower() in SUPPORTED_EXTENSIONS:
                        tracks.append(str(Path(root) / name))
        return tracks

    def _kind_supports_fade(self, kind: str) -> bool:
        kind = str(kind or '')
        return bool(kind) and not kind.startswith('hour_chime') and not kind.startswith('chime_strikes')

    def _play_path(self, path: str, kind: str, allow_fade_in: bool = True):
        use_fade = bool(self.settings.fade_enabled and allow_fade_in and self._kind_supports_fade(kind))
        target_volume = int(self.settings.volume)
        if use_fade:
            self.player.set_volume(0)
        else:
            self.player.set_volume(target_volume)
        self.player.play(path, kind)
        self.fade_triggered_for = ''
        if use_fade:
            # Always run fade-in off the calling thread to avoid blocking HTTP handlers
            # for up to fade_in_seconds (which can be up to 10s).
            threading.Thread(
                target=self.player.fade_to,
                args=(target_volume, float(self.settings.fade_in_seconds)),
                daemon=True,
            ).start()
        return path

    def pick_random(self, use_fill_source: bool = False):
        files = []
        kind = 'random'
        if use_fill_source and self.settings.fill_source_mode == 'selected_folders':
            self.ensure_fill_loaded()
            with self.lock:
                files = list(self.fill_files)
            kind = 'random_fill'
            if not files:
                self.log('Selected fill folders were requested, but fill scan is empty; falling back to main library.')
        if not files:
            self.ensure_library_loaded()
            with self.lock:
                files = list(self.library_files)
            kind = 'random'
        if not files:
            raise RuntimeError('No audio files found yet. Run the appropriate scan and wait for it to finish.')
        path = random.choice(files)
        return self._play_path(path, kind)

    def _choose_nonrepeat(self, tracks: list[str], recent: list[str] | None = None) -> str:
        if not tracks:
            raise RuntimeError('No tracks available.')
        recent = [x for x in (recent or []) if x]
        pool = [t for t in tracks if t not in recent]
        if not pool:
            pool = list(tracks)
        return random.choice(pool)

    def _segment_end_epoch(self, item: dict) -> float:
        hours = float(item.get('hours', 0) or 0)
        seconds = max(1, int(round(hours * 3600)))
        return time.time() + seconds

    def _start_show_segment(self, item: dict):
        folder = item.get('folder', '')
        label = item.get('label', '') or Path(folder).name
        tracks = self._tracks_from_folder(folder)
        if not tracks:
            raise RuntimeError(f'No audio files found in scheduled folder: {folder}')
        with self.lock:
            recent = list((self.active_segment or {}).get('recent', [])) if self.active_segment else []
        path = self._choose_nonrepeat(tracks, recent[:1])
        self._play_path(path, f'scheduled:{label}')
        with self.lock:
            self.show_started_at = time.time()
            self.breaks_this_show = 0
            self.active_segment = {
                'type': 'show',
                'index': item.get('index', 0),
                'label': label,
                'folder': folder,
                'start_time': item.get('start_time', ''),
                'start_minutes': item.get('start_minutes'),
                'end_time': item.get('end_time', ''),
                'end_minutes': item.get('end_minutes'),
                'end_epoch': self._segment_end_epoch(item),
                'recent': [path],
            }
        return path

    def _start_fill_segment(self, item: dict):
        path = self.pick_random(use_fill_source=True)
        with self.lock:
            self.show_started_at = time.time()
            self.breaks_this_show = 0
            self.active_segment = {
                'type': 'fill',
                'label': item.get('label', 'Random Fill'),
                'start_time': item.get('start_time', ''),
                'start_minutes': item.get('start_minutes'),
                'end_time': item.get('end_time', ''),
                'end_minutes': item.get('end_minutes'),
                'end_epoch': self._segment_end_epoch(item),
                'recent': [path],
            }
        return path

    def play_schedule_block(self, index: int = 0, item: dict | None = None):
        blocks = self.settings.program_blocks or []
        if not blocks:
            raise RuntimeError('No scheduler blocks saved.')
        if index < 0 or index >= len(blocks):
            index = 0
        if item is None:
            schedule = self.compute_schedule()
            item = next((x for x in schedule if x.get('type') == 'show' and int(x.get('index', -1)) == index), None)
            if item is None:
                block = blocks[index]
                folder = block.get('folder', '')
                label = block.get('label', '') or Path(folder).name
                item = {'type': 'show', 'index': index, 'folder': folder, 'label': label, 'hours': float(block.get('hours', 0) or 0)}
        return self._start_show_segment(item)

    def generate_strike_file(self, strikes: int) -> str:
        strikes = max(1, min(12, int(strikes)))
        sample_rate = 44100
        tone_hz = 880.0
        tone_duration = 0.18
        gap_duration = 0.14
        amplitude = 0.28
        frames = []
        for _ in range(strikes):
            tone_frames = int(sample_rate * tone_duration)
            for i in range(tone_frames):
                env = 1.0 - (i / max(1, tone_frames))
                sample = amplitude * env * math.sin(2 * math.pi * tone_hz * (i / sample_rate))
                frames.append(int(max(-1.0, min(1.0, sample)) * 32767))
            frames.extend([0] * int(sample_rate * gap_duration))
        fd, path = tempfile.mkstemp(prefix='pi_radio_chime_', suffix='.wav')
        os.close(fd)
        with wave.open(path, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(b''.join(struct.pack('<h', s) for s in frames))
        return path

    def _register_temp_media(self, path: str):
        if not path:
            return
        with self.lock:
            self.temp_generated_media.add(path)

    def _cleanup_temp_media(self, path: str):
        if not path:
            return
        should_delete = False
        with self.lock:
            if path in self.temp_generated_media:
                self.temp_generated_media.discard(path)
                should_delete = True
        if should_delete:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass

    def _cleanup_all_temp_media(self):
        with self.lock:
            paths = list(self.temp_generated_media)
            self.temp_generated_media.clear()
        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass

    def _capture_resume_state(self):
        if not self.player.is_playing():
            return None
        # Read path and kind atomically under the player's own lock so we
        # never see a torn state where current_path belongs to one track
        # and current_kind to another (possible if play() is called on a
        # different thread between the two reads).
        with self.player.lock:
            path = self.player.current_path
            kind = self.player.current_kind
        if not path or kind.startswith('hour_chime') or kind.startswith('chime_strikes'):
            return None
        time_ms = self.player.current_time_ms()
        # If VLC is still buffering/opening, current_time_ms() returns 0.
        # Wait briefly and retry so we don't resume from position 0.
        if time_ms == 0:
            time.sleep(0.15)
            time_ms = self.player.current_time_ms()
        return {'path': path, 'kind': kind, 'time_ms': time_ms}

    def _set_chime_active(self, kind: str):
        with self.lock:
            self.chime_active = True
            self.current_chime_kind = kind
            self.current_chime_started_at = time.time()

    def _clear_chime_active(self):
        with self.lock:
            self.chime_active = False
            self.current_chime_kind = ''
            self.current_chime_started_at = 0.0

    def _is_chime_active(self) -> bool:
        with self.lock:
            return bool(self.chime_active)

    def _finish_chime_and_resume(self):
        chime_path = self.player.current_path
        with self.lock:
            resume = self.resume_after_chime
            had_active_audio = bool(resume)
            self.resume_after_chime = None
        self._clear_chime_active()
        self._cleanup_temp_media(chime_path)
        if resume:
            if resume.get('interrupt_only'):
                pass  # interrupt mode: don't resume position, fall through to scheduler handoff
            else:
                try:
                    use_fade = bool(self.settings.fade_enabled)
                    self.player.resume_saved(
                        resume['path'],
                        resume['kind'],
                        resume['time_ms'],
                        fade_in=use_fade,
                        fade_in_seconds=float(self.settings.fade_in_seconds),
                        target_volume=int(self.settings.volume),
                    )
                    return 'resumed'
                except Exception as exc:
                    self.log(f'Could not resume after chime: {exc}')
        with self.lock:
            pending_break = int(self.pending_commercial_break or 0)
            pending_trigger = str(self._pending_commercial_trigger or '')
        if pending_break > 0:
            try:
                started = self._start_commercial_break(pending_break, trigger=pending_trigger)
                if started:
                    return 'commercial_started'
            except Exception as exc:
                self.log(f'Could not start queued commercial break after chime: {exc}')
                self._reset_commercial_state(clear_pending=True)
        # Hand off to the scheduler if something was playing before the chime.
        # If nothing was playing (had_active_audio is False), the chime fired
        # from a fully idle state — stay silent afterward.
        if had_active_audio:
            return 'no_resume'
        return 'no_resume_idle'

    def trigger_hourly_chime(self, hour24: int, test_only: bool = False):
        s = self.settings
        if not s.hourly_chimes_enabled and not test_only:
            return {'ok': True, 'message': 'Hourly chimes disabled'}
        was_playing = self.player.is_playing()
        resume_state = self._capture_resume_state() if not s.interrupt_hourly else None
        if s.interrupt_hourly and was_playing and resume_state is None:
            resume_state = {'interrupt_only': True}
        mode = s.chime_mode
        if mode == 'audio_drop':
            path = (s.hourly_audio_paths[hour24] if hour24 < len(s.hourly_audio_paths) else '').strip()
            if not path:
                raise RuntimeError(f'No hourly audio drop set for {hour24:02d}:00')
            kind = f'hour_chime:{hour24:02d}'
            with self.lock:
                self.resume_after_chime = resume_state
            self._set_chime_active(kind)
            self.player.play(path, kind)
            return {'ok': True, 'mode': mode, 'path': path, 'resumes': bool(resume_state)}
        strikes = (hour24 % 12) or 12
        path = self.generate_strike_file(strikes)
        self._register_temp_media(path)
        kind = f'chime_strikes:{strikes}'
        with self.lock:
            self.resume_after_chime = resume_state
        self._set_chime_active(kind)
        self.player.play(path, kind)
        return {'ok': True, 'mode': mode, 'path': path, 'strikes': strikes, 'resumes': bool(resume_state)}

    def compute_schedule(self):
        blocks = self.settings.program_blocks or []
        start_hour = int(self.settings.duration_start_hour) % 24
        start_minute = int(self.settings.duration_start_minute) % 60
        fill_mode = self.settings.schedule_fill_mode
        out = []
        cursor = start_hour * 60 + start_minute
        total_minutes = 0
        for idx, block in enumerate(blocks):
            try:
                hours = float(block.get('hours', 0) or 0)
            except Exception:
                hours = 0.0
            if hours <= 0:
                continue
            block_minutes = int(round(hours * 60))
            if total_minutes + block_minutes > 1440:
                remaining = max(0, 1440 - total_minutes)
                self.log(f"Schedule block {block.get('label') or Path(block.get('folder', '')).name or idx} exceeds 24 hours total; truncating remaining schedule.")
                if remaining <= 0:
                    break
                hours = round(remaining / 60.0, 2)
                block_minutes = remaining
            start = cursor % 1440
            end_cursor = cursor + block_minutes
            end = end_cursor % 1440
            label = block.get('label', '') or Path(block.get('folder', '')).name
            out.append({
                'type': 'show', 'index': idx, 'label': label, 'folder': block.get('folder', ''),
                'hours': hours, 'start_minutes': start, 'end_minutes': end,
                'start_time': f'{start // 60:02d}:{start % 60:02d}',
                'end_time': f'{end // 60:02d}:{end % 60:02d}',
            })
            cursor = end_cursor
            total_minutes += block_minutes
            if total_minutes >= 1440:
                break
        if fill_mode == 'random' and total_minutes < 1440:
            start = cursor % 1440
            end = (start_hour * 60 + start_minute) % 1440
            out.append({
                'type': 'fill', 'label': 'Random Fill', 'hours': round((1440 - total_minutes) / 60, 2),
                'start_minutes': start, 'end_minutes': end,
                'start_time': f'{start // 60:02d}:{start % 60:02d}',
                'end_time': f'{end // 60:02d}:{end % 60:02d}',
            })
        elif fill_mode == 'loop' and total_minutes > 0 and total_minutes < 1440:
            start = cursor % 1440
            end = (start_hour * 60 + start_minute) % 1440
            out.append({
                'type': 'fill', 'label': 'Loop Schedule', 'hours': round((1440 - total_minutes) / 60, 2),
                'start_minutes': start, 'end_minutes': end,
                'start_time': f'{start // 60:02d}:{start % 60:02d}',
                'end_time': f'{end // 60:02d}:{end % 60:02d}',
            })
        return out

    def _commercial_break_due(self, minute: int) -> bool:
        per_hour = int(self.settings.commercials_per_hour or 0)
        if not (self.settings.commercials_enabled and per_hour > 0) or minute == 0:
            return False
        targets = sorted({max(1, min(59, int(round((60.0 * idx) / (per_hour + 1))))) for idx in range(1, per_hour + 1)})
        return minute in targets

    def _minute_in_window(self, minute_of_day: int, start_minutes: int, end_minutes: int) -> bool:
        start_minutes = int(start_minutes) % 1440
        end_minutes = int(end_minutes) % 1440
        minute_of_day = int(minute_of_day) % 1440
        # Equal start/end represents a full-day window, not an empty one.
        if start_minutes == end_minutes:
            return True
        if start_minutes < end_minutes:
            return start_minutes <= minute_of_day < end_minutes
        return minute_of_day >= start_minutes or minute_of_day < end_minutes

    def _current_schedule_item(self, minute_of_day: int):
        for item in self.compute_schedule():
            if self._minute_in_window(minute_of_day, item['start_minutes'], item['end_minutes']):
                return item
        return None

    def _clear_active_segment(self):
        with self.lock:
            self.active_segment = None

    def _set_pending_schedule_item(self, item: dict | None):
        with self.lock:
            self.pending_schedule_item = dict(item) if item else None

    def _get_pending_schedule_item(self):
        with self.lock:
            return dict(self.pending_schedule_item) if self.pending_schedule_item else None

    def _resume_after_commercial_or_continue(self):
        """Called when a commercial break ends. Tries to resume the exact
        track that was playing before the ads (at the same position with
        fade-in). Falls back to _continue_active_segment (next track in
        show folder) if no resume state was captured, then to a full
        schedule handoff if there is no active segment."""
        # Check if this was a between-show commercial break
        with self.lock:
            next_item = self._between_show_next_item
            self._between_show_next_item = None
            resume = self.resume_after_commercial
        self._reset_commercial_state(clear_pending=False)
        with self.lock:
            self.last_break_ended_at = time.time()
        if next_item:
            self.log(f'Between-show commercials done — starting "{next_item.get("label", "")}".')
            try:
                self._start_item(next_item, force=True)
            except Exception as exc:
                self.log(f'Could not start next show after commercials: {exc}')
            return
        if resume:
            try:
                self.log(f'Resuming "{Path(resume["path"]).name}" at {resume["time_ms"]}ms after commercial break.')
                use_fade = bool(self.settings.fade_enabled)
                self.player.resume_saved(
                    resume['path'],
                    resume['kind'],
                    resume['time_ms'],
                    fade_in=use_fade,
                    fade_in_seconds=float(self.settings.fade_in_seconds),
                    target_volume=int(self.settings.volume),
                )
                return
            except Exception as exc:
                self.log(f'Could not resume track after commercial break: {exc}')
        # No resume state (e.g. test commercial, or capture failed) —
        # play the next track from the show folder as before.
        if not self._continue_active_segment(came_from_commercial=True):
            self._handoff_after_segment_finish()

    def _continue_active_segment(self, came_from_commercial: bool = False):
        with self.lock:
            seg = dict(self.active_segment or {})
            pending = dict(self.pending_schedule_item) if self.pending_schedule_item else None
        if not seg:
            return False

        # When a pending schedule change is waiting, this is the last track of
        # the current show.  If end_of_track commercials are enabled we still
        # want to fire one here — the commercial will finish and then
        # _resume_after_commercial_or_continue → _handoff_after_segment_finish
        # will pick up the pending item and start the next show.
        # Without this check the pending-item branch returns False immediately,
        # bypassing the end_of_track block below and skipping the commercial
        # on the show boundary.
        if pending:
            if (not came_from_commercial
                    and self.settings.commercials_enabled
                    and self.settings.commercials_end_of_track
                    and self.commercial_files
                    and not self._is_chime_active()):
                count = self._pick_spot_count()
                with self.lock:
                    self.pending_commercial_break = count
                    self._pending_commercial_trigger = ''
                    self.resume_after_commercial = None
                self.log(f'End-of-track (show boundary): queuing {count} commercial(s) before "{pending.get("label", "")}".')
                try:
                    started = self._start_commercial_break(count, trigger='end_of_track')
                    if started:
                        self._clear_active_segment()
                        return True  # commercial playing; handoff will fire when it ends
                except Exception as exc:
                    self.log(f'End-of-track commercial failed at show boundary: {exc}')
                    with self.lock:
                        self.pending_commercial_break = 0
            self.log(f"Segment {seg.get('label','')} finished; switching to pending block {pending.get('label','')}.")
            self._clear_active_segment()
            return False

        if time.time() >= float(seg.get('end_epoch', 0) or 0):
            self.log(f"Segment {seg.get('label','')} reached its end; letting current file finish before switching.")
            self._clear_active_segment()
            return False

        # End-of-track commercial break — skip if we just came from a commercial
        # to prevent an infinite loop (commercial ends → trigger → commercial → repeat)
        if (not came_from_commercial
                and self.settings.commercials_enabled
                and self.settings.commercials_end_of_track
                and self.commercial_files
                and not self._is_chime_active()):
            count = self._pick_spot_count()
            with self.lock:
                self.pending_commercial_break = count
                self._pending_commercial_trigger = ''
                # Explicitly clear resume state so after the break we play the
                # NEXT track rather than replaying the one that just finished.
                self.resume_after_commercial = None
            self.log(f'End-of-track: queuing {count} commercial(s).')
            try:
                started = self._start_commercial_break(count, trigger='end_of_track')
                if started:
                    return True  # commercial break is now playing; monitor will continue segment after
            except Exception as exc:
                self.log(f'End-of-track commercial failed: {exc}')
                with self.lock:
                    self.pending_commercial_break = 0

        if seg.get('type') == 'show':
            tracks = self._tracks_from_folder(seg.get('folder', ''))
            if not tracks:
                self.log(f"Scheduled folder is empty or unavailable: {seg.get('folder','')}")
                self._clear_active_segment()
                return False
            recent = list(seg.get('recent', []))[:3]
            path = self._choose_nonrepeat(tracks, recent)
            self._play_path(path, f"scheduled:{seg.get('label','Show')}")
            with self.lock:
                if self.active_segment:
                    recent = [path] + list(self.active_segment.get('recent', []))[:4]
                    self.active_segment['recent'] = recent
            return True
        if seg.get('type') == 'fill':
            path = self.pick_random(use_fill_source=True)
            with self.lock:
                if self.active_segment:
                    recent = [path] + list(self.active_segment.get('recent', []))[:4]
                    self.active_segment['recent'] = recent
            return True
        return False

    def _start_item(self, item: dict, force: bool = False):
        _now = time.localtime()
        year, yday = _now.tm_year, _now.tm_yday
        minute_of_day = _now.tm_hour * 60 + _now.tm_min
        key = f"{year}-{yday}-{item['label']}-{item['start_minutes']}"
        if not force and key == self.last_schedule_key:
            return False
        self.last_schedule_key = key
        self._set_pending_schedule_item(None)
        if item['type'] == 'show':
            self.log(f"Scheduler starting block {item['label']} ({item['start_time']}-{item['end_time']})")
            self.play_schedule_block(item['index'], item)
            self.current_segment_label = item['label']
            return True
        if item['label'] == 'Random Fill':
            self.log('Scheduler entering Random Fill.')
            self._start_fill_segment(item)
            self.current_segment_label = 'Random Fill'
            return True
        if item['label'] == 'Loop Schedule':
            first = next((x for x in self.compute_schedule() if x.get('type') == 'show'), None)
            if first:
                self.play_schedule_block(first['index'], first)
                self.current_segment_label = 'Loop Schedule'
                return True
        return False

    def _handoff_after_segment_finish(self):
        pending = self._get_pending_schedule_item()
        if pending:
            item = pending
            self._set_pending_schedule_item(None)
        else:
            minute_of_day = time.localtime().tm_hour * 60 + time.localtime().tm_min
            item = self._current_schedule_item(minute_of_day)
        if not item:
            fill_mode = self.settings.schedule_fill_mode
            if fill_mode == 'random':
                self.log('Schedule ended — fill mode is random, starting fill playback.')
                fill_item = {'type': 'fill', 'label': 'Random Fill', 'start_time': '', 'end_time': '', 'start_minutes': None, 'end_minutes': None, 'hours': 24}
                try:
                    self._start_fill_segment(fill_item)
                except Exception as exc:
                    self.log(f'Could not start fill after schedule end: {exc}')
            elif fill_mode == 'loop':
                self.log('Schedule ended — fill mode is loop, restarting schedule from block 0.')
                try:
                    self.play_schedule_block(0)
                except Exception as exc:
                    self.log(f'Could not loop schedule after end: {exc}')
            else:
                self.log('Schedule ended — fill mode is stop, going silent.')
            return
        # Between-shows commercial (original option)
        if (self.settings.commercials_enabled
                and self.settings.commercials_between_shows
                and self.commercial_files
                and not self._is_chime_active()):
            count = self._pick_spot_count()
            with self.lock:
                self._between_show_next_item = item
                self.pending_commercial_break = count
                self.resume_after_commercial = None
            self.log(f'Playing {count} commercial(s) between shows before "{item.get("label", "")}".')
            try:
                started = self._start_commercial_break(count, trigger='between_shows')
            except Exception as exc:
                self.log(f'Between-show commercial failed: {exc}')
                started = False
            if started:
                return
            # Guard blocked the break — clear stale state and fall through to start next show
            with self.lock:
                self._between_show_next_item = None
        # End-of-show commercial (new option — does not require between_shows)
        if (self.settings.commercials_enabled
                and self.settings.commercials_end_of_show
                and self.commercial_files
                and not self._is_chime_active()
                and not self.settings.commercials_between_shows):
            count = self._pick_spot_count()
            with self.lock:
                self._between_show_next_item = item
                self.pending_commercial_break = count
                self.resume_after_commercial = None
            self.log(f'End-of-show: playing {count} commercial(s) before "{item.get("label", "")}".')
            try:
                started = self._start_commercial_break(count, trigger='end_of_show')
            except Exception as exc:
                self.log(f'End-of-show commercial failed: {exc}')
                started = False
            if started:
                return
            # Guard blocked the break — clear stale state and fall through to start next show
            with self.lock:
                self._between_show_next_item = None
        try:
            self._start_item(item, force=True)
        except Exception as exc:
            self.log(f'Could not hand off after segment finish: {exc}')

    def playback_monitor_loop(self):
        last_playing = False
        last_kind = ''
        last_path = ''
        not_playing_count = 0   # debounce: require 2 consecutive not-playing polls
        while not self.shutdown_event.is_set():
            try:
                self.last_playback_heartbeat = time.time()
                # Watchdog: clear a commercial break that has been stuck for
                # longer than commercial_break_timeout_seconds so the player
                # can recover on its own without needing a restart.
                self._commercial_break_timed_out()
                playing = self.player.is_playing()
                kind = self.player.current_kind or ''
                path = self.player.current_path or ''
                current_signature = f'{kind}|{path}' if kind or path else ''

                # If VLC hit an error state (bad file, USB timeout, network
                # drop), log it and treat it as an immediate end-of-track so
                # recovery fires without waiting for the debounce counter.
                if not playing and kind and path and self.player.is_error():
                    err_sig = (kind, path)
                    if self.last_finished_signature != err_sig:
                        self.last_finished_signature = err_sig
                        self.log(f'VLC error state on "{Path(path).name}" ({kind}) — recovering.')
                        if kind.startswith('hour_chime') or kind.startswith('chime_strikes'):
                            result = self._finish_chime_and_resume()
                            if result in ('no_resume', 'resume_failed'):
                                self._handoff_after_segment_finish()
                        elif kind == 'commercial':
                            with self.lock:
                                remaining = int(self.commercial_break_remaining or 0)
                            if remaining > 0:
                                try:
                                    self._start_commercial_break(remaining)
                                except Exception:
                                    self._reset_commercial_state(clear_pending=True)
                                    self._resume_after_commercial_or_continue()
                            else:
                                self._extend_segment_for_commercial_break()
                                self._resume_after_commercial_or_continue()
                        elif kind.startswith('scheduled:') or kind == 'random_fill':
                            if not self._continue_active_segment():
                                self._handoff_after_segment_finish()
                    last_playing = False
                    last_kind = kind
                    last_path = path
                    self.shutdown_event.wait(0.25)
                    continue
                if playing and self.settings.fade_enabled and self._kind_supports_fade(kind):
                    total_ms = self.player.total_length_ms()
                    current_ms = self.player.current_time_ms()
                    remaining_ms = max(0, total_ms - current_ms)
                    fade_window_ms = int(max(0.0, float(self.settings.fade_out_seconds)) * 1000)
                    if total_ms > 0 and fade_window_ms > 0 and 0 < remaining_ms <= fade_window_ms:
                        if self.fade_triggered_for != current_signature:
                            self.fade_triggered_for = current_signature
                            duration = float(self.settings.fade_out_seconds)
                            threading.Thread(target=self.player.fade_to, args=(0, duration), daemon=True).start()
                    elif self.fade_triggered_for and self.fade_triggered_for != current_signature:
                        self.fade_triggered_for = ''
                # Clear stale fade_triggered_for whenever the playing track changes,
                # regardless of whether fade is currently enabled.  If fade is toggled
                # off mid-session the value would otherwise linger and misfire when
                # fade is re-enabled.
                if not playing and not current_signature:
                    self.fade_triggered_for = ''
                elif self.fade_triggered_for and self.fade_triggered_for != current_signature:
                    self.fade_triggered_for = ''
                signature = (last_kind, last_path)
                if not playing:
                    not_playing_count += 1
                else:
                    not_playing_count = 0
                # Only treat as "stopped" after 2 consecutive not-playing polls.
                # We do NOT update last_playing to False until the threshold is
                # met, so the trigger condition (last_playing and not playing)
                # stays True across both polls.
                confirmed_stopped = not playing and not_playing_count >= 2
                if last_playing and confirmed_stopped:
                    if last_kind.startswith('hour_chime') or last_kind.startswith('chime_strikes'):
                        if self.last_finished_signature != signature:
                            self.last_finished_signature = signature
                            result = self._finish_chime_and_resume()
                            if result in ('no_resume', 'resume_failed'):
                                self._handoff_after_segment_finish()
                            elif result == 'no_resume_idle':
                                # Nothing was playing before the chime.
                                # Stay silent — but consume any pending
                                # schedule item or pending commercial break
                                # that was queued while the chime played so
                                # they aren't silently dropped.
                                pending = self._get_pending_schedule_item()
                                if pending:
                                    self._handoff_after_segment_finish()
                                else:
                                    with self.lock:
                                        pending_break = int(self.pending_commercial_break or 0)
                                    if pending_break > 0:
                                        # Clear stale queued break —
                                        # nothing to interrupt from idle.
                                        with self.lock:
                                            self.pending_commercial_break = 0
                    elif last_kind == 'commercial':
                        if self.last_finished_signature != signature:
                            self.last_finished_signature = signature
                            with self.lock:
                                remaining = int(self.commercial_break_remaining or 0)
                            if remaining > 0:
                                try:
                                    self._start_commercial_break(remaining)
                                except Exception as exc:
                                    self.log(f'Commercial break continuation failed: {exc}')
                                    with self.lock:
                                        self.commercial_break_remaining = 0
                                    self._extend_segment_for_commercial_break()
                                    self._resume_after_commercial_or_continue()
                            else:
                                # All commercials done — extend the show timer,
                                # then resume the exact track that was interrupted.
                                self._extend_segment_for_commercial_break()
                                self._resume_after_commercial_or_continue()
                    elif last_kind.startswith('scheduled:') or last_kind == 'random_fill':
                        if self.last_finished_signature != signature:
                            self.last_finished_signature = signature
                            with self.lock:
                                pending_break = int(self.pending_commercial_break or 0)
                                pending_trigger = str(self._pending_commercial_trigger or '')
                            if pending_break > 0 and not self._is_chime_active():
                                # Consume trigger before starting — prevents a stale
                                # 'per_hour' tag from bleeding into end_of_track breaks.
                                with self.lock:
                                    self._pending_commercial_trigger = ''
                                try:
                                    self._start_commercial_break(pending_break, trigger=pending_trigger)
                                except Exception as exc:
                                    self.log(f'Commercial break failed: {exc}')
                                    with self.lock:
                                        self.pending_commercial_break = 0
                                    if not self._continue_active_segment():
                                        self._handoff_after_segment_finish()
                            elif not self._continue_active_segment():
                                self._handoff_after_segment_finish()
                    elif signature != ('', ''):
                        self.last_finished_signature = signature
                if playing:
                    self.last_finished_signature = None
                    self.last_playing_at = time.time()
                # Only lower last_playing once the debounce confirms stopped,
                # so the trigger above keeps firing on poll 2.
                if confirmed_stopped or playing:
                    last_playing = playing
                elif kind and path:
                    # Silence watchdog: if something should be playing but
                    # isn't, and we've been silent longer than the threshold,
                    # try to recover. Guards against any edge case that slips
                    # past the debounce and error-state checks above.
                    silent_for = time.time() - self.last_playing_at
                    if silent_for >= self.silence_watchdog_seconds:
                        self.last_playing_at = time.time()  # reset so we don't spam
                        self.log(f'Silence watchdog: silent for {int(silent_for)}s with kind={kind!r} — recovering.')
                        if not self._continue_active_segment():
                            self._handoff_after_segment_finish()
                last_kind = kind
                last_path = path
            except Exception as exc:
                self.log(f'Playback monitor error: {exc}')
            self.shutdown_event.wait(0.25)

    def _catchup_schedule(self, reason: str = ''):
        """If the scheduler is enabled and nothing is currently playing,
        check whether the current time falls inside a scheduled block or fill
        window and start it immediately.  Called on backend startup and
        whenever settings are saved so a newly-configured schedule takes
        effect without waiting for the next exact start-minute boundary."""
        if not self.settings.scheduler_enabled:
            return
        if not self.settings.program_blocks:
            return
        # Don't interrupt anything already playing.
        if self.player.is_playing():
            return
        with self.lock:
            active = self.active_segment
        if active:
            return
        minute_of_day = time.localtime().tm_hour * 60 + time.localtime().tm_min
        item = self._current_schedule_item(minute_of_day)
        if not item:
            return
        label = item.get('label', '')
        try:
            if item.get('type') == 'show':
                self.log(f'Catch-up scheduler{" (" + reason + ")" if reason else ""}: starting "{label}" (currently in its window {item.get("start_time","")}-{item.get("end_time","")}).')
                self._start_item(item, force=True)
            elif item.get('type') == 'fill':
                fill_mode = self.settings.schedule_fill_mode
                if fill_mode == 'random':
                    self.log(f'Catch-up scheduler{" (" + reason + ")" if reason else ""}: starting random fill.')
                    self._start_fill_segment(item)
                elif fill_mode == 'loop':
                    self.log(f'Catch-up scheduler{" (" + reason + ")" if reason else ""}: loop mode — restarting schedule from block 0.')
                    self._start_item(item, force=True)
                # fill_mode == 'stop': intentionally do nothing — silence is correct
        except Exception as exc:
            self.log(f'Catch-up scheduler failed: {exc}')

    def scheduler_loop(self):
        while not self.shutdown_event.is_set():
            try:
                self.last_scheduler_heartbeat = time.time()
                now = time.localtime()
                self._check_minute(now.tm_year, now.tm_yday, now.tm_hour, now.tm_min)
            except Exception as exc:
                self.log(f'Scheduler error: {exc}')
            now_secs = time.time()
            sleep_for = 60 - (now_secs % 60) + 0.05
            self.shutdown_event.wait(timeout=max(0.5, sleep_for))

    def _check_minute(self, year: int, yday: int, hour: int, minute: int):
        if minute == 0 and self.settings.hourly_chimes_enabled:
            key = f'{year}-{yday}-{hour}'
            if key != self.last_chime_key:
                self.last_chime_key = key
                try:
                    self.trigger_hourly_chime(hour)
                except Exception as exc:
                    self.log(f'Hourly chime failed: {exc}')
        if self.settings.commercials_enabled and self.settings.commercials_per_hour > 0 and minute != 0:
            per_hour = int(self.settings.commercials_per_hour)
            targets = sorted({max(1, min(59, int(round((60.0 * idx) / (per_hour + 1))))) for idx in range(1, per_hour + 1)})
            if minute in targets:
                key = f'{year}-{yday}-{hour}-{minute}'
                if key != self.last_commercial_break_key:
                    self.last_commercial_break_key = key
                    count = self._pick_spot_count()
                    with self.lock:
                        self.pending_commercial_break = count
                        self._pending_commercial_trigger = 'per_hour'
                    self.log(f'Commercial break queued for {hour:02d}:{minute:02d} ({count} spot(s)).')
        if not self.settings.scheduler_enabled:
            return
        minute_of_day = hour * 60 + minute
        schedule = self.compute_schedule()
        for item in schedule:
            if item['start_minutes'] != minute_of_day:
                continue
            # Fill and loop gap items are handled by _handoff_after_segment_finish
            # when a show ends — the scheduler should never auto-start them on a
            # clock trigger, otherwise fill mode 'random' or 'loop' would kick in
            # on its own without the user asking for it.
            if item.get('type') != 'show':
                return
            try:
                current_kind = self.player.current_kind or ''
                current_path = self.player.current_path or ''
                is_active_audio = bool(current_path and (self.player.is_playing() or current_kind.startswith('scheduled:') or current_kind == 'random_fill'))
                if self._is_chime_active():
                    self._set_pending_schedule_item(item)
                    self.log(f"Scheduled change queued for {item.get('label','')} at {item.get('start_time','')}; waiting for hour chime to finish.")
                elif is_active_audio:
                    self._set_pending_schedule_item(item)
                    self.log(f"Scheduled change queued for {item.get('label','')} at {item.get('start_time','')}; current file will finish first.")
                else:
                    self._start_item(item, force=False)
            except Exception as exc:
                self.log(f"Could not start scheduled item {item.get('label','')}: {exc}")
            return

    def _build_status_payload(self, include_logs: bool = False):
        self.last_status_heartbeat = time.time()
        now_playing = self.player.current_path
        play_kind = self.player.current_kind
        is_playing = self.player.is_playing()
        current_time_ms = self.player.current_time_ms()
        total_length_ms = self.player.total_length_ms()
        # Compute schedule BEFORE acquiring the lock – it only reads settings and
        # does pure arithmetic, so there is no need to hold the state lock here.
        # Holding it during this call blocked the worker/scheduler/playback threads
        # for up to several milliseconds on every 5-second UI poll.
        computed_schedule = self.compute_schedule()
        with self.lock:
            payload = {
                'media_folder': self.settings.media_folder,
                'parent_library_folder': self.settings.parent_library_folder,
                'api_version': APP_VERSION,
                'vlc_available': bool(vlc),
                'uptime_seconds': int(max(0, time.time() - self.started_at)),
                'health': {
                    'worker_age_seconds': round(max(0.0, time.time() - self.last_worker_heartbeat), 2),
                    'scheduler_age_seconds': round(max(0.0, time.time() - self.last_scheduler_heartbeat), 2),
                    'playback_age_seconds': round(max(0.0, time.time() - self.last_playback_heartbeat), 2),
                    'status_age_seconds': round(max(0.0, time.time() - self.last_status_heartbeat), 2),
                },
                'now_playing': now_playing,
                'play_kind': play_kind,
                'is_playing': is_playing,
                'current_time_ms': current_time_ms,
                'total_length_ms': total_length_ms,
                'library_count': len(self.library_files),
                'show_folder_count': len(self.show_folders),
                'show_folders': list(self.show_folders),
                'fill_library_count': len(self.fill_files),
                'fill_folder_count': len(self.settings.fill_folders or []),
                'fill_folders': list(self.settings.fill_folders or []),
                'fill_source_mode': self.settings.fill_source_mode,
                'fill_include_subfolders': self.settings.fill_include_subfolders,
                'commercial_count': len(self.commercial_files),
                'commercials_enabled': self.settings.commercials_enabled,
                'commercials_folder': self.settings.commercials_folder,
                'commercials_mode': self.settings.commercials_mode,
                'commercials_per_hour': self.settings.commercials_per_hour,
                'commercials_per_break': self.settings.commercials_per_break,
                'commercials_prefix': self.settings.commercials_prefix,
                'commercials_between_shows': self.settings.commercials_between_shows,
                'commercials_end_of_show': self.settings.commercials_end_of_show,
                'commercials_end_of_track': self.settings.commercials_end_of_track,
                'commercials_min_gap_minutes': self.settings.commercials_min_gap_minutes,
                'commercials_min_show_runtime_minutes': self.settings.commercials_min_show_runtime_minutes,
                'commercials_max_breaks_per_show': self.settings.commercials_max_breaks_per_show,
                'commercials_spots_min': self.settings.commercials_spots_min,
                'commercials_spots_max': self.settings.commercials_spots_max,
                'commercials_quiet_hours': list(self.settings.commercials_quiet_hours),
                'commercials_scheduled_only': self.settings.commercials_scheduled_only,
                'scheduler_enabled': self.settings.scheduler_enabled,
                'volume': self.settings.volume,
                'scheduler_time': f"{int(self.settings.duration_start_hour):02d}:{int(self.settings.duration_start_minute):02d}",
                'duration_start_hour': int(self.settings.duration_start_hour),
                'duration_start_minute': int(self.settings.duration_start_minute),
                'schedule_fill_mode': self.settings.schedule_fill_mode,
                'computed_schedule': computed_schedule,
                'program_blocks': list(self.settings.program_blocks or []),
                'fade_enabled': self.settings.fade_enabled,
                'fade_out_seconds': self.settings.fade_out_seconds,
                'fade_in_seconds': self.settings.fade_in_seconds,
                'hourly_chimes_enabled': self.settings.hourly_chimes_enabled,
                'chime_mode': self.settings.chime_mode,
                'interrupt_hourly': self.settings.interrupt_hourly,
                'chimes_folder': self.settings.chimes_folder,
                'hourly_audio_paths': list(self.settings.hourly_audio_paths),
                'scan_library_job': dict(self.pending_scan) if self.pending_scan else None,
                'scan_show_job': dict(self.pending_show_scan) if self.pending_show_scan else None,
                'scan_fill_job': dict(self.pending_fill_scan) if self.pending_fill_scan else None,
                'scan_commercial_job': dict(self.pending_commercial_scan) if self.pending_commercial_scan else None,
                'revisions': dict(self.ui_revisions),
                'current_segment_label': self.current_segment_label,
                'pending_schedule_label': (self.pending_schedule_item or {}).get('label') if self.pending_schedule_item else '',
                'active_segment': dict(self.active_segment) if self.active_segment else None,
                'chime_active': self.chime_active,
                'pending_resume_after_chime': bool(self.resume_after_chime),
                'commercial_break_remaining': int(self.commercial_break_remaining or 0),
                'pending_commercial_break': int(self.pending_commercial_break or 0),
                'commercial_break_started_at': float(self.commercial_break_started_at or 0.0),
                'commercial_break_timeout_seconds': float(self.commercial_break_timeout_seconds),
                'cache_files': {
                    'library': str(LIBRARY_CACHE_FILE),
                    'shows': str(SHOW_CACHE_FILE),
                    'fill': str(FILL_CACHE_FILE),
                    'commercials': str(COMMERCIAL_CACHE_FILE),
                    'log': str(LOG_FILE),
                },
                'capabilities': {
                    'test_hour_chime': True,
                    'interrupt_hourly': True,
                    'pause_resume_hourly': True,
                    'background_scans': True,
                    'cached_library_index': True,
                    'schedule_fill_mode': True,
                    'fill_source_mode': True,
                    'fill_folders': True,
                },
            }
            if include_logs:
                payload['logs'] = list(self.logs[-80:])
            return payload

    def ui_status(self):
        return self._build_status_payload(include_logs=False)

    def status(self):
        return self._build_status_payload(include_logs=True)

state = BackendState()
server_ref = {'server': None}


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload, code=200):
        data = json.dumps(payload).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def _serve_html(self):
        ui_file = Path(__file__).resolve().parent / 'pi_radio_web.html'
        try:
            data = ui_file.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            msg = b'<h2>pi_radio_web.html not found. Place it in the same folder as radio_backend.py.</h2>'
            self.send_response(404)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/ui', '/index.html'):
            self._serve_html()
            return
        if path == '/status':
            self._send({'ok': True, 'status': state.status()})
        elif path == '/ui_status':
            self._send({'ok': True, 'status': state.ui_status()})
        elif path == '/show_folders':
            state.ensure_show_folders_loaded()
            self._send({'ok': True, 'show_folders': list(state.show_folders)})
        elif path == '/logs_tail':
            self._send({'ok': True, 'logs': list(state.logs[-80:])})
        elif path == '/commercials':
            state.ensure_commercials_loaded()
            self._send({'ok': True, 'commercials': list(state.commercial_files)})
        elif path == '/discover_paths':
            def _human_size(num_bytes: int) -> str:
                units = ['B', 'KB', 'MB', 'GB', 'TB']
                size = float(max(0, int(num_bytes)))
                for unit in units:
                    if size < 1024.0 or unit == units[-1]:
                        if unit == 'B':
                            return f'{int(size)}{unit}'
                        return f'{size:.1f}{unit}'
                    size /= 1024.0
                return f'{int(num_bytes)}B'

            def _mount_rows():
                rows = []
                seen = set()
                user = os.getenv('USER', 'pi')
                allowed_prefixes = [
                    '/media/',
                    f'/media/{user}/',
                    '/run/media/',
                    f'/run/media/{user}/',
                    '/mnt/',
                ]
                excluded_mounts = {
                    '/', '/boot', '/boot/firmware', '/home', '/tmp', '/var', '/var/tmp',
                    '/var/log', '/var/cache', '/var/lib', '/var/run', '/root', '/proc',
                    '/sys', '/dev', '/run'
                }
                excluded_fstypes = {
                    'proc', 'sysfs', 'devtmpfs', 'devpts', 'tmpfs', 'cgroup', 'cgroup2',
                    'overlay', 'squashfs', 'autofs', 'pstore', 'debugfs', 'tracefs',
                    'securityfs', 'mqueue', 'hugetlbfs', 'configfs', 'fusectl', 'rpc_pipefs'
                }
                try:
                    mounts_text = Path('/proc/mounts').read_text(encoding='utf-8', errors='ignore')
                except Exception:
                    mounts_text = ''
                for line in mounts_text.splitlines():
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    source, mountpoint, fstype = parts[0], parts[1].replace('\040', ' '), parts[2]
                    if fstype in excluded_fstypes:
                        continue
                    if mountpoint in excluded_mounts:
                        continue
                    if not any(mountpoint.startswith(prefix) for prefix in allowed_prefixes):
                        continue
                    p = Path(mountpoint)
                    if not p.exists() or not p.is_dir():
                        continue
                    if mountpoint in seen:
                        continue
                    seen.add(mountpoint)
                    try:
                        stat = os.statvfs(mountpoint)
                        total_bytes = int(stat.f_frsize * stat.f_blocks)
                        size = _human_size(total_bytes) if total_bytes > 0 else ''
                    except Exception:
                        size = ''
                    label = p.name or mountpoint
                    rows.append({
                        'path': mountpoint,
                        'label': label,
                        'source': source,
                        'size': size,
                        'fstype': fstype,
                    })
                rows.sort(key=lambda item: (item.get('label', '').lower(), item.get('path', '').lower()))
                return rows

            mounts = _mount_rows()
            self._send({'ok': True, 'mounts': mounts, 'paths': [item['path'] for item in mounts]})
        else:
            self._send({'ok': False, 'error': 'Not found'}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get('Content-Length', '0'))
        raw = self.rfile.read(length) if length else b'{}'
        try:
            payload = json.loads(raw.decode('utf-8') or '{}')
            if path == '/save_settings':
                refresh = state.save_settings_and_refresh_caches(payload)
                state.log('Settings saved from frontend.')
                self._send({'ok': True, **refresh})
            elif path == '/scan_library':
                job = state.enqueue_scan_library(force=bool(payload.get('force', False)))
                self._send({'ok': True, 'queued': True, 'job': job})
            elif path == '/scan_show_folders':
                job = state.enqueue_scan_show_folders(force=bool(payload.get('force', False)))
                self._send({'ok': True, 'queued': True, 'job': job})
            elif path == '/scan_fill_library':
                job = state.enqueue_scan_fill_library(force=bool(payload.get('force', False)))
                self._send({'ok': True, 'queued': True, 'job': job})
            elif path == '/scan_commercials':
                job = state.enqueue_scan_commercials(force=bool(payload.get('force', False)))
                self._send({'ok': True, 'queued': True, 'job': job})
            elif path == '/play_random':
                track = state.pick_random()
                self._send({'ok': True, 'track': track})
            elif path == '/play_full_schedule':
                # Start from block 0 and let the scheduler chain through all blocks in order
                with state.lock:
                    state._clear_active_segment()
                    state._set_pending_schedule_item(None)
                    state.resume_after_chime = None
                    state.current_segment_label = ''
                state._reset_commercial_state(clear_pending=True)
                track = state.play_schedule_block(0)
                state.log('Playing full schedule from block 1.')
                self._send({'ok': True, 'track': track})
            elif path == '/play_schedule_now':
                minute_of_day = time.localtime().tm_hour * 60 + time.localtime().tm_min
                item = state._current_schedule_item(minute_of_day)
                if item and item.get('type') == 'show':
                    track = state.play_schedule_block(int(item.get('index', 0)), item)
                    self._send({'ok': True, 'track': track, 'item': item})
                elif item and item.get('type') == 'fill':
                    track = state._start_fill_segment(item)
                    self._send({'ok': True, 'track': track, 'item': item})
                else:
                    track = state.play_schedule_block(0)
                    self._send({'ok': True, 'track': track, 'fallback': True})
            elif path == '/test_hour_chime':
                hour = int(payload.get('hour', time.localtime().tm_hour)) % 24
                self._send(state.trigger_hourly_chime(hour, test_only=True))
            elif path == '/test_commercial_break':
                count = int(payload.get('count', state._pick_spot_count()))
                with state.lock:
                    state._clear_active_segment()
                    state._set_pending_schedule_item(None)
                    state.resume_after_chime = None
                    state.current_segment_label = ''
                state._reset_commercial_state(clear_pending=True)
                state.player.stop()
                state._start_commercial_break(max(1, count))
                self._send({'ok': True, 'count': count, 'started': True})
            elif path == '/stop':
                temp_path = state.player.current_path
                temp_kind = state.player.current_kind
                with state.lock:
                    state._clear_active_segment()
                    state._set_pending_schedule_item(None)
                    state.resume_after_chime = None
                    state.current_segment_label = ''
                    # Stamp last_finished_signature with the track being stopped so
                    # the playback monitor won't treat this as a natural end-of-track
                    # and trigger a handoff to the next scheduled item.
                    if temp_kind or temp_path:
                        state.last_finished_signature = (temp_kind, temp_path)
                state._reset_commercial_state(clear_pending=True)
                state.player.stop()
                state._cleanup_temp_media(temp_path)
                state._clear_chime_active()
                self._send({'ok': True})
            elif path == '/shutdown':
                temp_path = state.player.current_path
                state._clear_active_segment()
                state._reset_commercial_state(clear_pending=True)
                state._clear_chime_active()
                if bool(payload.get('stop_playback', True)):
                    state.player.stop()
                state._cleanup_temp_media(temp_path)
                state._cleanup_all_temp_media()
                state.shutdown_event.set()
                self._send({'ok': True, 'shutting_down': True})

                def _shutdown_server():
                    time.sleep(0.2)
                    srv = server_ref.get('server')
                    if srv is not None:
                        try:
                            srv.shutdown()
                        except Exception:
                            pass

                threading.Thread(target=_shutdown_server, daemon=True).start()
            else:
                self._send({'ok': False, 'error': 'Not found'}, 404)
        except Exception as exc:
            state.log(f'Command failed for {path}: {exc}')
            self._send({'ok': False, 'error': str(exc)}, 400)

    def log_message(self, *args):
        return



def _find_open_port(host: str, preferred: int, attempts: int = 25) -> int:
    for port in range(preferred, preferred + attempts):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, port))
            return port
        except OSError:
            continue
        finally:
            try:
                s.close()
            except Exception:
                pass
    raise OSError(f'No open port found from {preferred} to {preferred + attempts - 1}')


if __name__ == '__main__':
    try:
        BACKEND_PID_FILE.write_text(str(os.getpid()))
    except Exception:
        pass
    bind_host = os.environ.get('RADIO_BIND_HOST', '0.0.0.0')
    bind_port = _find_open_port(bind_host, DEFAULT_PORT)
    server = ThreadingHTTPServer((bind_host, bind_port), Handler)
    server_ref['server'] = server
    try:
        PORT_FILE.write_text(str(bind_port))
    except Exception:
        pass
    print('=== RADIO BACKEND STARTING ===', flush=True)
    print(f'Radio backend listening on http://{bind_host}:{bind_port}', flush=True)
    print(f'Web UI available at http://{bind_host}:{bind_port}/', flush=True)
    try:
        server.serve_forever()
    finally:
        try:
            state.player.stop()
        except Exception:
            pass
        try:
            if PORT_FILE.exists() and PORT_FILE.read_text().strip() == str(bind_port):
                PORT_FILE.unlink()
        except Exception:
            pass
        server.server_close()
        try:
            BACKEND_PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
