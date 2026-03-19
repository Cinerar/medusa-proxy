#!/usr/bin/env python3

import os
import re
import signal
import subprocess
import sys
import threading
import time

from config import VERSION, parse_time_interval
from config import HEADS, TORS, PROXY_CHECK_INTERVAL, PROXY_ROTATE_INTERVAL, PROXY_STARTUP_TIMEOUT
from config import UI_MODE, UI_REFRESH_INTERVAL
from proxy import Privoxy, log
from proxy.log import suppress_console_output, set_log_callback
from proxy.status import StatusManager, TorStatus
from proxy.ui import create_ui

PROXY_LIST_TXT = "proxy-list.txt"
PROXY_LIST_PY = "proxy-list.py"


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
    status_manager.set_rotation_interval(parse_time_interval(PROXY_ROTATE_INTERVAL).total_seconds())

    # Create UI instance
    ui = create_ui(status_manager, HEADS, TORS, UI_MODE)

    # Suppress console output if in TTY mode with full UI
    # Logs will only appear in the UI's log panel
    if ui and hasattr(ui, 'tty_available') and ui.tty_available:
        suppress_console_output(True)
        # Set callback to refresh UI on new log messages
        set_log_callback(ui.render)
        # Start the timer for updating time displays every second
        if hasattr(ui, 'start_timer'):
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
    log.info(f" UI_MODE: {UI_MODE}")
    log.info("")

    # Create Privoxy instances with Tor backends
    privoxy_instances = [Privoxy(TORS, i) for i in range(HEADS)]

    # Parse intervals
    check_interval = parse_time_interval(PROXY_CHECK_INTERVAL).total_seconds()
    rotate_interval = parse_time_interval(PROXY_ROTATE_INTERVAL).total_seconds()
    startup_timeout = parse_time_interval(PROXY_STARTUP_TIMEOUT).total_seconds()

    # Error instance checker thread (will be started after startup)
    error_check_interval = 30  # Check error instances every 30 seconds
    error_checker_running = False  # Will be set to True after startup

    def error_checker_thread():
        """Background thread to check error instances more frequently."""
        while error_checker_running:
            time.sleep(error_check_interval)
            if not error_checker_running:
                break

            # Check all Tor instances for error status
            for instance in privoxy_instances:
                for tor_proxy in instance.haproxy.proxies:
                    cached_status = status_manager.cache.get(tor_proxy.port)
                    if cached_status and cached_status.status in ("error", "restarting", "offline"):
                        log.info(f"[ErrorChecker] Checking port {tor_proxy.port} (status: {cached_status.status})")

                        # Check if process is running
                        quick_status = tor_proxy.get_quick_status()
                        status_manager.update_from_health_check(tor_proxy.port, quick_status)

                        if quick_status.status == "online":
                            log.info(f"[ErrorChecker] Port {tor_proxy.port} is online, checking if working...")
                            # If online, do full health check
                            time.sleep(2)  # Wait for Tor to bootstrap
                            new_status = tor_proxy.get_status()
                            status_manager.update_from_health_check(tor_proxy.port, new_status)

                            if new_status.status == "working":
                                log.info(f"[ErrorChecker] Port {tor_proxy.port} is now WORKING!")
                            else:
                                log.warning(f"[ErrorChecker] Port {tor_proxy.port} is {new_status.status}")
                        else:
                            log.warning(f"[ErrorChecker] Port {tor_proxy.port} is still {quick_status.status}")

                        # Render UI to show updated status
                        if ui:
                            ui.render()

    # Wait for at least one Tor instance to become available
    log.info("Waiting for Tor instances to bootstrap...")
    startup_check_interval = 10 # Check every 10 seconds
    start_time = time.time()

    while time.time() - start_time < startup_timeout:
        # Check if any Tor instance is working
        any_working = False
        for instance in privoxy_instances:
            for tor_proxy in instance.haproxy.proxies:
                status = tor_proxy.get_status()
                status_manager.update_from_health_check(tor_proxy.port, status)
                if status.status == "working":
                    any_working = True
                    break
            if any_working:
                break

        # Render UI during startup
        if ui:
            ui.render()

        if any_working:
            log.info("At least one Tor instance is ready. Starting main loop.")
            break

        elapsed = time.time() - start_time
        remaining = startup_timeout - elapsed
        log.debug(f" No Tor instances ready yet. Waiting... ({remaining:.0f}s remaining)")
        time.sleep(startup_check_interval)
    else:
        log.warning(f"Startup timeout ({PROXY_STARTUP_TIMEOUT}) reached. Starting main loop anyway.")
        log.warning("Health check will attempt to restart non-working Tor instances.")

    log.info("Writing proxy list.")
    with open(PROXY_LIST_TXT, "wt") as file:
        for http in privoxy_instances:
            file.write("http://127.0.0.1:%d\n" % http.port)
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

    # Main event loop
    while True:
        # Health check phase
        log.info("Testing proxies.")
        for instance in privoxy_instances:
            log.info(f"* Privoxy {instance.id}")
            haproxy = instance.haproxy
            for tor_proxy in haproxy.proxies:
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
                    status_manager.update_from_health_check(tor_proxy.port, restarting_status)

                    # Render UI to show restarting status
                    if ui:
                        ui.render()

                    # Perform the restart
                    tor_proxy.restart()

                    # Wait a moment for Tor to start
                    time.sleep(2)

                    # Check if process is running
                    quick_status = tor_proxy.get_quick_status()
                    status_manager.update_from_health_check(tor_proxy.port, quick_status)

                    # Render UI to show online status
                    if ui:
                        ui.render()

                    # If process is running, do a quick health check
                    if quick_status.status == "online":
                        # Give Tor a few more seconds to bootstrap
                        time.sleep(3)

                        # Perform full health check
                        new_status = tor_proxy.get_status()
                        status_manager.update_from_health_check(tor_proxy.port, new_status)

                        # Render UI to show working/error status
                        if ui:
                            ui.render()

        # Render UI after health check
        if ui:
            ui.render()

        # Check if rotation is needed
        current_time = time.time()
        time_since_rotation = current_time - last_rotation

        if time_since_rotation >= rotate_interval:
            log.info(f"Rotating Tor circuits (interval: {PROXY_ROTATE_INTERVAL}).")
            for instance in privoxy_instances:
                instance.rotate_circuits()
            last_rotation = current_time
            status_manager.record_rotation()
            log.info("Rotation complete.")
        else:
            remaining = rotate_interval - time_since_rotation
            log.debug(f"Next rotation in {remaining:.0f} seconds.")

        # Sleep until next health check
        log.info(f"Sleeping for {PROXY_CHECK_INTERVAL}.")
        time.sleep(check_interval)


try:
    main()
except KeyboardInterrupt:
    # Restore terminal state if UI was active
    if 'ui' in globals() and ui and hasattr(ui, 'restore_terminal'):
        ui.restore_terminal()
    try:
        sys.exit(0)
    except SystemExit:
        os._exit(0)
