import json
import jinja2
import requests
from signal import SIGHUP
from pathlib import Path
import os
import time
from datetime import datetime

from . import log
from .service import Service
from .status import TorStatus

CONFIG_PATH = "/etc/tor/torrc"

# Number of seconds to wait when checking if a proxy is working.
#
WORKING_TIMEOUT = 10


class Tor(Service):
    executable = "/usr/bin/tor"
    count = 0

    def __init__(
        self,
        new_circuit_period=None,
        max_circuit_dirtiness=None,
        circuit_build_timeout=None,
    ):
        self.id = Tor.count
        Tor.count += 1

        super().__init__(10000 + self.id)

        self.new_circuit_period = new_circuit_period or 120
        self.max_circuit_dirtiness = max_circuit_dirtiness or 600
        self.circuit_build_timeout = circuit_build_timeout or 60

        # Track uptime (time since instance became working)
        self._working_since: float = 0.0  # timestamp when instance became working
        self._was_working: bool = False  # track previous working state

        with open("templates/tor.cfg", "rt") as file:
            template = jinja2.Template(file.read())

        # Additional parameters for bridge enable
        #
        EXITNODES = os.environ.get("TOR_EXIT_NODES", "")

        if EXITNODES != "":
            exitnodes_list = [x.strip().strip("'") for x in EXITNODES.split(",")]
            exitnodes_string = "{" + "},{".join(exitnodes_list) + "}"
        else:
            exitnodes_string = ""

        BRIDGES = os.environ.get("TOR_BRIDGES", "")
        bridges_file = Path("bridges.lst")

        if bridges_file.exists():
            USEBRIDGES = "1"
            with open("bridges.lst", "r") as file_bridges:
                bridges_string = file_bridges.read()
        else:
            if BRIDGES == "":
                USEBRIDGES = "0"
                bridges_string = ""
            else:
                USEBRIDGES = "1"
                BRIDGESLIST = [x.strip().strip("'") for x in BRIDGES.split(",")]
                bridges_string = "\n".join(BRIDGESLIST) + "\n"

        config = template.render(
            new_circuit_period=self.new_circuit_period,
            exit_nodes=exitnodes_string,
            use_bridges=USEBRIDGES,
            bridges=bridges_string,
        )

        with open(CONFIG_PATH, "wt") as file:
            file.write(config)

        self.start()

    @property
    def uptime(self) -> float:
        """Get uptime in seconds since the instance became working."""
        if self._working_since == 0.0:
            return 0.0
        return time.time() - self._working_since

    def get_status(self) -> TorStatus:
        """
        Check if the Tor instance is working and return a TorStatus object.

        This method performs the health check and returns detailed status
        information for the UI display.

        Returns:
            TorStatus: Current status of the Tor instance
        """
        proxies = {
            "http": f"socks5://127.0.0.1:{self.port}",
            "https": f"socks5://127.0.0.1:{self.port}",
        }

        status = TorStatus(
            port=self.port,
            status="checking",
            pid=self.pid or 0,
            last_check=datetime.now(),
        )

        # Get IP.
        #
        try:
            response = requests.get(
                "https://api.ipify.org?format=json",
                proxies=proxies,
                timeout=WORKING_TIMEOUT,
            )
            ip = json.loads(response.text.strip())["ip"]
            status.ip = ip
            status.status = "working"
        except (
            KeyError,
            json.decoder.JSONDecodeError,
            requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout,
        ):
            status.ip = "---"
            status.status = "error"

        # Track when instance became working
        if status.status == "working" and not self._was_working:
            # Just became working, record the time
            self._working_since = time.time()
            self._was_working = True
        elif status.status != "working":
            # No longer working, reset tracking
            self._working_since = 0.0
            self._was_working = False

        # Set uptime for working instances
        if status.status == "working":
            status.uptime = self.uptime
            status.working_since = self._working_since

        # Get IP location if working
        #
        if status.status == "working":
            try:
                response = requests.get(
                    f"http://ip-api.com/json/{status.ip}",
                    proxies=proxies,
                    timeout=WORKING_TIMEOUT,
                )
                status.location = response.json()
            except (
                json.decoder.JSONDecodeError,
                requests.exceptions.ConnectTimeout,
                requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError,
            ):
                log.warning("🚨 Failed to get location.")
                status.location = {}

        # Log the status
        if status.location:
            location_str = [
                "",
                f"{status.location['country']:15}",
                f"{status.location['city']:18}",
                f"{status.location['lat']:+6.2f} / {status.location['lon']:+7.2f}",
            ]
            location_str = " | ".join(location_str)
        else:
            location_str = ""

        pid_str = status.pid if status.pid else "----"
        log.info(f"port {status.port}: {status.ip:>15} | PID {pid_str:>4}" + location_str)

        return status

    def is_process_running(self) -> bool:
        """
        Check if the Tor process is running by verifying PID.

        Returns:
            bool: True if the process is running, False otherwise
        """
        pid = self.pid
        if pid is None:
            return False
        try:
            # Send signal 0 to check if process exists
            import os
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def get_quick_status(self) -> TorStatus:
        """
        Get a quick status check without network requests.

        This checks if the process is running but doesn't verify
        if Tor is actually functional. Use get_status() for full check.

        Returns:
            TorStatus: Current status with 'online' or 'offline' status
        """
        status = TorStatus(
            port=self.port,
            pid=self.pid or 0,
            last_check=datetime.now(),
        )

        if self.is_process_running():
            status.status = "online"
            # Preserve working_since if we have it
            if self._working_since:
                status.working_since = self._working_since
                status.uptime = time.time() - self._working_since
        else:
            status.status = "offline"

        return status

    @property
    def working(self) -> bool:
        """
        Check if the Tor instance is working.

        This property is kept for backward compatibility.
        Use get_status() for detailed status information.

        Returns:
            bool: True if the Tor instance is working, False otherwise
        """
        status = self.get_status()
        return status.status == "working"

    @property
    def data_directory(self):
        return super().data_directory + "/" + str(self.port)

    def start(self):
        self.run(
            self.executable,
            # Suppress startup messages (before torrc is parsed).
            "--quiet",
            f"--SocksPort {self.port}",
            f"--DataDirectory {self.data_directory}",
            f"--PidFile {self.pid_file}",
        )

    def rotate_circuit(self):
        """
        Request a new Tor circuit by sending SIGHUP signal.

        This causes Tor to build a new circuit with a different exit node,
        resulting in a different IP address being used for outgoing connections.
        """
        log.info(f"Rotating circuit for Tor instance on port {self.port}.")
        self.kill(SIGHUP)
        # Record rotation time for circuit age tracking
        self._last_rotation_time = time.time()
