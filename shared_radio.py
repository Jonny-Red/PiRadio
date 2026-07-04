#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List
import json

APP_TITLE = "Pi Radio Station Player"
APP_VERSION = 'v41'
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
        SETTINGS_FILE.write_text(json.dumps(self.to_dict(), indent=2))
