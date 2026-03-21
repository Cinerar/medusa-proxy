"""
Split Terminal UI for Medusa Proxy monitoring.

This module provides a split-terminal interface similar to htop/top,
with a status panel at the top and log output at the bottom.
"""

import sys
import threading
import time
from typing import List, Optional

from .status import StatusManager, TorStatus, StatusSummary
from .log import get_log_buffer


# ANSI Escape Codes
class ANSI:
    """ANSI escape codes for terminal control."""

    # Clear screen
    CLEAR = "\033[2J"

    # Move cursor to position (row, col)
    @staticmethod
    def move(row: int, col: int = 1) -> str:
        return f"\033[{row};{col}H"

    # Clear line
    CLEAR_LINE = "\033[2K"

    # Clear from cursor to end of line
    CLEAR_EOL = "\033[K"

    # Hide/show cursor
    HIDE_CURSOR = "\033[?25l"
    SHOW_CURSOR = "\033[?25h"

    # Alternate screen buffer (like htop/vim)
    ALT_SCREEN_ENTER = "\033[?1049h"
    ALT_SCREEN_EXIT = "\033[?1049l"

    # Colors
    GREEN = "\033[92m"    # Working
    RED = "\033[91m"      # Error
    YELLOW = "\033[93m"   # Checking
    CYAN = "\033[96m"     # Header
    WHITE = "\033[97m"    # Normal
    DIM = "\033[90m"      # Dimmed
    MAGENTA = "\033[95m"  # Restarting
    BLUE = "\033[94m"     # Online
    RESET = "\033[0m"     # Reset

    # Bold
    BOLD = "\033[1m"

    # Save/restore cursor position
    SAVE_CURSOR = "\033[s"
    RESTORE_CURSOR = "\033[u"


class SplitTerminalUI:
    """
    Split terminal interface with status panel and log.

    Provides two rendering modes:
    - TTY mode: Full split-screen with ANSI escape codes
    - Plain mode: Linear output for Docker logs
    """

    def __init__(self, status_manager: StatusManager, heads: int, tors: int):
        self.status_manager = status_manager
        self.heads = heads
        self.tors = tors
        self.tty_available = self._detect_tty()
        self._last_render_count = 0
        self._alternate_screen_active = False
        self._timer_thread: Optional[threading.Thread] = None
        self._timer_running = False

    def _detect_tty(self) -> bool:
        """Detect if running in TTY mode."""
        return sys.stdout.isatty()

    def start_timer(self) -> None:
        """Start the background timer for updating time displays."""
        if self._timer_thread is None and self.tty_available:
            self._timer_running = True
            self._timer_thread = threading.Thread(target=self._timer_loop, daemon=True)
            self._timer_thread.start()

    def stop_timer(self) -> None:
        """Stop the background timer."""
        self._timer_running = False

    def _timer_loop(self) -> None:
        """Background loop that triggers UI updates every second."""
        while self._timer_running:
            time.sleep(1)
            if self._timer_running:
                self.render()

    def render(self) -> None:
        """Render the full UI based on available mode."""
        if self.tty_available:
            self._render_tty()
        else:
            self._render_plain()

    def _enter_alternate_screen(self) -> None:
        """Enter alternate screen buffer."""
        if not self._alternate_screen_active:
            print(ANSI.ALT_SCREEN_ENTER, end="", flush=True)
            self._alternate_screen_active = True

    def _exit_alternate_screen(self) -> None:
        """Exit alternate screen buffer."""
        if self._alternate_screen_active:
            print(ANSI.ALT_SCREEN_EXIT, end="", flush=True)
            self._alternate_screen_active = False

    def _render_tty(self) -> None:
        """Render with ANSI escape codes (split screen)."""
        # Enter alternate screen on first render
        if not self._alternate_screen_active:
            self._enter_alternate_screen()

        # Get current data
        summary = self.status_manager.get_summary(self.heads, self.tors)
        statuses = self.status_manager.cache.get_all()
        log_messages = get_log_buffer().get_messages()

        # Calculate layout
        terminal_height = self._get_terminal_height()
        header_lines = 2
        table_header_lines = 1
        status_lines = max(len(statuses), self.tors)  # Always show all expected rows
        log_header_lines = 2
        separator_lines = 2

        # Calculate available space for logs
        used_lines = header_lines + table_header_lines + status_lines + log_header_lines + separator_lines
        available_log_lines = max(5, terminal_height - used_lines)

        # Build output using cursor positioning for each line
        # This is more efficient than clearing the whole screen
        output = []
        row = 1

        # Hide cursor during redraw
        output.append(ANSI.HIDE_CURSOR)

        # Header (row 1-2)
        output.append(ANSI.move(row, 1) + ANSI.CLEAR_LINE + self._render_header(summary))
        row += 1
        output.append(ANSI.move(row, 1) + ANSI.CLEAR_LINE + ANSI.CYAN + "─" * 78 + ANSI.RESET)
        row += 1

        # Table header (row 3)
        output.append(ANSI.move(row, 1) + ANSI.CLEAR_LINE + ANSI.BOLD +
            f"{'Port':>6} {'Status':<10} {'IP':>15} {'Country':<15} {'City':<15} {'Uptime':>10} {'Latency':>8}" +
            ANSI.RESET)
        row += 1

        # Status rows - always show all expected rows
        sorted_statuses = sorted(statuses, key=lambda s: s.port)
        for i in range(self.tors):
            output.append(ANSI.move(row, 1) + ANSI.CLEAR_LINE)
            if i < len(sorted_statuses):
                output.append(self._render_status_row(sorted_statuses[i]))
            row += 1

        # Separator
        output.append(ANSI.move(row, 1) + ANSI.CLEAR_LINE + ANSI.CYAN + "─" * 78 + ANSI.RESET)
        row += 1

        # Log header
        output.append(ANSI.move(row, 1) + ANSI.CLEAR_LINE + ANSI.BOLD + f"Log Output (last {len(log_messages)} lines):" + ANSI.RESET)
        row += 1

        # Log messages (limited by available space)
        visible_logs = log_messages[-available_log_lines:] if log_messages else []
        for msg in visible_logs:
            output.append(ANSI.move(row, 1) + ANSI.CLEAR_LINE + self._render_log_line(msg))
            row += 1

        # Clear remaining lines
        for _ in range(max(0, available_log_lines - len(visible_logs))):
            output.append(ANSI.move(row, 1) + ANSI.CLEAR_LINE)
            row += 1

        # Show cursor
        output.append(ANSI.SHOW_CURSOR)

        # Print all at once
        print("".join(output), end="", flush=True)

    def _render_plain(self) -> None:
        """Render for Docker logs (linear output)."""
        summary = self.status_manager.get_summary(self.heads, self.tors)

        # Single status line
        status_line = (
            f"[{summary.last_check}] Status: {summary.working_count}/{summary.total_count} working | "
            f"Next rotation: {summary.next_rotation}"
        )
        print(status_line)

    def _render_header(self, summary: StatusSummary) -> str:
        """Render the header line."""
        return (
            f"{ANSI.BOLD}Medusa Proxy{ANSI.RESET} {ANSI.CYAN}v{summary.version}{ANSI.RESET} "
            f"Uptime: {summary.uptime} │ "
            f"Heads: {summary.heads} | Tors: {summary.tors} | "
            f"Working: {ANSI.GREEN}{summary.working_count}{ANSI.RESET}/{summary.total_count} │ "
            f"Next rotation: {summary.next_rotation}"
        )

    def _render_status_row(self, status: TorStatus) -> str:
        """Render a single status row."""
        # Choose color based on status
        if status.status == "working":
            color = ANSI.GREEN
        elif status.status == "error":
            color = ANSI.RED
        elif status.status == "restarting":
            color = ANSI.MAGENTA if hasattr(ANSI, 'MAGENTA') else "\033[95m"
        elif status.status == "online":
            color = ANSI.BLUE if hasattr(ANSI, 'BLUE') else "\033[94m"
        elif status.status == "offline":
            color = ANSI.DIM
        else:
            color = ANSI.YELLOW

        # Format location
        if status.location:
            country = status.location.get("country", "---")[:15]
            city = status.location.get("city", "---")[:15]
        else:
            country = "---"
            city = "---"

        # Calculate uptime dynamically from working_since timestamp
        if status.status == "working" and status.working_since:
            current_uptime = time.time() - status.working_since
            uptime_str = self._format_duration(current_uptime)
        elif status.status == "online" and status.working_since:
            current_uptime = time.time() - status.working_since
            uptime_str = self._format_duration(current_uptime)
        else:
            uptime_str = "---"

        # Format liveness response time
        if status.liveness_ms > 0:
            liveness_str = f"{status.liveness_ms:.0f}ms"
        else:
            liveness_str = "---"

        return (
            f"{status.port:>6} "
            f"{color}{status.status_icon} {status.status.upper():<8}{ANSI.RESET} "
            f"{status.ip:>15} "
            f"{country:<15} "
            f"{city:<15} "
            f"{uptime_str:>10} "
            f"{liveness_str:>8}"
        )

    def _render_log_line(self, message: str) -> str:
        """Render a log line with appropriate coloring."""
        if "[INFO]" in message:
            return f"{ANSI.WHITE}{message}{ANSI.RESET}"
        elif "[WARNING]" in message:
            return f"{ANSI.YELLOW}{message}{ANSI.RESET}"
        elif "[ERROR]" in message:
            return f"{ANSI.RED}{message}{ANSI.RESET}"
        else:
            return message

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

    @staticmethod
    def _get_terminal_height() -> int:
        """Get terminal height, default to 24 if not available."""
        try:
            import shutil
            return shutil.get_terminal_size().lines
        except Exception:
            return 24

    def restore_terminal(self) -> None:
        """Restore terminal state on exit."""
        if self.tty_available:
            print(ANSI.SHOW_CURSOR, end="", flush=True)
            self._exit_alternate_screen()


class PlainUI:
    """
    Simple plain text UI for non-TTY environments.
    
    Outputs status updates as single lines suitable for Docker logs.
    """

    def __init__(self, status_manager: StatusManager, heads: int, tors: int):
        self.status_manager = status_manager
        self.heads = heads
        self.tors = tors

    def render(self) -> None:
        """Render a single status line."""
        summary = self.status_manager.get_summary(self.heads, self.tors)

        status_line = (
            f"[{summary.last_check}] Status: {summary.working_count}/{summary.total_count} working | "
            f"Next rotation: {summary.next_rotation}"
        )
        print(status_line)


def create_ui(status_manager: StatusManager, heads: int, tors: int, mode: str = "full") -> Optional[SplitTerminalUI]:
    """
    Factory function to create the appropriate UI instance.

    Args:
        status_manager: The status manager instance
        heads: Number of Privoxy instances
        tors: Number of Tor instances per Privoxy
        mode: UI mode - "none", "status", or "full"

    Returns:
        UI instance or None if mode is "none"
    """
    if mode == "none":
        return None
    elif mode == "status":
        return PlainUI(status_manager, heads, tors)
    else:  # "full"
        return SplitTerminalUI(status_manager, heads, tors)
