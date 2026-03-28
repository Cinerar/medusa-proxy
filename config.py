import os
from datetime import timedelta

VERSION = "0.5.0"

# ============================================================================
# Time Parsing Utilities
# ============================================================================


def parse_time_interval(
    time_str: str, default: timedelta = timedelta(minutes=15)
) -> timedelta:
    """
    Parse time interval string into timedelta.

    Supported formats:
    - "30s" -> 30 seconds
    - "15m" -> 15 minutes
    - "1h"  -> 1 hour

    Args:
        time_str: Time string to parse
        default: Default value if parsing fails

    Returns:
        timedelta object
    """
    if not time_str:
        return default

    try:
        value = int(time_str[:-1])
        unit = time_str[-1].lower()

        if unit == "s":
            return timedelta(seconds=value)
        elif unit == "m":
            return timedelta(minutes=value)
        elif unit == "h":
            return timedelta(hours=value)
    except (ValueError, IndexError):
        pass

    return default


# ============================================================================
# Environment Configuration
# ============================================================================

# Number of Privoxy instances (HTTP proxy endpoints)
HEADS = int(os.environ.get("HEADS", 1))

# Number of Tor instances per Privoxy
TORS = int(os.environ.get("TORS", 5))

# Health check interval - how often to check if Tor instances are working
PROXY_CHECK_INTERVAL = os.environ.get("PROXY_CHECK_INTERVAL", "15m")

# Rotation interval - how often to rotate Tor circuits (change exit nodes)
# Default: 1 hour (1h)
PROXY_ROTATE_INTERVAL = os.environ.get("PROXY_ROTATE_INTERVAL", "1h")

# Startup timeout - maximum time to wait for at least one Tor instance to become available
# Default: 2 minutes (2m)
PROXY_STARTUP_TIMEOUT = os.environ.get("PROXY_STARTUP_TIMEOUT", "2m")

# Tor exit node countries (comma-separated country codes)
TOR_EXIT_NODES = os.environ.get("TOR_EXIT_NODES", "")

# ============================================================================
# Liveness Check Configuration
# ============================================================================

# How often to check if Tor instances can reach the target URL
# Default: 30 seconds (30s)
PROXY_LIVENESS_INTERVAL = os.environ.get("PROXY_LIVENESS_INTERVAL", "30s")

# URL to check for liveness (must be reachable through Tor)
# Default: Telegram API (common use case for Tor proxies)
PROXY_LIVENESS_URL = os.environ.get("PROXY_LIVENESS_URL", "https://api.telegram.org")

# Timeout for liveness check requests in seconds
# Default: 10 seconds
PROXY_LIVENESS_TIMEOUT = int(os.environ.get("PROXY_LIVENESS_TIMEOUT", "10"))

# Jitter percentage to add randomness to liveness check interval
# This prevents all Tor instances from checking at the same time
# Example: 20% jitter on 30s interval means actual interval is 30-36 seconds
# Default: 20 percent
PROXY_LIVENESS_JITTER = int(os.environ.get("PROXY_LIVENESS_JITTER", "20"))

# ============================================================================
# UI Configuration
# ============================================================================

# UI mode: "none" (legacy), "status" (single line), "full" (split screen)
UI_MODE = os.environ.get("UI_MODE", "full")

# UI refresh interval in seconds (for TTY mode)
UI_REFRESH_INTERVAL = int(os.environ.get("UI_REFRESH_INTERVAL", "1"))

# ============================================================================
# Individual Proxy Endpoints Configuration
# ============================================================================

# Enable individual HTTP proxy endpoints for each Tor instance
# When enabled, creates one HTTP proxy per Tor instance for fixed IP routing
# Default: disabled (0)
ENABLE_INDIVIDUAL_PROXIES = os.environ.get("ENABLE_INDIVIDUAL_PROXIES", "0") == "1"

# Base port for individual proxy endpoints
# Individual proxies will be created on ports: BASE_PORT, BASE_PORT+1, BASE_PORT+2, ...
# Default: 8890 (so first individual proxy is on 8890, second on 8891, etc.)
INDIVIDUAL_PROXY_BASE_PORT = int(os.environ.get("INDIVIDUAL_PROXY_BASE_PORT", "8890"))

# ============================================================================
# Web UI Configuration
# ============================================================================

# Enable web UI (alternative to terminal UI)
# When enabled, provides a web interface for monitoring at WEB_UI_PORT
# Default: disabled (0)
ENABLE_WEB_UI = os.environ.get("ENABLE_WEB_UI", "0") == "1"

# Port for web UI
# Default: 14789 (rare port to avoid conflicts)
WEB_UI_PORT = int(os.environ.get("WEB_UI_PORT", "14789"))
