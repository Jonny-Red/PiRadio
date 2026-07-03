#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import queue
import socket
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.request import Request, urlopen
from urllib.error import URLError

from shared_radio import APP_TITLE, APP_VERSION, DEFAULT_HOST, DEFAULT_PORT, PORT_FILE, UI_PID_FILE, AppSettings

_cached_backend_port: int | None = None

def get_backend_port() -> int:
    global _cached_backend_port
    if _cached_backend_port is not None:
        return _cached_backend_port
    try:
        value = PORT_FILE.read_text().strip()
        if value:
            _cached_backend_port = int(value)
            return _cached_backend_port
    except Exception:
        pass
    return DEFAULT_PORT


def invalidate_backend_port_cache() -> None:
    global _cached_backend_port
    _cached_backend_port = None


def api_get(path: str):
    base = f'http://{DEFAULT_HOST}:{get_backend_port()}'
    with urlopen(base + path, timeout=5) as r:
        return json.loads(r.read().decode('utf-8'))


def api_post(path: str, payload: dict):
    base = f'http://{DEFAULT_HOST}:{get_backend_port()}'
    data = json.dumps(payload).encode('utf-8')
    req = Request(base + path, data=data, method='POST', headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode('utf-8'))


def backend_up() -> bool:
    port = get_backend_port()
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect((DEFAULT_HOST, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


class RadioUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f'{APP_TITLE} - Chime Folder Frontend ({APP_VERSION})')
        # Wide enough for the web-address line to fit on one row, but only
        # as tall as the actual content (labels + two button rows) — the
        # previous 560px height left a large empty band at the bottom.
        self.geometry('820x420')
        self.minsize(680, 380)
        self.settings = AppSettings.load()
        self.backend_online = False
        self.backend_version = 'unknown'
        self.discovered_paths = []
        self.logs_cache = []
        self.last_status = {}
        self.hourly_audio_paths = list(self.settings.hourly_audio_paths or [""] * 24)
        while len(self.hourly_audio_paths) < 24:
            self.hourly_audio_paths.append('')
        self.protocol('WM_DELETE_WINDOW', self.on_close)
        try:
            UI_PID_FILE.write_text(str(os.getpid()))
        except Exception:
            pass

        self.now_playing_var = tk.StringVar(value='Now Playing: (nothing)')
        self.summary_var = tk.StringVar(value='Starting…')
        self.scan_var = tk.StringVar(value='')
        self.web_address_var = tk.StringVar(value='Web interface: (starting…)')
        self.busy_var = tk.StringVar(value='Ready')

        self.request_queue: queue.Queue = queue.Queue()
        self.worker_lock = threading.Lock()
        self.active_requests = 0
        self._poll_job = None
        self.child_windows: dict[str, tk.Toplevel] = {}
        self._closing = False
        self._ui_ready = False
        self._startup_failures = 0
        self._auto_mounts_loaded = False
        self.splash = None
        self.splash_message_var = tk.StringVar(value='Loading…')

        self.withdraw()
        self._build_main()
        self._build_splash()
        self.after(120, self._drain_request_queue)
        self.refresh_status(first=True)

    def _build_main(self):
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill='both', expand=True)

        ttk.Label(outer, textvariable=self.now_playing_var, font=('TkDefaultFont', 12, 'bold')).pack(anchor='w')
        # The website address, front and center — this is the screen someone
        # sitting at the Pi sees, so the address to type on a phone lives here.
        ttk.Label(outer, textvariable=self.web_address_var, foreground='#1f4f8b',
                  font=('TkDefaultFont', 10, 'bold'), wraplength=760).pack(anchor='w', pady=(4, 0))
        ttk.Label(outer, textvariable=self.summary_var, wraplength=760).pack(anchor='w', pady=(4, 0))
        ttk.Label(outer, textvariable=self.scan_var, foreground='#555').pack(anchor='w', pady=(2, 10))
        ttk.Label(outer, textvariable=self.busy_var, foreground='#1f4f8b').pack(anchor='w', pady=(0, 8))

        main_actions = ttk.LabelFrame(outer, text='Playback', padding=10)
        main_actions.pack(fill='x')
        ttk.Button(main_actions, text='Play Random', command=self.play_random).grid(row=0, column=0, sticky='ew')
        ttk.Button(main_actions, text='Stop', command=self.stop_playback).grid(row=0, column=1, sticky='ew', padx=6)
        ttk.Button(main_actions, text='Refresh Status', command=self.refresh_status).grid(row=0, column=2, sticky='ew')
        for i in range(3):
            main_actions.columnconfigure(i, weight=1)

        tools = ttk.LabelFrame(outer, text='Editors', padding=10)
        tools.pack(fill='x', pady=(10, 0))
        ttk.Button(tools, text='Libraries', command=self.open_libraries).grid(row=0, column=0, sticky='ew')
        ttk.Button(tools, text='Scheduler', command=self.open_scheduler).grid(row=0, column=1, sticky='ew', padx=6)
        ttk.Button(tools, text='Hour Chimes', command=self.open_chimes).grid(row=0, column=2, sticky='ew')
        ttk.Button(tools, text='Monitor / Timeline', command=self.open_monitor).grid(row=1, column=0, sticky='ew', pady=(6, 0))
        ttk.Button(tools, text='Logs', command=self.open_logs).grid(row=1, column=1, sticky='ew', padx=6, pady=(6, 0))
        ttk.Button(tools, text='Quit', command=self.on_close).grid(row=1, column=2, sticky='ew', pady=(6, 0))
        for i in range(3):
            tools.columnconfigure(i, weight=1)

        self.paths_box = None

    def _build_splash(self):
        splash = tk.Toplevel()
        splash.title(APP_TITLE)
        splash.geometry('360x170')
        splash.resizable(False, False)
        splash.protocol('WM_DELETE_WINDOW', lambda: None)
        try:
            splash.attributes('-topmost', True)
        except Exception:
            pass

        frame = ttk.Frame(splash, padding=20)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text=APP_TITLE, font=('TkDefaultFont', 14, 'bold')).pack(pady=(6, 10))
        ttk.Label(frame, text='Loading…', font=('TkDefaultFont', 12)).pack()
        ttk.Label(frame, textvariable=self.splash_message_var, wraplength=300, justify='center').pack(pady=(12, 0))
        pb = ttk.Progressbar(frame, mode='indeterminate', length=240)
        pb.pack(pady=(16, 0))
        pb.start(10)
        self.splash = splash
        self.splash_message_var.set('Starting backend connection…')
        splash.deiconify()
        splash.lift()
        splash.update_idletasks()
        try:
            splash.focus_force()
        except Exception:
            pass
        x = max(0, (splash.winfo_screenwidth() - splash.winfo_width()) // 2)
        y = max(0, (splash.winfo_screenheight() - splash.winfo_height()) // 2)
        splash.geometry(f'+{x}+{y}')

    def _finish_loading(self):
        if self._ui_ready:
            return
        self._ui_ready = True
        if self.splash is not None:
            try:
                self.splash.destroy()
            except Exception:
                pass
            self.splash = None
        self.deiconify()
        self.lift()
        self.update_idletasks()
        try:
            self.focus_force()
        except Exception:
            pass

    def _set_busy(self, msg: str | None = None):
        if msg:
            self.busy_var.set(msg)
        else:
            self.busy_var.set('Working…' if self.active_requests else 'Ready')

    def _run_async(self, label: str, func, on_success=None, on_error=None, final_message: str | None = None, quiet: bool = False):
        if self._closing:
            return
        self.active_requests += 1
        # quiet=True: routine background work (the 5-second status poll) that
        # should NOT take over the busy line. Previously every poll wrote
        # "Refreshing status…" and the reset below only cleared labels
        # starting with "Working", so the line said "Refreshing status…"
        # forever. Real actions (Play, Save, scans) still show their labels.
        if not quiet:
            self._set_busy(label)

        def worker():
            try:
                result = func()
                self.request_queue.put(('success', on_success, result, final_message))
            except Exception as exc:
                self.request_queue.put(('error', on_error, exc, label))
            finally:
                self.request_queue.put(('done', None, None, None))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_request_queue(self):
        try:
            while True:
                kind, cb, payload, extra = self.request_queue.get_nowait()
                if kind == 'success':
                    if cb:
                        cb(payload)
                    if extra:
                        self._set_busy(extra)
                elif kind == 'error':
                    if cb:
                        cb(payload)
                    else:
                        self._set_busy(f'Error: {payload}')
                elif kind == 'done':
                    self.active_requests = max(0, self.active_requests - 1)
                    current = self.busy_var.get()
                    if self.active_requests == 0 and (
                            current.startswith('Working')
                            or current.startswith('Refreshing')
                            or current.startswith('Loading')):
                        self._set_busy('Ready')
        except queue.Empty:
            pass
        if not self._closing:
            self.after(120, self._drain_request_queue)

    def _show_error(self, title: str, exc: Exception):
        self._set_busy(f'{title} failed')
        messagebox.showerror(title, str(exc))

    def _open_singleton(self, key: str, title: str, geometry: str):
        existing = self.child_windows.get(key)
        if existing and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            return existing, False
        win = tk.Toplevel(self)
        win.title(title)
        try:
            win.wm_state('zoomed')
        except Exception:
            try:
                win.attributes('-zoomed', True)
            except Exception:
                win.geometry(geometry)
        self.child_windows[key] = win
        def _cleanup():
            self.child_windows.pop(key, None)
            try:
                win.destroy()
            except Exception:
                pass
        win.protocol('WM_DELETE_WINDOW', _cleanup)
        return win, True

    def _coerce_int(self, value, default=0, minimum=None, maximum=None):
        try:
            out = int(str(value).strip())
        except Exception:
            out = default
        if minimum is not None:
            out = max(minimum, out)
        if maximum is not None:
            out = min(maximum, out)
        return out

    def _coerce_float(self, value, default=0.0, minimum=None, maximum=None):
        try:
            out = float(str(value).strip())
        except Exception:
            out = default
        if minimum is not None:
            out = max(minimum, out)
        if maximum is not None:
            out = min(maximum, out)
        return out

    def open_libraries(self):
        win, created = self._open_singleton('libraries', 'Libraries', '860x560')
        if not created:
            return
        media_var = tk.StringVar(value=self.settings.media_folder)
        parent_var = tk.StringVar(value=self.settings.parent_library_folder)
        volume_var = tk.IntVar(value=self.settings.volume)
        fade_enabled_var = tk.BooleanVar(value=bool(self.settings.fade_enabled))
        fade_out_var = tk.StringVar(value=f'{float(self.settings.fade_out_seconds):g}')
        fade_in_var = tk.StringVar(value=f'{float(self.settings.fade_in_seconds):g}')

        frame = ttk.Frame(win, padding=12)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Main library folder').grid(row=0, column=0, sticky='w')
        ttk.Entry(frame, textvariable=media_var, width=70).grid(row=0, column=1, sticky='ew', padx=6)
        ttk.Button(frame, text='Browse', command=lambda: self._choose_into_with_mounts(media_var, 'Choose main library folder', parent=win)).grid(row=0, column=2)
        ttk.Label(frame, text='Show library parent folder').grid(row=1, column=0, sticky='w', pady=(8, 0))
        ttk.Entry(frame, textvariable=parent_var, width=70).grid(row=1, column=1, sticky='ew', padx=6, pady=(8, 0))
        ttk.Button(frame, text='Browse', command=lambda: self._choose_into_with_mounts(parent_var, 'Choose show library parent folder', parent=win)).grid(row=1, column=2, pady=(8, 0))
        ttk.Label(frame, text='Volume').grid(row=2, column=0, sticky='w', pady=(8, 0))
        ttk.Scale(frame, from_=0, to=100, orient='horizontal', variable=volume_var).grid(row=2, column=1, sticky='ew', padx=6, pady=(8, 0))

        playback = ttk.LabelFrame(frame, text='Playback / Audio', padding=8)
        playback.grid(row=3, column=0, columnspan=3, sticky='ew', pady=(12, 0))
        ttk.Checkbutton(playback, text='Enable fade transitions', variable=fade_enabled_var).grid(row=0, column=0, columnspan=2, sticky='w')
        ttk.Label(playback, text='Fade out seconds').grid(row=1, column=0, sticky='w', pady=(8, 0))
        ttk.Entry(playback, textvariable=fade_out_var, width=8).grid(row=1, column=1, sticky='w', padx=6, pady=(8, 0))
        ttk.Label(playback, text='Fade in seconds').grid(row=2, column=0, sticky='w', pady=(8, 0))
        ttk.Entry(playback, textvariable=fade_in_var, width=8).grid(row=2, column=1, sticky='w', padx=6, pady=(8, 0))
        ttk.Label(playback, text='Set either to 0 for instant transitions. Fades apply only to normal track changes.', foreground='#555').grid(row=3, column=0, columnspan=3, sticky='w', pady=(8, 0))
        playback.columnconfigure(2, weight=1)

        actions = ttk.Frame(frame)
        actions.grid(row=4, column=0, columnspan=3, sticky='ew', pady=(12, 0))
        ttk.Button(actions, text='Save Settings', command=lambda: self._save_library_settings(media_var.get(), parent_var.get(), volume_var.get(), fade_enabled_var.get(), fade_out_var.get(), fade_in_var.get())).pack(side='left')
        ttk.Button(actions, text='Scan Main Library', command=lambda: self.scan_library(False)).pack(side='left', padx=6)
        ttk.Button(actions, text='Force Main Rescan', command=lambda: self.scan_library(True)).pack(side='left')
        ttk.Button(actions, text='Scan Show Folders', command=lambda: self.scan_shows(False)).pack(side='left', padx=(18, 6))
        ttk.Button(actions, text='Force Show Rescan', command=lambda: self.scan_shows(True)).pack(side='left')

        mounts = ttk.LabelFrame(frame, text='USB / Mounted Drives', padding=8)
        mounts.grid(row=5, column=0, columnspan=3, sticky='nsew', pady=(12, 0))
        self.paths_box = tk.Listbox(mounts, height=10, exportselection=False)
        self.paths_box.pack(fill='both', expand=True)
        self.paths_box.bind('<Double-Button-1>', lambda _evt: self.use_selected_main())
        path_buttons = ttk.Frame(mounts)
        path_buttons.pack(fill='x', pady=(6, 0))
        ttk.Button(path_buttons, text='Find USB / Refresh', command=self.find_mounts).pack(side='left')
        ttk.Button(path_buttons, text='Use Selected as Main Library', command=self.use_selected_main).pack(side='left', padx=6)
        ttk.Button(path_buttons, text='Use Selected as Show Library', command=self.use_selected_show).pack(side='left', padx=6)
        ttk.Button(path_buttons, text='Browse Inside Selected Drive for Main', command=lambda: self._browse_inside_selected_drive(media_var, 'Choose main library folder inside selected drive', parent=win)).pack(side='left', padx=(18, 6))
        ttk.Button(path_buttons, text='Browse Inside Selected Drive for Show', command=lambda: self._browse_inside_selected_drive(parent_var, 'Choose show library parent folder inside selected drive', parent=win)).pack(side='left')

        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(5, weight=1)
        self.find_mounts(quiet=True)

    def open_scheduler(self):
        win, created = self._open_singleton('scheduler', 'Scheduler', '860x540')
        if not created:
            return
        start_hour_var = tk.StringVar(value=f'{int(self.settings.duration_start_hour):02d}')
        start_min_var = tk.StringVar(value=f'{int(self.settings.duration_start_minute):02d}')
        fill_mode_var = tk.StringVar(value=self.settings.schedule_fill_mode)
        fill_source_var = tk.StringVar(value=self.settings.fill_source_mode)
        include_subfolders_var = tk.BooleanVar(value=bool(self.settings.fill_include_subfolders))
        scheduler_enabled_var = tk.BooleanVar(value=bool(self.settings.scheduler_enabled))
        autoplay_on_start_var = tk.BooleanVar(value=bool(getattr(self.settings, 'autoplay_on_start', True)))
        commercials_enabled_var = tk.BooleanVar(value=bool(getattr(self.settings, 'commercials_enabled', False)))
        commercials_folder_var = tk.StringVar(value=getattr(self.settings, 'commercials_folder', ''))
        commercials_mode_var = tk.StringVar(value=getattr(self.settings, 'commercials_mode', 'random'))
        commercials_per_hour_var = tk.StringVar(value=str(int(getattr(self.settings, 'commercials_per_hour', 0) or 0)))
        commercials_per_break_var = tk.StringVar(value=str(int(getattr(self.settings, 'commercials_per_break', 1) or 1)))
        commercials_prefix_var = tk.StringVar(value=str(getattr(self.settings, 'commercials_prefix', 'o') or 'o'))
        commercials_between_shows_var = tk.BooleanVar(value=bool(getattr(self.settings, 'commercials_between_shows', False)))
        commercials_end_of_show_var = tk.BooleanVar(value=bool(getattr(self.settings, 'commercials_end_of_show', False)))
        commercials_end_of_track_var = tk.BooleanVar(value=bool(getattr(self.settings, 'commercials_end_of_track', False)))
        commercials_min_gap_var = tk.StringVar(value=str(int(getattr(self.settings, 'commercials_min_gap_minutes', 0) or 0)))
        commercials_min_runtime_var = tk.StringVar(value=str(int(getattr(self.settings, 'commercials_min_show_runtime_minutes', 0) or 0)))
        commercials_max_breaks_var = tk.StringVar(value=str(int(getattr(self.settings, 'commercials_max_breaks_per_show', 0) or 0)))
        commercials_spots_min_var = tk.StringVar(value=str(int(getattr(self.settings, 'commercials_spots_min', 0) or 0)))
        commercials_spots_max_var = tk.StringVar(value=str(int(getattr(self.settings, 'commercials_spots_max', 0) or 0)))
        _quiet_hours_set = set(list(getattr(self.settings, 'commercials_quiet_hours', []) or []))
        commercials_scheduled_only_var = tk.BooleanVar(value=bool(getattr(self.settings, 'commercials_scheduled_only', False)))
        blocks = [dict(b) for b in (self.settings.program_blocks or [])]
        fill_folders = list(self.settings.fill_folders or [])
        show_folders = []

        frame = ttk.Frame(win, padding=12)
        frame.pack(fill='both', expand=True)

        top = ttk.Frame(frame)
        top.pack(fill='x')
        _ready = [False]
        ttk.Checkbutton(top, text='Scheduler enabled', variable=scheduler_enabled_var).pack(side='left')
        ttk.Checkbutton(top, text='Autoplay on start', variable=autoplay_on_start_var).pack(side='left', padx=(10, 0))
        ttk.Label(top, text='Start time').pack(side='left', padx=(14, 4))
        hour_spin = ttk.Spinbox(top, from_=0, to=23, textvariable=start_hour_var, width=4, format='%02.0f')
        hour_spin.pack(side='left')
        ttk.Label(top, text=':').pack(side='left', padx=4)
        min_spin = ttk.Spinbox(top, from_=0, to=59, textvariable=start_min_var, width=4, format='%02.0f')
        min_spin.pack(side='left')
        start_hour_var.trace_add('write', lambda *_: refresh_blocks() if _ready[0] else None)
        start_min_var.trace_add('write', lambda *_: refresh_blocks() if _ready[0] else None)
        ttk.Label(top, text='Fill mode').pack(side='left', padx=(16, 4))
        ttk.Combobox(top, textvariable=fill_mode_var, values=['random', 'stop', 'loop'], state='readonly', width=12).pack(side='left')

        mid = ttk.Panedwindow(frame, orient='horizontal')
        mid.pack(fill='both', expand=True, pady=(10, 0))
        left = ttk.Frame(mid)
        right = ttk.Frame(mid)
        mid.add(left, weight=1)
        mid.add(right, weight=1)

        lf = ttk.LabelFrame(left, text='Show folders', padding=8)
        lf.pack(fill='both', expand=True)
        shows_list = tk.Listbox(lf, exportselection=False)
        shows_list.pack(fill='both', expand=True)
        hours_var = tk.StringVar(value='1.0')
        row = ttk.Frame(lf)
        row.pack(fill='x', pady=(6, 0))
        ttk.Label(row, text='Hours').pack(side='left')
        ttk.Entry(row, textvariable=hours_var, width=8).pack(side='left', padx=6)
        ttk.Button(row, text='Refresh Shows', command=lambda: load_show_folders()).pack(side='left')
        ttk.Button(row, text='Add Selected Show', command=lambda: add_block()).pack(side='left', padx=6)

        rf = ttk.LabelFrame(right, text='Schedule blocks', padding=8)
        rf.pack(fill='both', expand=True)
        blocks_list = tk.Listbox(rf, exportselection=False)
        blocks_list.pack(fill='both', expand=True)
        total_hours_var = tk.StringVar(value='')
        ttk.Label(rf, textvariable=total_hours_var, foreground='#555').pack(anchor='w', pady=(2, 0))
        brow = ttk.Frame(rf)
        brow.pack(fill='x', pady=(6, 0))
        ttk.Button(brow, text='Remove', command=lambda: remove_block()).pack(side='left')
        ttk.Button(brow, text='Clear', command=lambda: clear_blocks()).pack(side='left', padx=6)
        ttk.Button(brow, text='Move Up', command=lambda: move_block(-1)).pack(side='left', padx=(18, 0))
        ttk.Button(brow, text='Move Down', command=lambda: move_block(1)).pack(side='left', padx=6)
        ttk.Button(brow, text='Play Schedule Now', command=self.play_schedule_now).pack(side='left', padx=(18, 0))
        ttk.Button(brow, text='Play Full Schedule', command=self.play_full_schedule).pack(side='left', padx=(6, 0))

        # ── Scrollable lower section (fill + commercials + save) ─────────────
        scroll_outer = ttk.Frame(frame)
        scroll_outer.pack(fill='both', expand=False, pady=(10, 0))

        _canvas = tk.Canvas(scroll_outer, highlightthickness=0)
        _scrollbar = ttk.Scrollbar(scroll_outer, orient='vertical', command=_canvas.yview)
        _canvas.configure(yscrollcommand=_scrollbar.set)
        _scrollbar.pack(side='right', fill='y')
        _canvas.pack(side='left', fill='both', expand=True)

        scroll_inner = ttk.Frame(_canvas)
        _canvas_window = _canvas.create_window((0, 0), window=scroll_inner, anchor='nw')

        def _on_inner_configure(evt):
            _canvas.configure(scrollregion=_canvas.bbox('all'))
            # Cap canvas height so it never pushes below the window
            _canvas.configure(height=min(scroll_inner.winfo_reqheight(), 340))

        def _on_canvas_configure(evt):
            _canvas.itemconfig(_canvas_window, width=evt.width)

        scroll_inner.bind('<Configure>', _on_inner_configure)
        _canvas.bind('<Configure>', _on_canvas_configure)

        def _on_mousewheel(evt):
            _canvas.yview_scroll(int(-1 * (evt.delta / 120)), 'units')

        def _on_mousewheel_linux(evt):
            if evt.num == 4:
                _canvas.yview_scroll(-1, 'units')
            elif evt.num == 5:
                _canvas.yview_scroll(1, 'units')

        _canvas.bind('<MouseWheel>', _on_mousewheel)
        _canvas.bind('<Button-4>', _on_mousewheel_linux)
        _canvas.bind('<Button-5>', _on_mousewheel_linux)
        scroll_inner.bind('<MouseWheel>', _on_mousewheel)
        scroll_inner.bind('<Button-4>', _on_mousewheel_linux)
        scroll_inner.bind('<Button-5>', _on_mousewheel_linux)

        ff = ttk.LabelFrame(scroll_inner, text='Random fill folders', padding=8)
        ff.pack(fill='x', expand=False)
        ftop = ttk.Frame(ff)
        ftop.pack(fill='x')
        ttk.Label(ftop, text='Fill source').pack(side='left')
        ttk.Combobox(ftop, textvariable=fill_source_var, values=['main_library', 'selected_folders'], state='readonly', width=18).pack(side='left', padx=(6, 12))
        ttk.Checkbutton(ftop, text='Include subfolders', variable=include_subfolders_var).pack(side='left')
        fill_list = tk.Listbox(ff, height=4, exportselection=False)
        fill_list.pack(fill='both', expand=True, pady=(8, 0))
        fbtn = ttk.Frame(ff)
        fbtn.pack(fill='x', pady=(6, 0))
        ttk.Button(fbtn, text='Add Fill Folder', command=lambda: add_fill_folder()).pack(side='left')
        ttk.Button(fbtn, text='Remove Selected', command=lambda: remove_fill_folder()).pack(side='left', padx=6)
        ttk.Button(fbtn, text='Clear', command=lambda: clear_fill_folders()).pack(side='left')
        ttk.Button(fbtn, text='Scan Fill Folders', command=lambda: self.scan_fill(False)).pack(side='left', padx=(18, 6))
        ttk.Button(fbtn, text='Force Fill Rescan', command=lambda: self.scan_fill(True)).pack(side='left')

        cf = ttk.LabelFrame(scroll_inner, text='Commercial Spots', padding=10)
        cf.pack(fill='x', expand=False, pady=(10, 0))

        # ── Row 1: Enable + folder ────────────────────────────────────────────
        crow1 = ttk.Frame(cf)
        crow1.pack(fill='x')
        ttk.Checkbutton(crow1, text='Enable commercials', variable=commercials_enabled_var).pack(side='left')
        ttk.Label(crow1, text='Folder:').pack(side='left', padx=(20, 4))
        ttk.Entry(crow1, textvariable=commercials_folder_var, width=46).pack(side='left', fill='x', expand=True, padx=(0, 6))
        ttk.Button(crow1, text='Browse', command=lambda: choose_commercial_folder()).pack(side='left')
        ttk.Button(crow1, text='Browse USB', command=lambda: self._browse_inside_selected_drive(commercials_folder_var, 'Choose commercial folder on USB drive', parent=win)).pack(side='left', padx=(6, 0))

        # ── Row 2: WHEN triggers ──────────────────────────────────────────────
        when_frame = ttk.LabelFrame(cf, text='When to play a break', padding=8)
        when_frame.pack(fill='x', pady=(10, 0))

        # Trigger checkboxes
        trig_row = ttk.Frame(when_frame)
        trig_row.pack(fill='x')
        ttk.Checkbutton(trig_row, text='Between shows',
                        variable=commercials_between_shows_var).pack(side='left')
        ttk.Checkbutton(trig_row, text='End of every show block',
                        variable=commercials_end_of_show_var).pack(side='left', padx=(18, 0))
        ttk.Checkbutton(trig_row, text='After every track',
                        variable=commercials_end_of_track_var).pack(side='left', padx=(18, 0))

        # Scheduled only option
        trig_row2 = ttk.Frame(when_frame)
        trig_row2.pack(fill='x', pady=(6, 0))
        ttk.Checkbutton(trig_row2, text='During scheduled shows only  (never fire during random fill or overnight)',
                        variable=commercials_scheduled_only_var).pack(side='left')

        # Per-hour timed breaks row
        ph_row = ttk.Frame(when_frame)
        ph_row.pack(fill='x', pady=(8, 0))
        ttk.Label(ph_row, text='Timed breaks per hour:').pack(side='left')
        ttk.Entry(ph_row, textvariable=commercials_per_hour_var, width=5).pack(side='left', padx=(6, 0))
        ttk.Label(ph_row, text='(0 = off  —  waits for current track to finish before firing)',
                  foreground='#555').pack(side='left', padx=(10, 0))

        # ── Row 3: GUARD RAILS ────────────────────────────────────────────────
        guard_frame = ttk.LabelFrame(cf, text='Guard rails  (0 = no limit)', padding=8)
        guard_frame.pack(fill='x', pady=(10, 0))

        gr1 = ttk.Frame(guard_frame)
        gr1.pack(fill='x')
        ttk.Label(gr1, text='Min gap between breaks:').pack(side='left')
        ttk.Entry(gr1, textvariable=commercials_min_gap_var, width=5).pack(side='left', padx=(6, 0))
        ttk.Label(gr1, text='min', foreground='#555').pack(side='left', padx=(4, 24))
        ttk.Label(gr1, text='Min show runtime before first break:').pack(side='left')
        ttk.Entry(gr1, textvariable=commercials_min_runtime_var, width=5).pack(side='left', padx=(6, 0))
        ttk.Label(gr1, text='min', foreground='#555').pack(side='left', padx=(4, 0))

        gr2 = ttk.Frame(guard_frame)
        gr2.pack(fill='x', pady=(8, 0))
        ttk.Label(gr2, text='Max breaks per show block:').pack(side='left')
        ttk.Entry(gr2, textvariable=commercials_max_breaks_var, width=5).pack(side='left', padx=(6, 0))
        ttk.Label(gr2, text='(0 = no cap)', foreground='#555').pack(side='left', padx=(4, 0))

        # ── Row 4: HOW MANY spots ─────────────────────────────────────────────
        spots_frame = ttk.LabelFrame(cf, text='Spots per break', padding=8)
        spots_frame.pack(fill='x', pady=(10, 0))

        sp1 = ttk.Frame(spots_frame)
        sp1.pack(fill='x')
        ttk.Label(sp1, text='Fixed spots:').pack(side='left')
        ttk.Entry(sp1, textvariable=commercials_per_break_var, width=5).pack(side='left', padx=(6, 0))
        ttk.Label(sp1, text='  —  OR random range:').pack(side='left', padx=(12, 0))
        ttk.Label(sp1, text='Min').pack(side='left', padx=(8, 4))
        ttk.Entry(sp1, textvariable=commercials_spots_min_var, width=5).pack(side='left')
        ttk.Label(sp1, text='Max').pack(side='left', padx=(8, 4))
        ttk.Entry(sp1, textvariable=commercials_spots_max_var, width=5).pack(side='left')
        ttk.Label(sp1, text='(set both > 0 to override fixed)', foreground='#555').pack(side='left', padx=(10, 0))

        # ── Row 5: QUIET HOURS grid ───────────────────────────────────────────
        qh_frame = ttk.LabelFrame(cf, text='Quiet hours  (checked = NO commercials that hour)', padding=8)
        qh_frame.pack(fill='x', pady=(10, 0))

        _qh_vars: list[tk.BooleanVar] = []
        for h in range(24):
            v = tk.BooleanVar(value=(h in _quiet_hours_set))
            _qh_vars.append(v)
            col = h % 12
            row_n = h // 12
            lbl = f'{h:02d}'
            cb = ttk.Checkbutton(qh_frame, text=lbl, variable=v)
            cb.grid(row=row_n, column=col, sticky='w', padx=4, pady=2)

        # ── Row 6: Order + prefix + scan/test buttons ─────────────────────────
        crow_order = ttk.Frame(cf)
        crow_order.pack(fill='x', pady=(10, 0))
        ttk.Label(crow_order, text='Playback order:').pack(side='left')
        ttk.Combobox(crow_order, textvariable=commercials_mode_var,
                     values=['random', 'ordered_label'], state='readonly', width=16).pack(side='left', padx=(4, 20))
        ttk.Label(crow_order, text='Order prefix:').pack(side='left')
        ttk.Entry(crow_order, textvariable=commercials_prefix_var, width=6).pack(side='left', padx=(4, 0))

        crow_tip = ttk.Frame(cf)
        crow_tip.pack(fill='x', pady=(4, 0))
        ttk.Label(crow_tip, text='Tip: for ordered playback name files like o1-ad.mp3, o2-ad.mp3 using the prefix above.',
                  foreground='#555').pack(side='left')

        crow_btns = ttk.Frame(cf)
        crow_btns.pack(fill='x', pady=(8, 0))
        ttk.Button(crow_btns, text='Scan Commercial Folder', command=lambda: self.scan_commercials(False)).pack(side='left')
        ttk.Button(crow_btns, text='Force Commercial Rescan', command=lambda: self.scan_commercials(True)).pack(side='left', padx=6)
        ttk.Button(crow_btns, text='Test Commercial Break', command=lambda: test_commercial_break()).pack(side='left', padx=(18, 0))

        bottom = ttk.Frame(scroll_inner)
        bottom.pack(fill='x', pady=(10, 6))
        ttk.Button(bottom, text='Save Scheduler Settings', command=lambda: save_all()).pack(side='left')

        def load_show_folders():
            def work():
                try:
                    data = api_get('/show_folders')
                    return list(data.get('show_folders', []))
                except Exception:
                    data = api_get('/status')
                    return list(data.get('status', {}).get('show_folders', []))
            def success(show_paths):
                nonlocal show_folders
                show_folders = list(show_paths)
                shows_list.delete(0, 'end')
                for p in show_folders:
                    shows_list.insert('end', Path(p).name or p)
            self._run_async('Loading show folders…', work, success, lambda e: self._show_error('Show Folders', e), 'Show folders loaded')

        def refresh_blocks():
            blocks_list.delete(0, 'end')
            start_mins = self._coerce_int(start_hour_var.get(), 0, 0, 23) * 60 + self._coerce_int(start_min_var.get(), 0, 0, 59)
            cursor = start_mins
            total = 0.0
            for idx, b in enumerate(blocks):
                hours = self._coerce_float(b.get('hours', 1.0), 1.0, 0.0)
                end = (cursor + int(round(hours * 60))) % (24 * 60)
                label = b.get('label') or Path(b.get('folder', '')).name or f'Block {idx+1}'
                blocks_list.insert('end', f"{cursor//60:02d}:{cursor%60:02d}  {label}  ({hours:g}h)  \u2192  {end//60:02d}:{end%60:02d}")
                cursor = end
                total += hours
            if blocks:
                remaining = max(0.0, 24.0 - total)
                total_hours_var.set(f"Total: {total:g}h scheduled  |  {remaining:g}h fill")
            else:
                total_hours_var.set('')

        def move_block(direction: int):
            sel = blocks_list.curselection()
            if not sel:
                return
            idx = sel[0]
            new_idx = idx + direction
            if new_idx < 0 or new_idx >= len(blocks):
                return
            blocks[idx], blocks[new_idx] = blocks[new_idx], blocks[idx]
            refresh_blocks()
            blocks_list.selection_clear(0, 'end')
            blocks_list.selection_set(new_idx)
            blocks_list.see(new_idx)

        _ready[0] = True

        def refresh_fill():
            fill_list.delete(0, 'end')
            for p in fill_folders:
                fill_list.insert('end', p)

        def add_block():
            sel = shows_list.curselection()
            if not sel:
                return
            folder = show_folders[sel[0]]
            try:
                hours = self._coerce_float(hours_var.get(), 1.0, 0.01)
            except Exception:
                hours = 1.0
            blocks.append({'folder': folder, 'hours': hours, 'label': Path(folder).name})
            refresh_blocks()

        def remove_block():
            sel = blocks_list.curselection()
            if not sel:
                return
            blocks.pop(sel[0])
            refresh_blocks()

        def clear_blocks():
            blocks.clear()
            refresh_blocks()

        def add_fill_folder():
            p = filedialog.askdirectory(title='Choose fill folder', initialdir=self.settings.media_folder or str(Path.home()), parent=win)
            if p and p not in fill_folders:
                fill_folders.append(p)
                refresh_fill()

        def remove_fill_folder():
            sel = fill_list.curselection()
            if not sel:
                return
            fill_folders.pop(sel[0])
            refresh_fill()

        def clear_fill_folders():
            fill_folders.clear()
            refresh_fill()

        def choose_commercial_folder():
            start_dir = (commercials_folder_var.get().strip()
                         or self._selected_mount_path()
                         or self.settings.media_folder
                         or str(Path.home()))
            p = filedialog.askdirectory(title='Choose commercial folder', initialdir=start_dir, parent=win)
            if p:
                commercials_folder_var.set(p)

        def test_commercial_break():
            try:
                count = max(1, int(commercials_per_break_var.get() or 1))
            except Exception:
                count = 1

            def after_save():
                self._run_async('Testing commercial break…',
                                lambda: api_post('/test_commercial_break', {'count': count}),
                                lambda _res: self.refresh_status(),
                                lambda e: self._show_error('Commercial Break', e),
                                'Commercial break started')

            save_all(notify=False, after=after_save)

        def save_all(notify=True, after=None):
            start_hour = self._coerce_int(start_hour_var.get(), 0, 0, 23)
            start_minute = self._coerce_int(start_min_var.get(), 0, 0, 59)
            commercials_per_hour = self._coerce_int(commercials_per_hour_var.get(), 0, 0, 60)
            commercials_per_break = self._coerce_int(commercials_per_break_var.get(), 1, 1, 20)
            sanitized_blocks = []
            total_hours = 0.0
            for b in blocks:
                hours = self._coerce_float(b.get('hours', 0), 0.0, 0.0)
                folder = str(b.get('folder', '')).strip()
                if not folder or hours <= 0:
                    continue
                total_hours += hours
                sanitized_blocks.append({'folder': folder, 'hours': hours, 'label': b.get('label') or Path(folder).name})
            if total_hours > 24.0:
                messagebox.showerror('Invalid Schedule', 'The total scheduled show hours are over 24. Please reduce the block lengths before saving.')
                return
            self.settings.scheduler_enabled = bool(scheduler_enabled_var.get())
            self.settings.autoplay_on_start = bool(autoplay_on_start_var.get())
            self.settings.duration_start_hour = start_hour
            self.settings.duration_start_minute = start_minute
            self.settings.schedule_fill_mode = fill_mode_var.get()
            self.settings.fill_source_mode = fill_source_var.get()
            self.settings.fill_include_subfolders = bool(include_subfolders_var.get())
            self.settings.program_blocks = sanitized_blocks
            self.settings.fill_folders = list(fill_folders)
            self.settings.commercials_enabled = bool(commercials_enabled_var.get())
            self.settings.commercials_folder = commercials_folder_var.get().strip()
            self.settings.commercials_mode = commercials_mode_var.get()
            self.settings.commercials_per_hour = commercials_per_hour
            self.settings.commercials_per_break = commercials_per_break
            self.settings.commercials_prefix = commercials_prefix_var.get().strip() or 'o'
            self.settings.commercials_between_shows = bool(commercials_between_shows_var.get())
            self.settings.commercials_end_of_show = bool(commercials_end_of_show_var.get())
            self.settings.commercials_end_of_track = bool(commercials_end_of_track_var.get())
            self.settings.commercials_min_gap_minutes = self._coerce_int(commercials_min_gap_var.get(), 0, 0, 120)
            self.settings.commercials_min_show_runtime_minutes = self._coerce_int(commercials_min_runtime_var.get(), 0, 0, 120)
            self.settings.commercials_max_breaks_per_show = self._coerce_int(commercials_max_breaks_var.get(), 0, 0, 20)
            self.settings.commercials_spots_min = self._coerce_int(commercials_spots_min_var.get(), 0, 0, 20)
            self.settings.commercials_spots_max = self._coerce_int(commercials_spots_max_var.get(), 0, 0, 20)
            self.settings.commercials_scheduled_only = bool(commercials_scheduled_only_var.get())
            self.settings.commercials_quiet_hours = sorted([h for h, v in enumerate(_qh_vars) if v.get()])

            def after_success():
                if notify:
                    messagebox.showinfo('Saved', 'Scheduler settings saved to backend.')
                if after:
                    after()

            self.save_settings_to_backend(after_success)

        refresh_blocks()
        refresh_fill()
        load_show_folders()
        # Silently sync current settings to backend on open so they're always active
        save_all(notify=False)


    def open_chimes(self):
        win, created = self._open_singleton('chimes', 'Hour Chimes', '860x560')
        if not created:
            return
        hourly_enabled_var = tk.BooleanVar(value=bool(self.settings.hourly_chimes_enabled))
        chime_mode_var = tk.StringVar(value=self.settings.chime_mode)
        interrupt_hourly_var = tk.BooleanVar(value=bool(getattr(self.settings, 'interrupt_hourly', False)))
        chimes_folder_var = tk.StringVar(value=getattr(self.settings, 'chimes_folder', ''))
        hourly_paths = list(self.hourly_audio_paths)
        while len(hourly_paths) < 24:
            hourly_paths.append('')
        selected_hour = tk.StringVar(value='00')
        selected_path_var = tk.StringVar(value=hourly_paths[0])

        frame = ttk.Frame(win, padding=12)
        frame.pack(fill='both', expand=True)

        top = ttk.Frame(frame)
        top.pack(fill='x')
        ttk.Checkbutton(top, text='Enable hourly chimes', variable=hourly_enabled_var).pack(side='left')
        ttk.Label(top, text='Mode').pack(side='left', padx=(12, 4))
        ttk.Combobox(top, textvariable=chime_mode_var, values=['repeat_strike', 'audio_drop'], state='readonly', width=16).pack(side='left')
        ttk.Checkbutton(top, text='Interrupt instead of pause/resume', variable=interrupt_hourly_var).pack(side='left', padx=(12, 0))

        folder_frame = ttk.LabelFrame(frame, text='Chimes Folder', padding=8)
        folder_frame.pack(fill='x', pady=(10, 0))
        ttk.Entry(folder_frame, textvariable=chimes_folder_var, width=70).pack(side='left', fill='x', expand=True)
        ttk.Button(folder_frame, text='Browse', command=lambda: choose_chimes_folder()).pack(side='left', padx=6)
        ttk.Button(folder_frame, text='Browse from USB Drive', command=lambda: self._browse_inside_selected_drive(chimes_folder_var, 'Choose chimes folder inside selected drive', parent=win)).pack(side='left')

        mid = ttk.Frame(frame)
        mid.pack(fill='both', expand=True, pady=(10, 0))
        hours_box_frame = ttk.LabelFrame(mid, text='Hours', padding=8)
        hours_box_frame.pack(side='left', fill='y')
        hours_list = tk.Listbox(hours_box_frame, exportselection=False, width=18, height=18)
        hours_list.pack(fill='y', expand=True)
        right = ttk.Frame(mid)
        right.pack(side='left', fill='both', expand=True, padx=(10, 0))

        ttk.Label(right, text='Selected hour file').pack(anchor='w')
        ttk.Entry(right, textvariable=selected_path_var, width=90).pack(fill='x')
        pathbtns = ttk.Frame(right)
        pathbtns.pack(fill='x', pady=(6, 0))
        ttk.Button(pathbtns, text='Add Chime', command=lambda: choose_hour_path()).pack(side='left')
        ttk.Button(pathbtns, text='Add Chime from USB', command=lambda: choose_hour_path(from_usb=True)).pack(side='left', padx=6)
        ttk.Button(pathbtns, text='Clear Chime', command=lambda: clear_hour_path()).pack(side='left', padx=6)
        ttk.Button(pathbtns, text='Save This Hour', command=lambda: save_hour_path()).pack(side='left', padx=6)
        ttk.Button(pathbtns, text='Test Selected Hour Chime', command=lambda: test_hour()).pack(side='left', padx=(12, 0))

        assign_summary_var = tk.StringVar(value='')
        ttk.Label(right, textvariable=assign_summary_var, foreground='#555').pack(anchor='w', pady=(8, 0))
        hours_map = tk.Text(right, height=18)
        hours_map.pack(fill='both', expand=True, pady=(6, 0))

        btm = ttk.Frame(frame)
        btm.pack(fill='x', pady=(10, 0))
        ttk.Button(btm, text='Save Chime Settings', command=lambda: save_all()).pack(side='left')

        def choose_chimes_folder():
            start_dir = chimes_folder_var.get().strip() or str(Path.home())
            p = filedialog.askdirectory(title='Choose chimes folder', initialdir=start_dir, parent=win)
            if p:
                chimes_folder_var.set(p)

        def refresh_hours_list():
            hours_list.delete(0, 'end')
            assigned = 0
            for h in range(24):
                p = hourly_paths[h].strip()
                label = Path(p).name if p else '(not set)'
                if p:
                    assigned += 1
                prefix = '✔' if p else '•'
                hours_list.insert('end', f'{prefix} {h:02d}:00  {label}')
            assign_summary_var.set(f'Assigned: {assigned} / 24 hours')

        def refresh_map():
            hours_map.delete('1.0', 'end')
            for h in range(24):
                p = hourly_paths[h].strip()
                label = Path(p).name if p else '(not set)'
                hours_map.insert('end', f'{h:02d}:00  {label}\n')

        def on_hour_select(evt=None):
            sel = hours_list.curselection()
            if not sel:
                return
            h = sel[0]
            selected_hour.set(f'{h:02d}')
            selected_path_var.set(hourly_paths[h])

        def choose_hour_path(from_usb: bool = False):
            if from_usb:
                usb = self._selected_mount_path()
                if not usb:
                    from tkinter import messagebox as _mb
                    _mb.showinfo('USB / Mounted Drives', 'No USB drive selected. Open the Libraries tab and click "Find USB / Refresh" first, then come back and try again.')
                    return
                initial_dir = usb
            else:
                initial_dir = chimes_folder_var.get().strip() or str(Path.home())
            p = filedialog.askopenfilename(
                title=f'Choose chime for {selected_hour.get()}:00',
                initialdir=initial_dir,
                parent=win
            )
            if p:
                selected_path_var.set(p)
                save_hour_path()

        def clear_hour_path():
            h = int(selected_hour.get())
            hourly_paths[h] = ''
            selected_path_var.set('')
            refresh_hours_list()
            refresh_map()

        def save_hour_path():
            h = int(selected_hour.get())
            hourly_paths[h] = selected_path_var.get().strip()
            refresh_hours_list()
            refresh_map()
            hours_list.selection_clear(0, 'end')
            hours_list.selection_set(h)
            hours_list.see(h)

        def save_all(after=None):
            save_hour_path()
            self.settings.hourly_chimes_enabled = bool(hourly_enabled_var.get())
            self.settings.chime_mode = chime_mode_var.get()
            self.settings.chimes_folder = chimes_folder_var.get().strip()
            self.settings.interrupt_hourly = bool(interrupt_hourly_var.get())
            self.settings.hourly_audio_paths = list(hourly_paths)
            self.hourly_audio_paths = list(hourly_paths)
            self.save_settings_to_backend(after)

        def test_hour():
            save_hour_path()
            self.settings.hourly_audio_paths = list(hourly_paths)
            self.settings.hourly_chimes_enabled = bool(hourly_enabled_var.get())
            self.settings.chime_mode = chime_mode_var.get()
            self.settings.chimes_folder = chimes_folder_var.get().strip()
            def work():
                self.settings.normalize()
                api_post('/save_settings', self.settings.to_dict())
                return api_post('/test_hour_chime', {'hour': int(selected_hour.get())})
            def success(res):
                self.settings.save()
                if res.get('ok'):
                    messagebox.showinfo('Hour Chime', 'Test chime triggered. It should pause, play the chime, then resume the same file.')
                else:
                    messagebox.showerror('Hour Chime', res.get('error', 'Test failed'))
                self.refresh_status()
            self._run_async('Testing hour chime…', work, success, lambda e: self._show_error('Hour Chime', e), 'Hour chime test complete')

        hours_list.bind('<<ListboxSelect>>', on_hour_select)
        refresh_hours_list()
        refresh_map()
        hours_list.selection_set(0)
        on_hour_select()


    def open_monitor(self):
        win, created = self._open_singleton('monitor', 'Monitor / Timeline', '860x600')
        if not created:
            return

        frame = ttk.Frame(win, padding=12)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=0)
        frame.rowconfigure(2, weight=1)

        # ── helpers ──────────────────────────────────────────────────────────
        def fmt_ms(ms):
            s = max(0, int(ms // 1000))
            h, r = divmod(s, 3600)
            m, s = divmod(r, 60)
            return f'{h:02d}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'

        def fmt_uptime(secs):
            h, r = divmod(int(secs), 3600)
            return f'{h}h {r//60}m' if h else f'{r//60}m'

        # ── Now Playing ───────────────────────────────────────────────────────
        np_frame = ttk.LabelFrame(frame, text='Now Playing', padding=8)
        np_frame.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 8))
        np_frame.columnconfigure(0, weight=1)

        v_title = tk.StringVar(value='(nothing playing)')
        v_time  = tk.StringVar(value='00:00 / 00:00')
        v_rem   = tk.StringVar(value='')

        np_top = ttk.Frame(np_frame)
        np_top.pack(fill='x')
        np_top.columnconfigure(0, weight=1)
        ttk.Label(np_top, textvariable=v_title, font=('TkDefaultFont', 11, 'bold')).grid(row=0, column=0, sticky='w')
        ttk.Label(np_top, textvariable=v_time).grid(row=0, column=1, sticky='e', padx=(12, 0))
        ttk.Label(np_top, textvariable=v_rem, foreground='#8a4b00').grid(row=0, column=2, sticky='e', padx=(8, 0))

        v_prog = tk.DoubleVar(value=0.0)
        prog_bar = ttk.Progressbar(np_frame, variable=v_prog, maximum=100.0, length=400)
        prog_bar.pack(fill='x', pady=(6, 0))

        # ── Info row ──────────────────────────────────────────────────────────
        info_frame = ttk.Frame(frame)
        info_frame.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(0, 8))

        v_curblk  = tk.StringVar(value='—')
        v_nxtblk  = tk.StringVar(value='—')
        v_status  = tk.StringVar(value='—')
        v_pending = tk.StringVar(value='')

        ttk.Label(info_frame, text='Block:', foreground='#555').grid(row=0, column=0, sticky='w')
        ttk.Label(info_frame, textvariable=v_curblk, font=('TkDefaultFont', 10, 'bold')).grid(row=0, column=1, sticky='w', padx=(4, 20))
        ttk.Label(info_frame, text='Next:', foreground='#555').grid(row=0, column=2, sticky='w')
        ttk.Label(info_frame, textvariable=v_nxtblk).grid(row=0, column=3, sticky='w', padx=(4, 20))
        ttk.Label(info_frame, text='Status:', foreground='#555').grid(row=0, column=4, sticky='w')
        ttk.Label(info_frame, textvariable=v_status, font=('TkDefaultFont', 10, 'bold')).grid(row=0, column=5, sticky='w', padx=(4, 20))
        ttk.Label(info_frame, textvariable=v_pending, foreground='#8a4b00').grid(row=0, column=6, sticky='w')

        # ── Body: schedule (left) + status panels (right) ────────────────────
        body = ttk.Frame(frame)
        body.grid(row=2, column=0, columnspan=2, sticky='nsew')
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=0)
        body.rowconfigure(0, weight=1)

        # Schedule list
        sched_frame = ttk.LabelFrame(body, text='Schedule', padding=6)
        sched_frame.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        sched_frame.rowconfigure(0, weight=1)
        sched_frame.columnconfigure(0, weight=1)

        sched_box = tk.Listbox(sched_frame, width=38, activestyle='none',
                               selectbackground='#cce4ff', selectforeground='#000',
                               font=('TkFixedFont', 10))
        sched_scroll = ttk.Scrollbar(sched_frame, orient='vertical', command=sched_box.yview)
        sched_box.configure(yscrollcommand=sched_scroll.set)
        sched_box.grid(row=0, column=0, sticky='nsew')
        sched_scroll.grid(row=0, column=1, sticky='ns')

        # Right-side status panels
        right = ttk.Frame(body, width=240)
        right.grid(row=0, column=1, sticky='ns')
        right.pack_propagate(False)

        comm_frame = ttk.LabelFrame(right, text='Commercials', padding=8)
        comm_frame.pack(fill='x', pady=(0, 8))

        def stat_row(parent, label, row):
            ttk.Label(parent, text=label, foreground='#555').grid(row=row, column=0, sticky='w', pady=1)
            var = tk.StringVar(value='—')
            ttk.Label(parent, textvariable=var).grid(row=row, column=1, sticky='w', padx=(8, 0), pady=1)
            return var

        v_cstat = stat_row(comm_frame, 'Status',     0)
        v_cnext = stat_row(comm_frame, 'Next break', 1)
        v_cphr  = stat_row(comm_frame, 'Spots/hr',   2)
        v_clib  = stat_row(comm_frame, 'Library',    3)

        chime_frame = ttk.LabelFrame(right, text='Chimes', padding=8)
        chime_frame.pack(fill='x', pady=(0, 8))
        v_hen  = stat_row(chime_frame, 'Enabled',    0)
        v_hmod = stat_row(chime_frame, 'Mode',       1)
        v_hint = stat_row(chime_frame, 'Interrupt',  2)
        v_hnxt = stat_row(chime_frame, 'Next chime', 3)

        health_frame = ttk.LabelFrame(right, text='Health', padding=8)
        health_frame.pack(fill='x', pady=(0, 8))
        v_vlc  = stat_row(health_frame, 'VLC',     0)
        v_upt  = stat_row(health_frame, 'Uptime',  1)
        v_lib2 = stat_row(health_frame, 'Library', 2)
        v_ver  = stat_row(health_frame, 'Version', 3)

        # ── Bottom bar ────────────────────────────────────────────────────────
        bot = ttk.Frame(frame)
        bot.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(8, 0))
        ttk.Button(bot, text='■  Stop',        command=self.stop_playback).pack(side='left')
        ttk.Button(bot, text='▶  Play Random', command=self.play_random).pack(side='left', padx=6)
        ttk.Button(bot, text='⟳  Refresh',     command=self.refresh_status).pack(side='left')
        v_stale = tk.StringVar(value='Connecting…')
        ttk.Label(bot, textvariable=v_stale, foreground='#888').pack(side='right')
        v_clock = tk.StringVar(value='')
        ttk.Label(bot, textvariable=v_clock).pack(side='right', padx=(0, 12))

        # ── Schedule list builder ─────────────────────────────────────────────
        _prev_key = {'v': None}

        def build_sched(sched, cur, pend):
            import time as _t
            sched_box.delete(0, 'end')
            now_hm = _t.strftime('%H:%M')
            active_idx = None
            for idx, item in enumerate(sched):
                lbl   = item.get('label', '')
                start = item.get('start_time', '??:??')
                dur   = item.get('hours', 0)
                actv  = lbl == cur
                pndg  = bool(pend and lbl == pend)
                try:
                    past = not actv and start < now_hm
                except Exception:
                    past = False
                marker = '▶' if actv else ('⏭' if pndg else ' ')
                dur_str = f'{dur}h' if dur else '  '
                line = f' {marker} {start}  {lbl:<28} {dur_str}'
                sched_box.insert('end', line)
                if past:
                    sched_box.itemconfig(idx, foreground='#aaa')
                elif actv:
                    sched_box.itemconfig(idx, foreground='#1a56cc', selectbackground='#cce4ff')
                    active_idx = idx
                elif pndg:
                    sched_box.itemconfig(idx, foreground='#8a4b00')
            if active_idx is not None:
                sched_box.see(max(0, active_idx - 1))

        # ── Background fetch (every 2 s) ──────────────────────────────────────
        _st      = {'v': {}}
        _fetch_t = {'v': 0.0}

        def do_fetch():
            import time as _t
            if not win.winfo_exists():
                return
            def work():
                try:
                    return api_get('/status')
                except Exception:
                    return {}
            def done(raw):
                import time as _t
                if not win.winfo_exists():
                    return
                data = raw.get('status', {}) if isinstance(raw, dict) else {}
                if data:
                    _st['v'] = data
                    self.last_status = dict(data)
                    _fetch_t['v'] = _t.time()
                if win.winfo_exists():
                    win.after(2000, do_fetch)
            # Run the HTTP request on the background thread, then marshal only
            # the result handling back to the Tk thread. (Previously the thread
            # scheduled `done(work())` via after(0), which made the blocking
            # network call run ON the UI thread every 2s — freezing the whole
            # app for up to 5s per poll whenever the backend was slow or down.)
            import threading as _thr
            def _bg():
                result = work()
                try:
                    if win.winfo_exists():
                        win.after(0, lambda: done(result))
                except Exception:
                    pass
            _thr.Thread(target=_bg, daemon=True).start()

        do_fetch()

        # ── Render loop (every 1 s) ───────────────────────────────────────────
        def render():
            import time as _t
            if not win.winfo_exists():
                return
            try:
                st = dict(_st['v'] or self.last_status or {})

                path   = st.get('now_playing') or ''
                isplay = bool(st.get('is_playing'))
                kind   = st.get('play_kind') or ''
                cur_ms = int(st.get('current_time_ms') or 0)
                tot_ms = int(st.get('total_length_ms') or 0)
                rem_ms = max(0, tot_ms - cur_ms)

                v_title.set(Path(path).name if path else '(nothing playing)')
                v_time.set(f'{fmt_ms(cur_ms)} / {fmt_ms(tot_ms)}')
                v_rem.set(f'  –{fmt_ms(rem_ms)}' if tot_ms > 0 else '')
                v_prog.set(round(100.0 * cur_ms / tot_ms, 2) if tot_ms > 0 else 0.0)

                sched = st.get('computed_schedule') or []
                cur   = st.get('current_segment_label') or '—'
                pend  = st.get('pending_schedule_label') or ''
                nxt   = pend
                if not nxt:
                    act = st.get('active_segment') or {}
                    for i, item in enumerate(sched):
                        if (item.get('label') == act.get('label') and
                                item.get('start_minutes') == act.get('start_minutes')):
                            if i + 1 < len(sched):
                                nxt = sched[i+1].get('label', '')
                            break

                v_curblk.set(cur)
                v_nxtblk.set(nxt or '—')
                v_pending.set(f'  ⏭ pending: {pend}' if pend else '')

                if kind == 'commercial':
                    v_status.set('Commercial')
                elif kind.startswith('hour_chime') or kind.startswith('chime_'):
                    v_status.set('Chime')
                elif isplay:
                    v_status.set('Playing')
                else:
                    v_status.set('Stopped')

                key = (tuple((i.get('label'), i.get('start_time')) for i in sched), cur, pend)
                if sched and key != _prev_key['v']:
                    _prev_key['v'] = key
                    build_sched(sched, cur, pend)

                cen  = bool(st.get('commercials_enabled'))
                crem = int(st.get('commercial_break_remaining') or 0)
                cpnd = int(st.get('pending_commercial_break') or 0)
                if not cen:
                    v_cstat.set('Off')
                elif crem > 0:
                    v_cstat.set(f'On-air ({crem} left)')
                elif cpnd > 0:
                    v_cstat.set('Queued')
                else:
                    v_cstat.set('Ready')
                phr = int(st.get('commercials_per_hour') or 0)
                cbetween = bool(st.get('commercials_between_shows'))
                if cen and phr > 0:
                    ivl = max(1, round(60/phr))
                    v_cnext.set(f'~{ivl - int(_t.strftime("%M")) % ivl} min')
                elif cen and cbetween:
                    v_cnext.set('At show end')
                else:
                    v_cnext.set('—')
                v_cphr.set(str(phr) if cen else '—')
                v_clib.set(f"{st.get('commercial_count', 0):,} files")

                hen = bool(st.get('hourly_chimes_enabled'))
                v_hen.set('On' if hen else 'Off')
                md = st.get('chime_mode') or ''
                v_hmod.set('Strike' if md == 'repeat_strike' else 'Audio drop' if md == 'audio_drop' else md or '—')
                v_hint.set('Interrupt' if st.get('interrupt_hourly') else 'Pause/resume')
                v_hnxt.set(f'{(int(_t.strftime("%H"))+1)%24:02d}:00' if hen else '—')

                vlc = bool(st.get('vlc_available'))
                v_vlc.set('OK' if vlc else 'Missing')
                v_upt.set(fmt_uptime(int(st.get('uptime_seconds') or 0)))
                v_lib2.set(f"{st.get('library_count', 0):,}")
                v_ver.set(st.get('api_version') or '—')

                v_clock.set(_t.strftime('%H:%M:%S'))
                ft  = _fetch_t['v']
                age = int(_t.time() - ft) if ft else 999
                v_stale.set(f'Updated {age}s ago' if age < 60 else f'Updated {age//60}m {age%60}s ago')

            except Exception as err:
                v_stale.set(f'Error: {err}')

            win.after(1000, render)

        render()

    def open_logs(self):
        win, created = self._open_singleton('logs', 'Logs', '880x460')
        if not created:
            return
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill='both', expand=True)
        text = tk.Text(frame)
        text.pack(fill='both', expand=True)
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=(8, 0))
        ttk.Button(row, text='Refresh Logs', command=lambda: refresh()).pack(side='left')

        def refresh():
            def work():
                return api_get('/logs_tail').get('logs', [])
            def success(logs):
                self.logs_cache = list(logs)
                text.delete('1.0', 'end')
                text.insert('1.0', '\n'.join(self.logs_cache[-200:]))
            self._run_async('Loading logs…', work, success, lambda e: self._show_error('Logs', e), 'Logs refreshed')

        text.insert('1.0', '\n'.join(self.logs_cache[-200:]))

    def _choose_into(self, var: tk.StringVar, parent=None):
        p = filedialog.askdirectory(title='Choose folder', initialdir=var.get() or str(Path.home()), parent=parent or self)
        if p:
            var.set(p)

    def _selected_mount_path(self) -> str:
        if self.paths_box is not None and self.paths_box.winfo_exists():
            sel = self.paths_box.curselection()
            if sel and 0 <= sel[0] < len(self.discovered_paths):
                return str(self.discovered_paths[sel[0]]).strip()
        for path in self.discovered_paths:
            s = str(path).strip()
            if s:
                return s
        return ''

    def _choose_into_with_mounts(self, var: tk.StringVar, title: str = 'Choose folder', parent=None):
        initial = str(var.get() or '').strip()
        if not initial:
            initial = self._selected_mount_path() or str(Path.home())
        p = filedialog.askdirectory(title=title, initialdir=initial, parent=parent or self)
        if p:
            var.set(p)

    def _browse_inside_selected_drive(self, var: tk.StringVar, title: str = 'Choose folder inside selected drive', parent=None):
        initial = self._selected_mount_path()
        if not initial:
            messagebox.showinfo('USB / Mounted Drives', 'Select a USB or mounted drive first, then browse inside it.')
            return
        p = filedialog.askdirectory(title=title, initialdir=initial, parent=parent or self)
        if p:
            var.set(p)

    def _save_library_settings(self, media_folder: str, parent_folder: str, volume: int, fade_enabled: bool = False, fade_out_seconds: str | float = 3.0, fade_in_seconds: str | float = 2.0):
        self.settings.media_folder = media_folder.strip()
        self.settings.parent_library_folder = parent_folder.strip()
        self.settings.volume = int(volume)
        self.settings.fade_enabled = bool(fade_enabled)
        try:
            self.settings.fade_out_seconds = float(fade_out_seconds)
        except Exception:
            self.settings.fade_out_seconds = 3.0
        try:
            self.settings.fade_in_seconds = float(fade_in_seconds)
        except Exception:
            self.settings.fade_in_seconds = 2.0
        self.settings.normalize()
        self.save_settings_to_backend(lambda: self.summary_var.set('Library settings saved.'))

    def save_settings_to_backend(self, after_success=None):
        self.settings.normalize()
        payload = self.settings.to_dict()
        def work():
            return api_post('/save_settings', payload)
        def success(_res):
            self.settings.save()
            if after_success:
                after_success()
            self.refresh_status()
        self._run_async('Saving settings…', work, success, lambda e: self._show_error('Save Settings', e), 'Settings saved')

    def scan_library(self, force: bool):
        self._run_async('Queueing main library scan…', lambda: api_post('/scan_library', {'force': bool(force)}),
                        lambda _res: self.scan_var.set('Main library scan queued.'),
                        lambda e: self._show_error('Scan Library', e), 'Main library scan queued')

    def scan_shows(self, force: bool):
        self._run_async('Queueing show scan…', lambda: api_post('/scan_show_folders', {'force': bool(force)}),
                        lambda _res: self.scan_var.set('Show folder scan queued.'),
                        lambda e: self._show_error('Scan Show Folders', e), 'Show folder scan queued')

    def scan_fill(self, force: bool):
        self._run_async('Queueing fill scan…', lambda: api_post('/scan_fill_library', {'force': bool(force)}),
                        lambda _res: self.scan_var.set('Fill folder scan queued.'),
                        lambda e: self._show_error('Scan Fill Library', e), 'Fill folder scan queued')

    def scan_commercials(self, force: bool):
        self._run_async('Queueing commercial scan…', lambda: api_post('/scan_commercials', {'force': bool(force)}),
                        lambda _res: self.scan_var.set('Commercial scan queued.'),
                        lambda e: self._show_error('Scan Commercials', e), 'Commercial scan queued')

    def play_random(self):
        self._run_async('Starting random playback…', lambda: api_post('/play_random', {}),
                        lambda _res: self.refresh_status(), lambda e: self._show_error('Play Random', e), 'Random playback started')

    def play_schedule_now(self):
        self._run_async('Starting current schedule block…', lambda: api_post('/play_schedule_now', {}),
                        lambda _res: self.refresh_status(), lambda e: self._show_error('Schedule', e), 'Current schedule block started')

    def play_full_schedule(self):
        self._run_async('Starting full schedule from block 1…', lambda: api_post('/play_full_schedule', {}),
                        lambda _res: self.refresh_status(), lambda e: self._show_error('Schedule', e), 'Full schedule started from block 1')

    def stop_playback(self):
        self._run_async('Stopping playback…', lambda: api_post('/stop', {}),
                        lambda _res: self.refresh_status(), lambda e: self._show_error('Stop', e), 'Playback stopped')

    def find_mounts(self, quiet: bool = False):
        def success(data):
            mounts = list(data.get('mounts', []))
            fallback_paths = list(data.get('paths', []))
            rows = []
            seen = set()
            for item in mounts:
                path = str(item.get('path', '')).strip()
                if not path or path in seen:
                    continue
                seen.add(path)
                label = str(item.get('label', '')).strip()
                source = str(item.get('source', '')).strip()
                size = str(item.get('size', '')).strip()
                parts = [label or Path(path).name or path]
                if size:
                    parts.append(size)
                if source:
                    parts.append(source)
                rows.append({'path': path, 'display': ' | '.join(parts)})
            for path in fallback_paths:
                path = str(path).strip()
                if not path or path in seen:
                    continue
                seen.add(path)
                rows.append({'path': path, 'display': path})
            self.discovered_paths = [row['path'] for row in rows]
            if self.paths_box is not None and self.paths_box.winfo_exists():
                self.paths_box.delete(0, 'end')
                for row in rows:
                    self.paths_box.insert('end', row['display'])
            count = len(rows)
            if count:
                self.scan_var.set(f'USB / mount paths found: {count}')
            else:
                self.scan_var.set('No USB / mounted drives found')
            if not quiet:
                self.summary_var.set(f'Detected {count} USB / mounted path(s).')
        def error(exc):
            if quiet:
                self.scan_var.set('USB detection failed')
                return
            self._show_error('Find Mounts', exc)
        self._run_async('Searching USB / mounted drives…', lambda: api_get('/discover_paths'), success, error, 'USB search complete')

    def use_selected_main(self):
        if self.paths_box is None or not self.paths_box.winfo_exists():
            self.open_libraries()
            return
        sel = self.paths_box.curselection()
        if not sel:
            return
        self.settings.media_folder = self.discovered_paths[sel[0]]
        self.save_settings_to_backend(lambda: self.summary_var.set(f'Main library set to {self.settings.media_folder}'))

    def use_selected_show(self):
        sel = self.paths_box.curselection()
        if not sel:
            return
        self.settings.parent_library_folder = self.discovered_paths[sel[0]]
        self.save_settings_to_backend(lambda: self.summary_var.set(f'Show library set to {self.settings.parent_library_folder}'))

    def refresh_status(self, first: bool = False):
        if self._closing:
            return
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except Exception:
                pass
            self._poll_job = None

        def success(data):
            st = data.get('status', {})
            self._startup_failures = 0
            self.last_status = dict(st)
            self.backend_online = True
            self.backend_version = st.get('api_version') or st.get('app_version') or 'unknown'
            self.logs_cache = list(st.get('logs', []))
            now = st.get('now_playing') or '(nothing)'
            self.now_playing_var.set(f'Now Playing: {now}')
            player_state = 'playing' if st.get('is_playing') else 'idle'
            hourly = f"hourly {st.get('hourly_chimes_enabled')} ({st.get('chime_mode')}, {'interrupt' if st.get('interrupt_hourly') else 'pause/resume'})"
            fill_summary = f"fill {st.get('schedule_fill_mode')} via {st.get('fill_source_mode')}"
            commercial_summary = f"commercials {'on' if st.get('commercials_enabled') else 'off'} ({st.get('commercials_per_hour',0)}/hr)"
            vlc_summary = 'VLC ok' if st.get('vlc_available', True) else 'VLC missing'
            health = st.get('health', {}) or {}
            health_summary = f"hb w:{health.get('worker_age_seconds', '?')} s:{health.get('scheduler_age_seconds', '?')} p:{health.get('playback_age_seconds', '?')}"
            self.summary_var.set(f"Backend connected ({self.backend_version}) on {get_backend_port()} | {player_state} | {vlc_summary} | up {st.get('uptime_seconds',0)}s | {hourly} | {fill_summary} | {commercial_summary}")
            scan_parts = []
            if st.get('scan_library_job'):
                scan_parts.append('main scan running')
            if st.get('scan_show_job'):
                scan_parts.append('show scan running')
            if st.get('scan_fill_job'):
                scan_parts.append('fill scan running')
            if st.get('scan_commercial_job'):
                scan_parts.append('commercial scan running')
            if not scan_parts:
                scan_parts.append(f"library {st.get('library_count',0)} | shows {st.get('show_folder_count',0)} | fill {st.get('fill_library_count',0)} | commercials {st.get('commercial_count',0)}")
            if st.get('commercial_break_remaining', 0):
                scan_parts.append(f"break remaining {st.get('commercial_break_remaining', 0)}")
            if st.get('pending_commercial_break', 0):
                scan_parts.append(f"pending break {st.get('pending_commercial_break', 0)}")
            scan_parts.append(health_summary)
            self.scan_var.set(' | '.join(scan_parts))
            addrs = st.get('web_addresses') or []
            if addrs:
                self.web_address_var.set('Web interface (open on any phone/PC):  ' + '   |   '.join(addrs))
            else:
                self.web_address_var.set(f'Web interface: http://<this-pi>:{get_backend_port()}')
            self.splash_message_var.set(f'Connected to backend {self.backend_version}. Finalizing UI…')
            self._finish_loading()
            if not self._auto_mounts_loaded:
                self._auto_mounts_loaded = True
                self.find_mounts(quiet=True)
            self._poll_job = self.after(5000, self.refresh_status)

        def error(exc):
            invalidate_backend_port_cache()
            self.backend_online = False
            self.last_status = {}
            self._startup_failures += 1
            self.now_playing_var.set('Now Playing: backend offline')
            self.summary_var.set(f'Backend offline on {get_backend_port()}: {exc}')
            self.scan_var.set('')
            if not self._ui_ready and self._startup_failures >= 8:
                self.splash_message_var.set('Backend did not come online. Opening UI in offline mode…')
                self._finish_loading()
            else:
                self.splash_message_var.set(f'Loading backend on port {get_backend_port()}… (attempt {self._startup_failures})')
            self._poll_job = self.after(1000 if first else 5000, self.refresh_status)

        label = 'Refreshing status…' if self._ui_ready else 'Loading backend…'
        self._run_async(label, lambda: api_get('/status'), success, error, quiet=self._ui_ready)

    def on_close(self):
        if self._closing:
            return
        self._closing = True
        try:
            UI_PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        for child in list(self.child_windows.values()):
            try:
                child.destroy()
            except Exception:
                pass
        self.child_windows.clear()
        try:
            self.busy_var.set('Shutting down backend…')
            self.update_idletasks()
        except Exception:
            pass
        try:
            api_post('/shutdown', {'stop_playback': True})
        except Exception:
            pass
        self.destroy()



def main():
    app = RadioUI()
    app.mainloop()


if __name__ == '__main__':
    main()
