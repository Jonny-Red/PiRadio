#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List
import json
import os

APP_TITLE = "Pi Radio Station Player"
APP_VERSION = 'v41.32'  # shown in the web UI badge — confirms which build the page is talking to
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
SUPPORTED_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}
SETTINGS_FILE = Path.home() / ".pi_radio_split_settings.json"
LIBRARY_CACHE_FILE = Path.home() / ".pi_radio_library_index.json"
SHOW_CACHE_FILE = Path.home() / ".pi_radio_show_index.json"
FILL_CACHE_FILE = Path.home() / ".pi_radio_fill_index.json"
COMMERCIAL_CACHE_FILE = Path.home() / ".pi_radio_commercial_index.json"
LOG_FILE = Path.home() / ".pi_radio_backend.log"
PORT_FILE = Path.home() / '.pi_radio_backend_port'
BACKEND_PID_FILE = Path.home() / '.pi_radio_backend.pid'
UI_PID_FILE = Path.home() / '.pi_radio_ui.pid'


def _default_hourly_paths() -> List[str]:
    return [""] * 24


def _default_fill_folders() -> List[str]:
    return []


@dataclass
class ProgramBlock:
    folder: str
    hours: float
    label: str = ""


@dataclass
class AppSettings:
    media_folder: str = ""
    parent_library_folder: str = ""
    custom_network_path: str = ""
    volume: int = 80
    fade_enabled: bool = False
    fade_out_seconds: float = 3.0
    fade_in_seconds: float = 2.0
    auto_resume_random: bool = True
    scheduler_enabled: bool = True
    autoplay_on_start: bool = True  # if False, skip the startup catch-up auto-play; scheduler still runs going forward
    duration_start_hour: int = 1
    duration_start_minute: int = 0
    program_blocks: List[dict] = field(default_factory=list)
    schedule_fill_mode: str = "random"
    fill_source_mode: str = "main_library"
    fill_folders: List[str] = field(default_factory=_default_fill_folders)
    fill_include_subfolders: bool = True
    commercials_enabled: bool = False
    commercials_folder: str = ""
    commercials_mode: str = "random"
    commercials_per_hour: int = 0
    commercials_per_break: int = 1
    commercials_prefix: str = "o"
    commercials_between_shows: bool = False
    # Commercial Break Rules
    commercials_end_of_show: bool = False        # fire a break at the end of every show block
    commercials_end_of_track: bool = False       # fire a break after every single track
    commercials_min_gap_minutes: int = 0         # minimum minutes between any two breaks (0 = no limit)
    commercials_min_show_runtime_minutes: int = 0  # don't break within first N minutes of a show (0 = no limit)
    commercials_max_breaks_per_show: int = 0     # max breaks per show block (0 = no limit)
    commercials_spots_min: int = 0               # random spots: minimum (0 = use fixed per_break)
    commercials_spots_max: int = 0               # random spots: maximum (0 = use fixed per_break)
    commercials_quiet_hours: List[int] = field(default_factory=list)  # hours (0-23) where commercials are suppressed
    commercials_scheduled_only: bool = False  # only fire commercials during scheduled show blocks, never during fill
    hourly_chimes_enabled: bool = False
    chime_mode: str = "repeat_strike"
    interrupt_hourly: bool = False
    chimes_folder: str = ""
    hourly_audio_paths: List[str] = field(default_factory=_default_hourly_paths)

    def normalize(self) -> None:
        self.duration_start_hour = int(self.duration_start_hour) % 24
        self.duration_start_minute = int(self.duration_start_minute) % 60
        self.volume = max(0, min(100, int(self.volume)))
        self.fade_enabled = bool(self.fade_enabled)
        try:
            self.fade_out_seconds = float(self.fade_out_seconds)
        except Exception:
            self.fade_out_seconds = 3.0
        try:
            self.fade_in_seconds = float(self.fade_in_seconds)
        except Exception:
            self.fade_in_seconds = 2.0
        self.fade_out_seconds = max(0.0, min(10.0, self.fade_out_seconds))
        self.fade_in_seconds = max(0.0, min(10.0, self.fade_in_seconds))
        self.autoplay_on_start = bool(self.autoplay_on_start)
        if not isinstance(self.program_blocks, list):
            self.program_blocks = []
        hap = list(self.hourly_audio_paths or [])
        while len(hap) < 24:
            hap.append("")
        self.hourly_audio_paths = hap[:24]
        if self.chime_mode not in {"repeat_strike", "audio_drop"}:
            self.chime_mode = "repeat_strike"
        self.chimes_folder = str(self.chimes_folder or "").strip()
        if self.schedule_fill_mode not in {"random", "stop", "loop"}:
            self.schedule_fill_mode = "random"
        if self.fill_source_mode not in {"main_library", "selected_folders"}:
            self.fill_source_mode = "main_library"
        if not isinstance(self.fill_folders, list):
            self.fill_folders = []
        clean_fill = []
        for item in self.fill_folders:
            s = str(item or "").strip()
            if s and s not in clean_fill:
                clean_fill.append(s)
        self.fill_folders = clean_fill
        self.fill_include_subfolders = bool(self.fill_include_subfolders)
        self.commercials_enabled = bool(self.commercials_enabled)
        self.commercials_folder = str(self.commercials_folder or "").strip()
        if self.commercials_mode not in {"random", "ordered_label"}:
            self.commercials_mode = "random"
        try:
            self.commercials_per_hour = int(self.commercials_per_hour)
        except Exception:
            self.commercials_per_hour = 0
        try:
            self.commercials_per_break = int(self.commercials_per_break)
        except Exception:
            self.commercials_per_break = 1
        self.commercials_per_hour = max(0, min(60, self.commercials_per_hour))
        self.commercials_per_break = max(1, min(20, self.commercials_per_break))
        self.commercials_prefix = str(self.commercials_prefix or "o").strip() or "o"
        self.commercials_between_shows = bool(self.commercials_between_shows)
        self.commercials_end_of_show = bool(self.commercials_end_of_show)
        self.commercials_end_of_track = bool(self.commercials_end_of_track)
        try:
            self.commercials_min_gap_minutes = int(self.commercials_min_gap_minutes)
        except Exception:
            self.commercials_min_gap_minutes = 0
        try:
            self.commercials_min_show_runtime_minutes = int(self.commercials_min_show_runtime_minutes)
        except Exception:
            self.commercials_min_show_runtime_minutes = 0
        try:
            self.commercials_max_breaks_per_show = int(self.commercials_max_breaks_per_show)
        except Exception:
            self.commercials_max_breaks_per_show = 0
        try:
            self.commercials_spots_min = int(self.commercials_spots_min)
        except Exception:
            self.commercials_spots_min = 0
        try:
            self.commercials_spots_max = int(self.commercials_spots_max)
        except Exception:
            self.commercials_spots_max = 0
        self.commercials_min_gap_minutes = max(0, min(120, self.commercials_min_gap_minutes))
        self.commercials_min_show_runtime_minutes = max(0, min(120, self.commercials_min_show_runtime_minutes))
        self.commercials_max_breaks_per_show = max(0, min(20, self.commercials_max_breaks_per_show))
        self.commercials_spots_min = max(0, min(20, self.commercials_spots_min))
        self.commercials_spots_max = max(0, min(20, self.commercials_spots_max))
        if not isinstance(self.commercials_quiet_hours, list):
            self.commercials_quiet_hours = []
        self.commercials_quiet_hours = [int(h) for h in self.commercials_quiet_hours if 0 <= int(h) <= 23]
        self.commercials_scheduled_only = bool(self.commercials_scheduled_only)

    def to_dict(self):
        self.normalize()
        return asdict(self)

    @classmethod
    def load(cls) -> "AppSettings":
        if SETTINGS_FILE.exists():
            try:
                obj = cls(**json.loads(SETTINGS_FILE.read_text()))
                obj.normalize()
                return obj
            except Exception:
                pass
        obj = cls()
        obj.normalize()
        return obj

    def save(self) -> None:
        self.normalize()
        # Atomic write: temp file + os.replace so a power cut mid-save
        # can't corrupt the settings file (which would silently reset
        # every setting to defaults on the next load).
        tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + '.tmp')
        tmp.write_text(json.dumps(self.to_dict(), indent=2))
        os.replace(tmp, SETTINGS_FILE)
