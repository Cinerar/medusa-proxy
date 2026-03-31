"""
Status tracking for Tor instances.

This module provides status tracking and caching for all Tor instances,
supporting the split-terminal UI display.
"""

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .direct_first import FallbackStatus, FallbackProxy
    from .privoxy import Privoxy


@dataclass
class TorStatus:
    """Tracks status for a single Tor instance."""

    port: int
    status: str = "checking"  # "working", "error", "checking"
    ip: str = "---"
    location: Dict = field(default_factory=dict)  # {country, city, lat, lon}
    uptime: float = 0.0  # seconds since instance became working
    working_since: Optional[float] = None  # timestamp when instance became working
    last_check: Optional[datetime] = None
    pid: int = 0
    liveness_ms: float = 0.0  # response time in milliseconds from liveness check

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


@dataclass
class ProxyEndpointStatus:
    """Status of a single proxy endpoint."""

    proxy_type: str  # "HTTP", "SOCKS", "Direct-First", "Individual"
    port: int
    status: str  # "active", "inactive", "tor", "direct"
    details: str  # Human-readable details


@dataclass
class DirectFirstStatus:
    """Status of the Direct-First proxy."""

    enabled: bool
    port: int
    mode: str  # "direct" or "tor"
    failure_count: int
    max_failures: int
    bypass_count: int
    requests_total: int
    requests_direct: int
    requests_tor: int


@dataclass
class IndividualProxyStatus:
    """Status of an individual proxy endpoint."""

    port: int
    tor_port: int
    status: str  # "active", "inactive"
    ip: str
    location: Dict = field(default_factory=dict)


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
        self._fallback_proxy = None  # Reference to FallbackProxy (DirectFirst) instance
        self._individual_proxies: List["Privoxy"] = []  # List of individual Privoxy instances
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

    def set_fallback_proxy(self, fallback_proxy: "FallbackProxy") -> None:
        """Set reference to the fallback proxy instance."""
        with self._lock:
            self._fallback_proxy = fallback_proxy

    def get_fallback_status(self) -> Optional["FallbackStatus"]:
        """Get current status of the fallback proxy."""
        with self._lock:
            if self._fallback_proxy is None:
                return None
            return self._fallback_proxy.get_status()

    def set_individual_proxies(self, proxies: List["Privoxy"]) -> None:
        """Set reference to individual proxy instances."""
        with self._lock:
            self._individual_proxies = proxies

    def get_direct_first_status(self) -> Optional[DirectFirstStatus]:
        """
        Get status of the Direct-First proxy.

        Returns:
            DirectFirstStatus or None if not enabled
        """
        with self._lock:
            if self._fallback_proxy is None:
                return None

            status = self._fallback_proxy.get_status()
            return DirectFirstStatus(
                enabled=status.enabled,
                port=status.port,
                mode="direct" if status.direct_mode else "tor",
                failure_count=status.failure_count,
                max_failures=status.max_failures,
                bypass_count=status.bypass_count,
                requests_total=status.requests_total,
                requests_direct=status.requests_direct,
                requests_tor=status.requests_tor,
            )

    def get_individual_proxies_status(self) -> List[IndividualProxyStatus]:
        """
        Get status of all individual proxy endpoints.

        Returns:
            List of IndividualProxyStatus for each individual proxy
        """
        with self._lock:
            if not self._individual_proxies:
                return []

            statuses = []
            for privoxy in self._individual_proxies:
                # Get the Tor status for this individual proxy
                tor_status = self._cache.get(privoxy.haproxy.fixed_proxy.port)
                if tor_status:
                    statuses.append(
                        IndividualProxyStatus(
                            port=privoxy.port,
                            tor_port=privoxy.haproxy.fixed_proxy.port,
                            status="active" if tor_status.status == "working" else "inactive",
                            ip=tor_status.ip,
                            location=tor_status.location,
                        )
                    )
                else:
                    statuses.append(
                        IndividualProxyStatus(
                            port=privoxy.port,
                            tor_port=privoxy.haproxy.fixed_proxy.port,
                            status="inactive",
                            ip="---",
                            location={},
                        )
                    )
            return statuses

    def get_proxy_endpoints(self, heads: int, tors: int) -> List[ProxyEndpointStatus]:
        """
        Get status of all proxy endpoints for display.

        Args:
            heads: Number of Privoxy instances
            tors: Number of Tor instances per Privoxy

        Returns:
            List of ProxyEndpointStatus for each endpoint
        """
        endpoints = []

        # HTTP Proxy (Privoxy) - port 8888
        working_count = sum(1 for s in self._cache.get_all() if s.status == "working")
        endpoints.append(
            ProxyEndpointStatus(
                proxy_type="HTTP Proxy",
                port=8888,
                status="active",
                details=f"Balanced ({working_count} Tor)",
            )
        )

        # SOCKS Proxy (HAProxy) - port 1080
        endpoints.append(
            ProxyEndpointStatus(
                proxy_type="SOCKS Proxy",
                port=1080,
                status="active",
                details="Load balanced",
            )
        )

        # Direct-First Proxy - port 9090
        df_status = self.get_direct_first_status()
        if df_status:
            mode_str = "Direct" if df_status.mode == "direct" else "Tor"
            endpoints.append(
                ProxyEndpointStatus(
                    proxy_type="Direct-First",
                    port=df_status.port,
                    status=df_status.mode,
                    details=f"Bypass: {df_status.bypass_count} entries",
                )
            )

        # Individual Proxies - ports 8890+
        individual_statuses = self.get_individual_proxies_status()
        if individual_statuses:
            port_range = f"{individual_statuses[0].port}-{individual_statuses[-1].port}"
            active_count = sum(1 for s in individual_statuses if s.status == "active")
            endpoints.append(
                ProxyEndpointStatus(
                    proxy_type="Individual",
                    port=port_range,
                    status="active" if active_count > 0 else "inactive",
                    details=f"{len(individual_statuses)} endpoints",
                )
            )

        return endpoints

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
