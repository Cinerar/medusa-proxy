import jinja2

from .service import Service
from .haproxy import Haproxy
from .tor import Tor
from . import log


class Privoxy(Service):
    executable = "/usr/sbin/privoxy"

    def __init__(self, ntor, id=0, port=8888):
        self.id = id
        super().__init__(port + self.id)

        self.config = f"/etc/privoxy/config-{self.id}"
        self.haproxy = Haproxy(self.id, [Tor() for i in range(ntor)])

        with open("templates/privoxy.cfg", "rt") as file:
            template = jinja2.Template(file.read())

        config = template.render(
            port=self.port,
            socks=self.haproxy,
        )

        with open(self.config, "wt") as file:
            file.write(config)

        self.run(
            self.executable,
            self.config,
        )

    @classmethod
    def create_individual(cls, tor_instance, id, base_port=8890):
        """
        Create a Privoxy instance with a single fixed Tor backend.

        This is used to create individual HTTP proxy endpoints for each Tor instance,
        allowing users to route requests through a specific Tor instance for a fixed IP.

        Args:
            tor_instance: The Tor instance to use as backend
            id: Instance identifier
            base_port: Base port for HTTP proxy (default: 8890)

        Returns:
            Privoxy: New Privoxy instance with single Tor backend
        """
        privoxy_port = base_port + id
        haproxy_port = 1080 + id + 100

        instance = cls.__new__(cls)
        instance.id = id
        instance.port = privoxy_port
        instance.PID = None

        instance.config = f"/etc/privoxy/config-individual-{id}"
        instance.haproxy = Haproxy(
            id=id + 100,
            proxies=[tor_instance],
            port=haproxy_port,
            fixed_proxy=tor_instance,
        )

        with open("templates/privoxy.cfg", "rt") as file:
            template = jinja2.Template(file.read())

        config = template.render(
            port=instance.port,
            socks=instance.haproxy,
        )

        with open(instance.config, "wt") as file:
            file.write(config)

        instance.run(
            instance.executable,
            instance.config,
        )

        return instance

    def rotate_circuits(self):
        """
        Rotate Tor circuits for all backend proxies.

        This sends SIGHUP to each Tor process, causing them to build new circuits
        with different exit nodes. Used for changing IP addresses periodically.
        """
        log.info(
            f"Privoxy {self.id}: Rotating {len(self.haproxy.proxies)} Tor circuits."
        )
        for tor_proxy in self.haproxy.proxies:
            tor_proxy.rotate_circuit()

    def stop(self):
        self.haproxy.stop()
