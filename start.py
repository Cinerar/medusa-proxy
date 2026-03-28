#!/usr/bin/env python3

import os
import re
import signal
import subprocess
import sys
import threading
import time

from config import VERSION, parse_time_interval

# Global shutdown event for graceful termination
shutdown_event = threading.Event()
from config import (
    HEADS,
    TORS,
    PROXY_CHECK_INTERVAL,
    PROXY_ROTATE_INTERVAL,
    PROXY_STARTUP_TIMEOUT,
)
from config import UI_MODE, UI_REFRESH_INTERVAL
from config import (
    PROXY_LIVENESS_INTERVAL,
    PROXY_LIVENESS_URL,
    PROXY_LIVENESS_TIMEOUT,
    PROXY_LIVENESS_JITTER,
)
from config import ENABLE_INDIVIDUAL_PROXIES, INDIVIDUAL_PROXY_BASE_PORT
from config import ENABLE_WEB_UI, WEB_UI_PORT
from proxy import Privoxy, log
from proxy.log import suppress_console_output, set_log_callback, get_log_buffer
from proxy.status import StatusManager, TorStatus
from proxy.ui import create_ui

PROXY_LIST_TXT = "proxy-list.txt"
PROXY_LIST_PY = "proxy-list.py"


def interruptible_sleep(seconds: float, check_interval: float = 0.5) -> bool:
    """
    Sleep that can be interrupted by shutdown_event.
    
    Args:
        seconds: Total seconds to sleep
        check_interval: How often to check shutdown_event (default 0.5s)
    
    Returns:
        True if sleep completed normally, False if interrupted
    """
    elapsed = 0.0
    while elapsed < seconds:
        if shutdown_event.is_set():
            return False
        
        sleep_time = min(check_interval, seconds - elapsed)
        time.sleep(sleep_time)
        elapsed += sleep_time
    
    return True


def setup_signal_handlers(ui=None):
    """Setup signal handlers for graceful shutdown."""
    
    def signal_handler(signum, frame):
        signal_name = signal.Signals(signum).name
        log.info(f"Received {signal_name}, initiating graceful shutdown...")
        
        # Set shutdown event to stop all threads
        shutdown_event.set()
        
        # Restore terminal state if UI was active
        if ui and hasattr(ui, "restore_terminal"):
            ui.restore_terminal()
        
        # Force exit - don't rely on garbage collection
        os._exit(0)
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)


def reap_children(*_):
    """Reap any exited child process so PID 1 does not accumulate zombies."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            # No child processes.
            break

        if pid == 0:
            # Child processes still running, nothing to reap right now.
            break


def get_versions():
    for cmd in ["privoxy --version", "haproxy -v", "tor --version"]:
        result = subprocess.run(cmd.split(), stdout=subprocess.PIPE)

        version = result.stdout.decode("utf-8").partition("\n")[0]
        version = re.sub(r" +([0-9/]{10})?[ -]*\(?(https://.*)?\)?\.?$", "", version)
        version = re.sub(r" version", ":", version)
        version = re.sub(r"\.$", "", version)

        log.info("- " + version)


def main():
    signal.signal(signal.SIGCHLD, reap_children)

    # Initialize status manager
    status_manager = StatusManager(version=VERSION)
    status_manager.set_rotation_interval(
        parse_time_interval(PROXY_ROTATE_INTERVAL).total_seconds()
    )

    # Create UI instance
    ui = create_ui(status_manager, HEADS, TORS, UI_MODE)

    # Setup signal handlers for graceful shutdown (after UI is created)
    setup_signal_handlers(ui)

    # Suppress console output if in TTY mode with full UI
    status_manager.set_rotation_interval(
        parse_time_interval(PROXY_ROTATE_INTERVAL).total_seconds()
    )

    # Create UI instance
    ui = create_ui(status_manager, HEADS, TORS, UI_MODE)

    # Suppress console output if in TTY mode with full UI
    # Logs will only appear in the UI's log panel
    if ui and hasattr(ui, "tty_available") and ui.tty_available:
        suppress_console_output(True)
        # Set callback to refresh UI on new log messages
        set_log_callback(ui.render)
        # Start the timer for updating time displays every second
        if hasattr(ui, "start_timer"):
            ui.start_timer()
        # Render initial empty UI
        ui.render()

    log.info("========================================")
    log.info(f"Medusa Proxy: {VERSION}")
    log.info("")
    get_versions()
    log.info("========================================")

    # Log configuration
    log.info("Configuration:")
    log.info(f" HEADS: {HEADS}")
    log.info(f" TORS: {TORS}")
    log.info(f" PROXY_CHECK_INTERVAL: {PROXY_CHECK_INTERVAL}")
    log.info(f" PROXY_ROTATE_INTERVAL: {PROXY_ROTATE_INTERVAL}")
    log.info(f" PROXY_STARTUP_TIMEOUT: {PROXY_STARTUP_TIMEOUT}")
    log.info(f" PROXY_LIVENESS_INTERVAL: {PROXY_LIVENESS_INTERVAL}")
    log.info(f" PROXY_LIVENESS_URL: {PROXY_LIVENESS_URL}")
    log.info(f" UI_MODE: {UI_MODE}")
    log.info(f" ENABLE_WEB_UI: {ENABLE_WEB_UI}")
    if ENABLE_WEB_UI:
        log.info(f" WEB_UI_PORT: {WEB_UI_PORT}")
    log.info("")

    # Start Web UI if enabled
    web_thread = None
    if ENABLE_WEB_UI:
        try:
            from proxy.web import create_web_app

            # Create Flask app with shared state
            web_app = create_web_app(
                status_manager=status_manager,
                log_buffer=get_log_buffer(),
                heads=HEADS,
                tors=TORS,
            )

            def run_web_server():
                """Run Flask web server in background thread."""
                import logging
                # Suppress Flask/Werkzeug logging except errors
                log = logging.getLogger("werkzeug")
                log.setLevel(logging.ERROR)
                web_app.run(
                    host="0.0.0.0",
                    port=WEB_UI_PORT,
                    threaded=True,
                    use_reloader=False,
                )

            web_thread = threading.Thread(target=run_web_server, daemon=True)
            web_thread.start()
            log.info(f"Web UI available at http://0.0.0.0:{WEB_UI_PORT}")
        except ImportError as e:
            log.warning(f"Web UI disabled: Flask not installed ({e})")
        except Exception as e:
            log.warning(f"Failed to start Web UI: {e}")

    # Create Privoxy instances with Tor backends
    privoxy_instances = [Privoxy(TORS, i) for i in range(HEADS)]

    # Create individual proxy instances if enabled
    individual_privoxy_instances = []
    if ENABLE_INDIVIDUAL_PROXIES:
        log.info("Creating individual proxy endpoints...")
        reference_haproxy = privoxy_instances[0].haproxy
        for i, tor_instance in enumerate(reference_haproxy.proxies):
            individual = Privoxy.create_individual(
                tor_instance,
                id=i,
                base_port=INDIVIDUAL_PROXY_BASE_PORT,
            )
            individual_privoxy_instances.append(individual)
            log.info(
                f"  Individual proxy #{i}: HTTP port {individual.port} -> Tor port {tor_instance.port}"
            )
        log.info(
            f"Created {len(individual_privoxy_instances)} individual proxy endpoints."
        )

    # Parse intervals
    check_interval = parse_time_interval(PROXY_CHECK_INTERVAL).total_seconds()
    rotate_interval = parse_time_interval(PROXY_ROTATE_INTERVAL).total_seconds()
    startup_timeout = parse_time_interval(PROXY_STARTUP_TIMEOUT).total_seconds()
    liveness_interval = parse_time_interval(PROXY_LIVENESS_INTERVAL).total_seconds()

    # Error instance checker thread (will be started after startup)
    error_check_interval = 30  # Check error instances every 30 seconds
    error_checker_running = False  # Will be set to True after startup

    def error_checker_thread():
        """Background thread to check error instances more frequently."""
        while not shutdown_event.is_set() and error_checker_running:
            # Use interruptible sleep
            if not interruptible_sleep(error_check_interval):
                break
            if not error_checker_running or shutdown_event.is_set():
                break
    
            # Check all Tor instances for error status
            for instance in privoxy_instances:
                if shutdown_event.is_set():
                    break
                for tor_proxy in instance.haproxy.proxies:
                    if shutdown_event.is_set():
                        break
                    cached_status = status_manager.cache.get(tor_proxy.port)
                    if cached_status and cached_status.status in (
                        "error",
                        "restarting",
                        "offline",
                    ):
                        log.info(
                            f"[ErrorChecker] Checking port {tor_proxy.port} (status: {cached_status.status})"
                        )
    
                        # Check if process is running
                        quick_status = tor_proxy.get_quick_status()
                        status_manager.update_from_health_check(
                            tor_proxy.port, quick_status
                        )
    
                        if quick_status.status == "online":
                            log.info(
                                f"[ErrorChecker] Port {tor_proxy.port} is online, checking if working..."
                            )
                            # If online, do full health check
                            if not interruptible_sleep(2):  # Wait for Tor to bootstrap
                                break
                            new_status = tor_proxy.get_status()
                            status_manager.update_from_health_check(
                                tor_proxy.port, new_status
                            )
    
                            if new_status.status == "working":
                                log.info(
                                    f"[ErrorChecker] Port {tor_proxy.port} is now WORKING!"
                                )
                            else:
                                log.warning(
                                    f"[ErrorChecker] Port {tor_proxy.port} health check failed: {new_status.status} (IP: {new_status.ip or 'N/A'})"
                                )
                        else:
                            log.warning(
                                f"[ErrorChecker] Port {tor_proxy.port} process not running (PID: {tor_proxy.pid or 'N/A'})"
                            )
    
                    # Render UI to show updated status
                    if ui and not shutdown_event.is_set():
                        ui.render()

    # Liveness checker thread (will be started after startup)
    liveness_checker_running = False  # Will be set to True after startup

    def liveness_checker_thread():
        """Background thread to check if working Tor instances can reach target URL."""
        import random
        
        while not shutdown_event.is_set() and liveness_checker_running:
            # Calculate sleep with jitter to avoid predictable patterns
            jitter_seconds = liveness_interval * (PROXY_LIVENESS_JITTER / 100.0)
            actual_sleep = liveness_interval + random.uniform(0, jitter_seconds)
            # Use interruptible sleep
            if not interruptible_sleep(actual_sleep):
                break
            if not liveness_checker_running or shutdown_event.is_set():
                break
        
            # Check all WORKING Tor instances
            for instance in privoxy_instances:
                if shutdown_event.is_set():
                    break
                for tor_proxy in instance.haproxy.proxies:
                    if shutdown_event.is_set():
                        break
                    cached_status = status_manager.cache.get(tor_proxy.port)
        
                    # Only check working instances
                    if cached_status and cached_status.status == "working":
                        log.info(
                            f"[LivenessChecker] Checking port {tor_proxy.port} -> {PROXY_LIVENESS_URL}"
                        )
                        success, error_msg, response_ms = tor_proxy.check_liveness(
                            PROXY_LIVENESS_URL, PROXY_LIVENESS_TIMEOUT
                        )
        
                        # Update liveness_ms in cached status
                        if cached_status.liveness_ms != response_ms:
                            cached_status.liveness_ms = response_ms
                            status_manager.update_from_health_check(
                                tor_proxy.port, cached_status
                            )
                        if ui and not shutdown_event.is_set():
                            ui.render()
        
                        if success:
                            log.info(
                                f"[LivenessChecker] Port {tor_proxy.port} - OK ({response_ms:.0f}ms)"
                            )
                        else:
                            log.warning(
                                f"[LivenessChecker] Port {tor_proxy.port} - FAILED: {error_msg}"
                            )
                            log.warning(
                                f"[LivenessChecker] Restarting Tor on port {tor_proxy.port}"
                            )
        
                            # Set restarting status BEFORE restart
                            from proxy.status import TorStatus
        
                            restarting_status = TorStatus(
                                port=tor_proxy.port,
                                status="restarting",
                                pid=tor_proxy.pid or 0,
                            )
                            status_manager.update_from_health_check(
                                tor_proxy.port, restarting_status
                            )
        
                            # Render UI to show restarting status
                            if ui and not shutdown_event.is_set():
                                ui.render()
        
                            # Reset working tracking since we're restarting
                            tor_proxy._working_since = 0.0
                            tor_proxy._was_working = False
        
                            # Perform the restart
                            tor_proxy.restart()
        
                            # Wait a moment for Tor to start
                            if not interruptible_sleep(2):
                                break
        
                            # Check if process is running
                            quick_status = tor_proxy.get_quick_status()
                            status_manager.update_from_health_check(
                                tor_proxy.port, quick_status
                            )
        
                            # Render UI to show new status
                            if ui and not shutdown_event.is_set():
                                ui.render()
        
                            # If process is running, do a full health check
                            if quick_status.status == "online":
                                if not interruptible_sleep(3):  # Wait for bootstrap
                                    break
                                new_status = tor_proxy.get_status()
                                status_manager.update_from_health_check(
                                    tor_proxy.port, new_status
                                )
        
                                if new_status.status == "working":
                                    log.info(
                                        f"[LivenessChecker] Port {tor_proxy.port} is now WORKING!"
                                    )
                                else:
                                    log.warning(
                                        f"[LivenessChecker] Port {tor_proxy.port} is {new_status.status}"
                                    )
        
                                if ui and not shutdown_event.is_set():
                                    ui.render()
        
                        # Small delay between instances to avoid peak load
                        if not interruptible_sleep(0.5):
                            break

                    # Wait for at least one Tor instance to become available

    log.info("Waiting for Tor instances to bootstrap...")
    startup_check_interval = 10  # Check every 10 seconds
    start_time = time.time()

    while time.time() - start_time < startup_timeout:
        if shutdown_event.is_set():
            log.info("Shutdown requested during startup.")
            break
    
        # Check if any Tor instance is working
        any_working = False
        for instance in privoxy_instances:
            if shutdown_event.is_set():
                break
            for tor_proxy in instance.haproxy.proxies:
                if shutdown_event.is_set():
                    break
                status = tor_proxy.get_status()
                status_manager.update_from_health_check(tor_proxy.port, status)
                if status.status == "working":
                    any_working = True
                    break
            if any_working:
                break
    
        # Render UI during startup
        if ui and not shutdown_event.is_set():
            ui.render()
    
        if any_working:
            log.info("At least one Tor instance is ready. Starting main loop.")
            break
    
        elapsed = time.time() - start_time
        remaining = startup_timeout - elapsed
        log.debug(
            f" No Tor instances ready yet. Waiting... ({remaining:.0f}s remaining)"
        )
        # Use interruptible sleep
        if not interruptible_sleep(startup_check_interval):
            break
    else:
        log.warning(
            f"Startup timeout ({PROXY_STARTUP_TIMEOUT}) reached. Starting main loop anyway."
        )
        log.warning("Health check will attempt to restart non-working Tor instances.")

    log.info("Writing proxy list.")
    with open(PROXY_LIST_TXT, "wt") as file:
        for http in privoxy_instances:
            file.write("http://127.0.0.1:%d\n" % http.port)
    log.info("Done.")

    # Write individual proxy list if enabled
    if ENABLE_INDIVIDUAL_PROXIES and individual_privoxy_instances:
        individual_proxy_file = "proxy-list-individual.txt"
        log.info(f"Writing individual proxy list to {individual_proxy_file}.")
        with open(individual_proxy_file, "wt") as file:
            file.write("# Individual Proxy Endpoints\n")
            file.write(
                "# Each proxy routes through a specific Tor instance for fixed IP\n"
            )
            file.write(
                "# Format: http://127.0.0.1:<PORT>  # Tor SOCKS port: <TOR_PORT>\n\n"
            )
            for i, http in enumerate(individual_privoxy_instances):
                tor_port = individual_privoxy_instances[i].haproxy.proxies[0].port
                file.write(f"http://127.0.0.1:{http.port}  # Tor port: {tor_port}\n")
        log.info("Done.")

    log.info("Serving proxy list.")
    os.spawnl(os.P_NOWAIT, sys.executable, sys.executable, PROXY_LIST_PY)

    # Track last rotation time
    last_rotation = time.time()
    status_manager.record_rotation()

    # Start error checker thread (now that startup is complete)
    error_checker_running = True
    error_checker = threading.Thread(target=error_checker_thread, daemon=True)
    error_checker.start()
    log.info("Error checker thread started (checking every 30s).")

    # Start liveness checker thread (now that startup is complete)
    liveness_checker_running = True
    liveness_checker = threading.Thread(target=liveness_checker_thread, daemon=True)
    liveness_checker.start()
    log.info(
        f"Liveness checker thread started (checking {PROXY_LIVENESS_URL} every {PROXY_LIVENESS_INTERVAL})."
    )

    # Main event loop
    while not shutdown_event.is_set():
        # Health check phase
        log.info("Testing proxies.")
        for instance in privoxy_instances:
            if shutdown_event.is_set():
                break
            log.info(f"* Privoxy {instance.id}")
            haproxy = instance.haproxy
            for tor_proxy in haproxy.proxies:
                if shutdown_event.is_set():
                    break
                # Get status and update cache
                status = tor_proxy.get_status()
                status_manager.update_from_health_check(tor_proxy.port, status)
    
                # Restart if not working
                if status.status != "working":
                    log.warning(f" Restarting Tor on port {tor_proxy.port}.")
    
                    # Set restarting status and update cache immediately
                    from proxy.status import TorStatus
    
                    restarting_status = TorStatus(
                        port=tor_proxy.port,
                        status="restarting",
                        pid=tor_proxy.pid or 0,
                    )
                    status_manager.update_from_health_check(
                        tor_proxy.port, restarting_status
                    )
    
                    # Render UI to show restarting status
                    if ui and not shutdown_event.is_set():
                        ui.render()
    
                    # Perform the restart
                    tor_proxy.restart()
    
                    # Wait a moment for Tor to start
                    if not interruptible_sleep(2):
                        break
    
                    # Check if process is running
                    quick_status = tor_proxy.get_quick_status()
                    status_manager.update_from_health_check(
                        tor_proxy.port, quick_status
                    )
    
                    # Render UI to show online status
                    if ui and not shutdown_event.is_set():
                        ui.render()
    
                    # If process is running, do a quick health check
                    if quick_status.status == "online":
                        # Give Tor a few more seconds to bootstrap
                        if not interruptible_sleep(3):
                            break
    
                        # Perform full health check
                        new_status = tor_proxy.get_status()
                        status_manager.update_from_health_check(
                            tor_proxy.port, new_status
                        )
    
                        # Render UI to show working/error status
                        if ui and not shutdown_event.is_set():
                            ui.render()
    
        # Render UI after health check
        if ui and not shutdown_event.is_set():
            ui.render()
    
        # Check if rotation is needed
        if not shutdown_event.is_set():
            current_time = time.time()
            time_since_rotation = current_time - last_rotation
    
            if time_since_rotation >= rotate_interval:
                log.info(f"Rotating Tor circuits (interval: {PROXY_ROTATE_INTERVAL}).")
                for instance in privoxy_instances:
                    if shutdown_event.is_set():
                        break
                    instance.rotate_circuits()
                last_rotation = current_time
                status_manager.record_rotation()
                log.info("Rotation complete.")
            else:
                remaining = rotate_interval - time_since_rotation
                log.debug(f"Next rotation in {remaining:.0f} seconds.")
    
        # Sleep until next health check
        log.info(f"Sleeping for {PROXY_CHECK_INTERVAL}.")
        if not interruptible_sleep(check_interval):
            break


try:
    main()
except KeyboardInterrupt:
    # Restore terminal state if UI was active
    if "ui" in globals() and ui and hasattr(ui, "restore_terminal"):
        ui.restore_terminal()
    try:
        sys.exit(0)
    except SystemExit:
        os._exit(0)
