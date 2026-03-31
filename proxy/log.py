import sys
import threading
from collections import deque
from datetime import datetime
from logging import basicConfig, getLogger, INFO, WARNING, ERROR, StreamHandler, info, debug, warning, Handler, LogRecord
from typing import Callable, Optional

LOG_LEVEL = INFO

__all__ = ["info", "debug", "warning", "error", "get_log_buffer", "LogBufferHandler", "suppress_console_output", "set_log_callback"]

# Global log buffer for UI display
_log_buffer: deque = deque(maxlen=20)
_log_lock = threading.Lock()

# Console output suppression flag
_suppress_console = False
_console_handler: StreamHandler = None

# Callback for UI refresh on new log
_log_callback: Optional[Callable[[], None]] = None


def set_log_callback(callback: Optional[Callable[[], None]]) -> None:
    """
    Set a callback function to be called when a new log message is added.
    
    This is used to trigger UI refresh when a new log message is captured.
    
    Args:
        callback: Function to call on new log, or None to remove callback
    """
    global _log_callback
    _log_callback = callback


class LogBufferHandler(Handler):
    """
    Custom logging handler that captures log messages into a circular buffer.

    This handler stores the last N log messages for display in the split-terminal UI.
    Only captures INFO, WARNING, and ERROR levels (not DEBUG).
    """

    def __init__(self, maxlen: int = 20):
        super().__init__()
        self._buffer: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, record: LogRecord) -> None:
        """Add a log record to the buffer."""
        # Only capture INFO, WARNING, ERROR levels
        if record.levelno < INFO:
            return

        try:
            timestamp = datetime.now().strftime("%H:%M:%S")
            level = record.levelname
            message = record.getMessage()
            formatted = f"{timestamp} [{level:7s}] {message}"

            with self._lock:
                self._buffer.append(formatted)

            # Trigger UI refresh callback if set
            if _log_callback is not None:
                try:
                    _log_callback()
                except Exception:
                    pass  # Don't raise exceptions in logging handler
        except Exception:
            # Don't raise exceptions in logging handler
            pass

    def get_messages(self) -> list:
        """Get all buffered messages."""
        with self._lock:
            return list(self._buffer)

    def clear(self) -> None:
        """Clear the buffer."""
        with self._lock:
            self._buffer.clear()


# Global log buffer handler instance
_log_buffer_handler: LogBufferHandler = None


def get_log_buffer() -> LogBufferHandler:
    """Get the global log buffer handler."""
    global _log_buffer_handler
    return _log_buffer_handler


def _init_log_buffer() -> LogBufferHandler:
    """Initialize and attach the log buffer handler to the root logger."""
    global _log_buffer_handler

    _log_buffer_handler = LogBufferHandler(maxlen=20)

    # Attach to root logger
    root_logger = getLogger()
    root_logger.addHandler(_log_buffer_handler)

    return _log_buffer_handler


def suppress_console_output(suppress: bool) -> None:
    """
    Suppress or enable console output for logging.
    
    When suppress=True, logs are only captured in the buffer (for TTY UI mode).
    When suppress=False, logs are output to console normally.
    
    Args:
        suppress: True to suppress console output, False to enable it
    """
    global _suppress_console, _console_handler

    _suppress_console = suppress
    root_logger = getLogger()

    if suppress:
        # Remove console handler if it exists
        for handler in root_logger.handlers[:]:
            if isinstance(handler, StreamHandler) and not isinstance(handler, LogBufferHandler):
                _console_handler = handler
                root_logger.removeHandler(handler)
                break
    else:
        # Re-add console handler if it was removed
        if _console_handler and _console_handler not in root_logger.handlers:
            root_logger.addHandler(_console_handler)


# Initialize logging
basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)7s] %(message)s")
getLogger("urllib3").setLevel(WARNING)

# Initialize log buffer handler
_init_log_buffer()
