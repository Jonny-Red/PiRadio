#!/usr/bin/env python3
"""
Pi Stream — Standalone audio streaming companion for Raspberry Pi.
Captures system audio output and streams it to Icecast via darkice.
Completely independent of Pi Radio.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

# ── Config file ───────────────────────────────────────────────────────────────
CONFIG_FILE = Path.home() / '.pi_stream_settings.json'
DARKICE_CFG = Path.home() / '.pi_stream_darkice.cfg'
DARKICE_LOG = Path.home() / '.pi_stream_darkice.log'

DEFAULT_CONFIG = {
    'icecast_host':     '127.0.0.1',
    'icecast_port':     8000,
    'icecast_mount':    'stream',
    'icecast_password': 'hackme',
    'bitrate':          128,
    'station_name':     'Pi Radio',
    'description':      'Pi Radio Station',
    'audio_source':     '',   # saved PulseAudio source name; '' = auto-detect
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            merged = dict(DEFAULT_CONFIG)
            merged.update(cfg)
            return merged
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ── darkice config writer ─────────────────────────────────────────────────────
def write_darkice_cfg(cfg: dict, device: str) -> None:
    content = f"""[general]
duration        = 0
bufferSecs      = 5
reconnect       = yes

[input]
device          = pulseaudio
sampleRate      = 44100
bitsPerSample   = 16
channel         = 2
paSourceName    = {device}

[icecast2-0]
bitrateMode     = cbr
format          = mp3
bitrate         = {cfg['bitrate']}
server          = {cfg['icecast_host']}
port            = {cfg['icecast_port']}
password        = {cfg['icecast_password']}
mountPoint      = {cfg['icecast_mount'].lstrip('/')}
name            = {cfg['station_name']}
description     = {cfg['description']}
"""
    DARKICE_CFG.write_text(content)


# ── PulseAudio monitor source detection ──────────────────────────────────────
def get_monitor_source() -> str | None:
    """Find the best monitor source — prefers RUNNING sources (active audio)."""
    try:
        result = subprocess.run(
            ['pactl', 'list', 'short', 'sources'],
            capture_output=True, text=True, timeout=5
        )
        running = []
        idle = []
        for line in result.stdout.splitlines():
            if 'monitor' not in line.lower():
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            name = parts[1]
            state = parts[-1].upper()
            if state == 'RUNNING':
                running.append(name)
            else:
                idle.append(name)
        candidates = running + idle
        return candidates[0] if candidates else None
    except Exception:
        pass
    return None


def get_all_sources() -> list[tuple[str, str]]:
    """Return list of (name, display_label) for ALL PulseAudio sources.

    Groups them as:
      ▶ ACTIVE  — sources currently receiving audio (RUNNING state)
      🔊 Output Monitor — loopback taps of audio output devices
      🎤 Input  — microphones, line-in, USB audio inputs
    Within each group, RUNNING sources sort first.
    """
    running: list[tuple[str, str]] = []
    monitors: list[tuple[str, str]] = []
    inputs: list[tuple[str, str]] = []

    try:
        # Build state map (name → RUNNING / IDLE / SUSPENDED …)
        state_map: dict[str, str] = {}
        short = subprocess.run(
            ['pactl', 'list', 'short', 'sources'],
            capture_output=True, text=True, timeout=5
        )
        for line in short.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                state_map[parts[1]] = parts[-1].upper()

        # Parse full source list for descriptions
        full = subprocess.run(
            ['pactl', 'list', 'sources'],
            capture_output=True, text=True, timeout=5
        )
        current_name: str | None = None
        for line in full.stdout.splitlines():
            line = line.strip()
            if line.startswith('Name:'):
                current_name = line.split(':', 1)[1].strip()
            elif line.startswith('Description:') and current_name:
                desc = line.split(':', 1)[1].strip()
                state = state_map.get(current_name, 'IDLE')
                is_monitor = 'monitor' in current_name.lower()
                is_active = state == 'RUNNING'

                if is_active:
                    kind_tag = '🔊 Monitor' if is_monitor else '🎤 Input'
                    label = f'▶ ACTIVE  {kind_tag} — {desc}'
                    running.append((current_name, label))
                elif is_monitor:
                    monitors.append((current_name, f'🔊 Output Monitor — {desc}'))
                else:
                    inputs.append((current_name, f'🎤 Input — {desc}'))
                current_name = None
    except Exception:
        pass

    return running + monitors + inputs


# Keep old name as alias so nothing else breaks
def get_all_monitor_sources() -> list[tuple[str, str]]:
    return get_all_sources()


# ── Icecast listener count ────────────────────────────────────────────────────
def get_listener_count(cfg: dict) -> int | None:
    try:
        mount = '/' + cfg['icecast_mount'].lstrip('/')
        url = f"http://{cfg['icecast_host']}:{cfg['icecast_port']}/status-json.xsl"
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.loads(r.read().decode('utf-8'))
        icestats = data.get('icestats', {})
        source = icestats.get('source')
        if source is None:
            return 0
        if isinstance(source, list):
            for s in source:
                if s.get('listenurl', '').endswith(mount):
                    return int(s.get('listeners', 0))
            return 0
        return int(source.get('listeners', 0))
    except Exception:
        return None


# ── darkice process management ────────────────────────────────────────────────
_darkice_proc: subprocess.Popen | None = None
_darkice_lock = threading.Lock()


def is_darkice_running() -> bool:
    with _darkice_lock:
        if _darkice_proc is None:
            return False
        return _darkice_proc.poll() is None


def start_darkice(cfg: dict, device: str) -> tuple[bool, str]:
    global _darkice_proc
    with _darkice_lock:
        if _darkice_proc and _darkice_proc.poll() is None:
            return False, 'Already running.'
        write_darkice_cfg(cfg, device)
        log_f = None
        try:
            log_f = open(DARKICE_LOG, 'w')
            _darkice_proc = subprocess.Popen(
                ['darkice', '-c', str(DARKICE_CFG)],
                stdout=log_f, stderr=log_f
            )
            time.sleep(2)
            if _darkice_proc.poll() is not None:
                log_f.close()
                log_f = None
                log = DARKICE_LOG.read_text(errors='replace')[-500:]
                return False, f'darkice exited immediately:\n{log}'
            # Leave log_f open so darkice can keep writing to it
            return True, ''
        except FileNotFoundError:
            if log_f:
                log_f.close()
            return False, 'darkice is not installed.\nRun: sudo apt install darkice'
        except Exception as exc:
            if log_f:
                log_f.close()
            return False, str(exc)


def stop_darkice() -> None:
    global _darkice_proc
    with _darkice_lock:
        if _darkice_proc:
            try:
                _darkice_proc.terminate()
                _darkice_proc.wait(timeout=5)
            except Exception:
                try:
                    _darkice_proc.kill()
                except Exception:
                    pass
            _darkice_proc = None


# ── Main GUI ──────────────────────────────────────────────────────────────────
class PiStreamApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title('Pi Stream')
        self.cfg = load_config()
        self._monitor_sources: list[tuple[str, str]] = []
        self._build_ui()
        self._refresh_sources()
        self._poll()
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

    def _build_ui(self):
        root = self.root
        root.configure(bg='#1a1a2e')

        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(root, bg='#16213e', pady=12)
        hdr.pack(fill='x')
        tk.Label(hdr, text='📻  Pi Stream', font=('Georgia', 20, 'bold'),
                 bg='#16213e', fg='#e94560').pack()
        tk.Label(hdr, text='System Audio → Icecast → Anywhere',
                 font=('Georgia', 9), bg='#16213e', fg='#a0a0c0').pack()

        # ── Status bar ────────────────────────────────────────────────────────
        status_frame = tk.Frame(root, bg='#0f3460', pady=8)
        status_frame.pack(fill='x')

        self.status_dot = tk.Label(status_frame, text='●', font=('courier', 16),
                                   bg='#0f3460', fg='#555577')
        self.status_dot.pack(side='left', padx=(14, 4))

        self.status_label = tk.Label(status_frame, text='Not streaming',
                                     font=('Georgia', 11, 'bold'),
                                     bg='#0f3460', fg='#a0a0c0')
        self.status_label.pack(side='left')

        self.listener_label = tk.Label(status_frame, text='',
                                       font=('Georgia', 9),
                                       bg='#0f3460', fg='#6080a0')
        self.listener_label.pack(side='right', padx=14)

        # ── Main content ──────────────────────────────────────────────────────
        content = tk.Frame(root, bg='#1a1a2e', padx=16, pady=12)
        content.pack(fill='both')

        # Audio source
        src_frame = tk.LabelFrame(content, text=' Audio Source ',
                                  bg='#1a1a2e', fg='#e94560',
                                  font=('Georgia', 9, 'bold'), padx=10, pady=8)
        src_frame.pack(fill='x', pady=(0, 10))

        self.source_var = tk.StringVar(value='auto')
        self.source_combo = ttk.Combobox(src_frame, textvariable=self.source_var,
                                         width=48, state='readonly')
        self.source_combo.pack(side='left')
        ttk.Button(src_frame, text='↺', width=3,
                   command=self._refresh_sources).pack(side='left', padx=(6, 0))

        # Icecast settings
        ice_frame = tk.LabelFrame(content, text=' Icecast Settings ',
                                  bg='#1a1a2e', fg='#e94560',
                                  font=('Georgia', 9, 'bold'), padx=10, pady=8)
        ice_frame.pack(fill='x', pady=(0, 10))
        ice_frame.columnconfigure(1, weight=1)
        ice_frame.columnconfigure(3, weight=1)

        def lbl(text, r, c):
            tk.Label(ice_frame, text=text, bg='#1a1a2e', fg='#a0a0c0',
                     font=('Georgia', 9)).grid(row=r, column=c, sticky='w', pady=3)

        def ent(var, r, c, w=18):
            e = tk.Entry(ice_frame, textvariable=var, width=w,
                         bg='#0f3460', fg='#e0e0ff', insertbackground='white',
                         relief='flat', font=('courier', 10))
            e.grid(row=r, column=c, sticky='ew', padx=(4, 12), pady=3)
            return e

        self.host_var     = tk.StringVar(value=self.cfg['icecast_host'])
        self.port_var     = tk.StringVar(value=str(self.cfg['icecast_port']))
        self.mount_var    = tk.StringVar(value=self.cfg['icecast_mount'])
        self.password_var = tk.StringVar(value=self.cfg['icecast_password'])
        self.bitrate_var  = tk.StringVar(value=str(self.cfg['bitrate']))
        self.name_var     = tk.StringVar(value=self.cfg['station_name'])

        lbl('Host',     0, 0); ent(self.host_var,     0, 1, 16)
        lbl('Port',     0, 2); ent(self.port_var,     0, 3, 6)
        lbl('Mount',    1, 0); ent(self.mount_var,    1, 1, 16)
        lbl('Password', 1, 2); ent(self.password_var, 1, 3, 10)
        lbl('Bitrate',  2, 0); ent(self.bitrate_var,  2, 1, 6)
        lbl('Station',  2, 2); ent(self.name_var,     2, 3, 16)

        # Stream URL display
        self.url_var = tk.StringVar(value='')
        url_frame = tk.Frame(content, bg='#1a1a2e')
        url_frame.pack(fill='x', pady=(0, 10))
        tk.Label(url_frame, text='Stream URL:', bg='#1a1a2e', fg='#606080',
                 font=('Georgia', 9)).pack(side='left')
        self.url_label = tk.Label(url_frame, textvariable=self.url_var,
                                  bg='#1a1a2e', fg='#4090c0',
                                  font=('courier', 9))
        self.url_label.pack(side='left', padx=(6, 0))

        # Buttons
        btn_frame = tk.Frame(content, bg='#1a1a2e')
        btn_frame.pack(fill='x', pady=(4, 0))

        self.start_btn = tk.Button(btn_frame, text='▶  Start Streaming',
                                   font=('Georgia', 11, 'bold'),
                                   bg='#e94560', fg='white', relief='flat',
                                   activebackground='#c73350', activeforeground='white',
                                   padx=18, pady=8, cursor='hand2',
                                   command=self._start)
        self.start_btn.pack(side='left')

        self.stop_btn = tk.Button(btn_frame, text='■  Stop',
                                  font=('Georgia', 11, 'bold'),
                                  bg='#333355', fg='#a0a0c0', relief='flat',
                                  activebackground='#444466', activeforeground='white',
                                  padx=18, pady=8, cursor='hand2',
                                  state='disabled', command=self._stop)
        self.stop_btn.pack(side='left', padx=(8, 0))

        tk.Button(btn_frame, text='Check Icecast',
                  font=('Georgia', 9),
                  bg='#1a1a2e', fg='#606080', relief='flat',
                  activebackground='#2a2a4e', activeforeground='#a0a0c0',
                  padx=10, pady=8, cursor='hand2',
                  command=self._check_icecast).pack(side='right')

        # Tailscale hint
        hint = tk.Frame(root, bg='#12122a', pady=8)
        hint.pack(fill='x')
        tk.Label(hint, text='🌐  Listeners connect via Tailscale:  http://<Pi-Tailscale-IP>:8000/stream',
                 font=('courier', 8), bg='#12122a', fg='#404060').pack()

        self._update_url()

    def _refresh_sources(self):
        sources = get_all_sources()
        self._monitor_sources = sources
        labels = ['Auto-detect (recommended)'] + [desc for _, desc in sources]
        self.source_combo['values'] = labels
        # Restore previously saved source, or default to auto
        saved = self.cfg.get('audio_source', '')
        if saved:
            for i, (name, _) in enumerate(sources):
                if name == saved:
                    self.source_combo.current(i + 1)
                    return
        self.source_combo.current(0)

    def _get_selected_device(self) -> str:
        idx = self.source_combo.current()
        if idx <= 0 or idx - 1 >= len(self._monitor_sources):
            # Auto-detect
            src = get_monitor_source()
            return src or 'default'
        return self._monitor_sources[idx - 1][0]

    def _collect_cfg(self) -> dict:
        cfg = dict(self.cfg)
        cfg['icecast_host']     = self.host_var.get().strip()
        try:
            cfg['icecast_port'] = int(self.port_var.get().strip())
        except Exception:
            cfg['icecast_port'] = 8000
        cfg['icecast_mount']    = self.mount_var.get().strip().lstrip('/')
        cfg['icecast_password'] = self.password_var.get().strip()
        cfg['station_name']     = self.name_var.get().strip()
        try:
            cfg['bitrate'] = int(self.bitrate_var.get().strip())
        except Exception:
            cfg['bitrate'] = 128
        # Save selected audio source
        idx = self.source_combo.current()
        if idx > 0 and idx - 1 < len(self._monitor_sources):
            cfg['audio_source'] = self._monitor_sources[idx - 1][0]
        else:
            cfg['audio_source'] = ''
        return cfg

    def _update_url(self):
        host  = self.host_var.get().strip()
        port  = self.port_var.get().strip()
        mount = self.mount_var.get().strip().lstrip('/')
        self.url_var.set(f'http://{host}:{port}/{mount}')

    def _start(self):
        self.cfg = self._collect_cfg()
        save_config(self.cfg)
        device = self._get_selected_device()
        self.status_label.config(text='Starting...', fg='#e0c040')
        self.status_dot.config(fg='#e0c040')
        self.start_btn.config(state='disabled')
        self.root.update()

        def _do_start():
            ok, err = start_darkice(self.cfg, device)
            # Schedule UI update back on main thread
            self.root.after(0, lambda: self._on_start_result(ok, err))

        threading.Thread(target=_do_start, daemon=True).start()

    def _on_start_result(self, ok: bool, err: str):
        if ok:
            self._set_streaming(True)
        else:
            self.start_btn.config(state='normal')
            self.status_label.config(text='Failed to start', fg='#e94560')
            self.status_dot.config(fg='#e94560')
            messagebox.showerror('Pi Stream — Error', err)

    def _stop(self):
        stop_darkice()
        self._set_streaming(False)

    def _set_streaming(self, streaming: bool):
        if streaming:
            self.status_dot.config(fg='#40e080')
            self.status_label.config(text='Streaming  ●  LIVE', fg='#40e080')
            self.start_btn.config(state='disabled')
            self.stop_btn.config(state='normal')
        else:
            self.status_dot.config(fg='#555577')
            self.status_label.config(text='Not streaming', fg='#a0a0c0')
            self.listener_label.config(text='')
            self.start_btn.config(state='normal')
            self.stop_btn.config(state='disabled')

    def _check_icecast(self):
        cfg = self._collect_cfg()
        url = f"http://{cfg['icecast_host']}:{cfg['icecast_port']}"
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                r.read()
            messagebox.showinfo('Icecast', f'Icecast is running at {url} ✓')
        except Exception:
            messagebox.showerror('Icecast', 
                f'Cannot reach Icecast at {url}\n\n'
                'Make sure Icecast is installed and running:\n'
                '  sudo apt install icecast2\n'
                '  sudo systemctl start icecast2')

    def _poll(self):
        """Periodic update — check darkice is still running, update listener count."""
        running = is_darkice_running()
        was_streaming = self.stop_btn['state'] == 'normal'

        if running:
            if not was_streaming:
                self._set_streaming(True)
            count = get_listener_count(self._collect_cfg())
            if count is not None:
                self.listener_label.config(
                    text=f'{count} listener{"s" if count != 1 else ""}')
        else:
            if was_streaming:
                # Was streaming but darkice died unexpectedly
                self._set_streaming(False)
                self.status_label.config(text='Stream stopped unexpectedly', fg='#e94560')

        self._update_url()
        self.root.after(5000, self._poll)

    def _on_close(self):
        if is_darkice_running():
            if messagebox.askyesno('Pi Stream',
                                   'Streaming is active. Stop streaming and quit?'):
                stop_darkice()
                self.root.destroy()
        else:
            self.root.destroy()


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    root = tk.Tk()
    root.configure(bg='#1a1a2e')
    app = PiStreamApp(root)
    # Measure content, then center on screen
    root.update_idletasks()
    w = root.winfo_reqwidth() + 20
    h = root.winfo_reqheight() + 20
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    x = (sw - w) // 2
    y = (sh - h) // 2
    root.geometry(f'{w}x{h}+{x}+{y}')
    root.resizable(True, True)
    root.minsize(w, h)
    root.lift()
    root.attributes('-topmost', True)
    root.after(200, lambda: root.attributes('-topmost', False))
    root.focus_force()
    root.mainloop()


if __name__ == '__main__':
    main()
