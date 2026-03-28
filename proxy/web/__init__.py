"""
Flask Web UI for Medusa Proxy monitoring.

This module provides a web interface that replicates the terminal UI,
allowing remote monitoring through a browser.
"""

from flask import Flask

from .routes import register_routes


def create_web_app(status_manager, log_buffer, heads: int, tors: int):
    """
    Create Flask app with access to shared state.

    Args:
        status_manager: StatusManager instance for Tor status data
        log_buffer: LogBufferHandler instance for log messages
        heads: Number of Privoxy instances
        tors: Number of Tor instances per Privoxy

    Returns:
        Configured Flask application
    """
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    # Store shared state in app config
    app.status_manager = status_manager
    app.log_buffer = log_buffer
    app.config["HEADS"] = heads
    app.config["TORS"] = tors

    # Register routes
    register_routes(app)

    return app
