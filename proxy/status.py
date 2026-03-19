"""
Status tracking for Tor instances.

This module provides status tracking and caching for all Tor instances,
supporting the split-terminal UI display.
"""

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class TorStatus:
    """Tracks status for a single Tor instance."""

    port: int
    status: str = "checking" # "working", "error", "checking"
    ip: str = "---"
    location: Dict = field(default_factory=dict) # {country, city, lat, lon}
    uptime: float = 0.0 # seconds since instance became working
    working_since: Optional[float] = None # timestamp when instance became working
    last_check: Optional[datetime] = None
    pid: int = 0

    @property
    def status_icon(self) -> str:
        """Return the icon for the current status."""
        icons = {
            "working": "✓",
            "error": "✗",
            "checking": "⏳",
            "restarting": "⟳",
            "online": "●",
        }
        return icons.get(self.status, "?")

    @property
    def status_color(self) -> str:
        """Return the ANSI color code for the current status."""
        colors = {
            "working": "\033[92m", # Green
            "error": "\033[91m", # Red
            "checking": "\033[93m", # Yellow
            "restarting": "\033[95m", # Magenta
            "online": "\033[94m", # Blue
        }
        return colors.get(self.status, "\033[0m")


@dataclass
class StatusSummary:
    """Summary statistics for the header display."""

    version: str
    uptime: str
    heads: int
    tors: int
    working_count: int
    total_count: int
    next_rotation: str
    last_check: str


class DisplayCache:
    """Thread-safe cache for status data."""

    def __init__(self):
        self._data: Dict[int, TorStatus] = {}
        self._lock = threading.Lock()

    def update(self, port: int, status: TorStatus) -> None:
        """Update status for a specific port."""
        with self._lock:
            self._data[port] = status

    def get_all(self) -> List[TorStatus]:
        """Get all cached statuses."""
        with self._lock:
            return list(self._data.values())

    def get(self, port: int) -> Optional[TorStatus]:
        """Get status for a specific port."""
        with self._lock:
            return self._data.get(port)

    def clear(self) -> None:
        """Clear all cached data."""
        with self._lock:
            self._data.clear()


class StatusManager:
    """Manages status cache for all Tor instances."""

    def __init__(self, version: str = "0.0.0"):
        self._cache = DisplayCache()
        self._version = version
        self._start_time = datetime.now()
        self._last_rotation: Optional[datetime] = None
        self._rotation_interval: float = 3600.0  # Default 1 hour
        self._last_health_check: Optional[datetime] = None
        self._lock = threading.Lock()

    @property
    def cache(self) -> DisplayCache:
        """Get the display cache."""
        return self._cache

    def set_rotation_interval(self, interval_seconds: float) -> None:
        """Set the rotation interval for calculating next rotation."""
        with self._lock:
            self._rotation_interval = interval_seconds

    def record_rotation(self) -> None:
        """Record that a rotation just occurred."""
        with self._lock:
            self._last_rotation = datetime.now()

    def record_health_check(self) -> None:
        """Record that a health check just occurred."""
        with self._lock:
            self._last_health_check = datetime.now()

    def update_from_health_check(self, port: int, status: TorStatus) -> None:
        """Update status after a health check."""
        self._cache.update(port, status)
        self.record_health_check()

    def get_uptime(self) -> float:
        """Get total uptime in seconds."""
        return (datetime.now() - self._start_time).total_seconds()

    def get_next_rotation(self) -> float:
        """Get seconds until next rotation."""
        with self._lock:
            if self._last_rotation is None:
                return self._rotation_interval
            elapsed = (datetime.now() - self._last_rotation).total_seconds()
            remaining = self._rotation_interval - elapsed
            return max(0, remaining)

    def get_summary(self, heads: int, tors: int) -> StatusSummary:
        """Returns summary for header display."""
        statuses = self._cache.get_all()
        working_count = sum(1 for s in statuses if s.status == "working")
        total_count = len(statuses)

        # Format uptime
        uptime_seconds = self.get_uptime()
        uptime_str = self._format_duration(uptime_seconds)

        # Format next rotation
        next_rotation_seconds = self.get_next_rotation()
        next_rotation_str = self._format_duration(next_rotation_seconds)

        # Format last check
        with self._lock:
            last_check_str = "---"
            if self._last_health_check:
                last_check_str = self._last_health_check.strftime("%H:%M")

        return StatusSummary(
            version=self._version,
            uptime=uptime_str,
            heads=heads,
            tors=tors,
            working_count=working_count,
            total_count=total_count,
            next_rotation=next_rotation_str,
            last_check=last_check_str,
        )

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format duration in seconds to human-readable string."""
        seconds = int(seconds)
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60

        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        else:
            return f"{minutes}:{secs:02d}"
