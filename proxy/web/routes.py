"""
Flask routes for Web UI.

Provides SSE endpoint for real-time updates and REST API for one-shot queries.
"""

import json
import time
from flask import Response, render_template, jsonify

from proxy.status import TorStatus, StatusSummary


def _tor_status_to_dict(status: TorStatus) -> dict:
    """Convert TorStatus dataclass to dictionary for JSON serialization."""
    return {
        "port": status.port,
        "status": status.status,
        "status_icon": status.status_icon,
        "ip": status.ip,
        "location": status.location,
        "uptime": status.uptime,
        "working_since": status.working_since,
        "last_check": status.last_check.isoformat() if status.last_check else None,
        "pid": status.pid,
        "liveness_ms": status.liveness_ms,
    }


def _status_summary_to_dict(summary: StatusSummary) -> dict:
    """Convert StatusSummary dataclass to dictionary for JSON serialization."""
    return {
        "version": summary.version,
        "uptime": summary.uptime,
        "heads": summary.heads,
        "tors": summary.tors,
        "working_count": summary.working_count,
        "total_count": summary.total_count,
        "next_rotation": summary.next_rotation,
        "last_check": summary.last_check,
    }


def register_routes(app):
    """Register all routes with the Flask app."""

    @app.route("/")
    def index():
        """Serve the main HTML page."""
        return render_template(
            "index.html",
            heads=app.config["HEADS"],
            tors=app.config["TORS"],
        )

    @app.route("/events")
    def sse_stream():
        """
        Server-Sent Events endpoint for real-time updates.

        Streams status and log updates to connected clients.
        Uses 'event' field to differentiate between status and log updates.
        """

        def event_stream():
            last_status_hash = None
            last_log_hash = None
    
            while True:
                try:
                    # Get current state
                    status_manager = app.status_manager
                    log_buffer = app.log_buffer
    
                    # Get status data
                    statuses = status_manager.cache.get_all()
                    summary = status_manager.get_summary(
                        app.config["HEADS"],
                        app.config["TORS"],
                    )
    
                    # Get log messages
                    log_messages = log_buffer.get_messages()
    
                    # Create status hash to detect changes
                    status_data = {
                        "statuses": [_tor_status_to_dict(s) for s in statuses],
                        "summary": _status_summary_to_dict(summary),
                    }
                    current_status_hash = hash(json.dumps(status_data, sort_keys=True))
    
                    # Send status update if changed
                    if current_status_hash != last_status_hash:
                        yield f"event: status\ndata: {json.dumps(status_data)}\n\n"
                        last_status_hash = current_status_hash
    
                    # Create log hash to detect content changes (not just count)
                    current_log_hash = hash(tuple(log_messages))
    
                    # Send log update if content changed
                    if current_log_hash != last_log_hash:
                        yield f"event: logs\ndata: {json.dumps(log_messages)}\n\n"
                        last_log_hash = current_log_hash
    
                    # Sleep before next check
                    time.sleep(1)

                except GeneratorExit:
                    # Client disconnected
                    break
                except Exception as e:
                    # Log error but continue streaming
                    import logging
                    logging.error(f"SSE stream error: {e}")
                    time.sleep(1)

        return Response(
            event_stream(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            },
        )

    @app.route("/api/status")
    def api_status():
        """
        One-shot status snapshot.

        Returns current status of all Tor instances as JSON.
        Useful for polling or initial state fetch.
        """
        status_manager = app.status_manager
        statuses = status_manager.cache.get_all()
        summary = status_manager.get_summary(
            app.config["HEADS"],
            app.config["TORS"],
        )

        return jsonify(
            {
                "statuses": [_tor_status_to_dict(s) for s in statuses],
                "summary": _status_summary_to_dict(summary),
            }
        )

    @app.route("/api/logs")
    def api_logs():
        """
        One-shot log messages.

        Returns current log buffer as JSON.
        """
        log_messages = app.log_buffer.get_messages()
        return jsonify({"logs": log_messages})

    @app.route("/api/health")
    def api_health():
        """
        Health check endpoint.

        Returns basic health information.
        """
        status_manager = app.status_manager
        statuses = status_manager.cache.get_all()
        working_count = sum(1 for s in statuses if s.status == "working")

        return jsonify(
            {
                "status": "healthy" if working_count > 0 else "degraded",
                "working_instances": working_count,
                "total_instances": len(statuses),
            }
        )
