"""
Direct-First Proxy - HTTP proxy with direct-first, Tor-fallback strategy.

This module implements an HTTP proxy that:
1. Attempts direct connection to the target
2. After N consecutive failures, routes requests through Tor
3. Resets to direct mode after successful direct connection
4. Supports bypass list for hosts that should always use direct connection
"""

import ipaddress
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import List, Optional
from urllib.parse import urlparse

import requests
from requests.exceptions import RequestException

from . import log
from .service import Service


@dataclass
class FallbackStatus:
    """Status of the direct-first proxy."""

    enabled: bool
    port: int
    direct_mode: bool
    failure_count: int
    max_failures: int
    requests_total: int
    requests_direct: int
    requests_tor: int
    requests_failed: int
    bypass_count: int  # Number of bypass rules loaded


@dataclass
class BypassList:
    """
    List of hosts that should always use direct connection.
    
    Supports:
    - Exact host matching: "localhost", "api.example.com"
    - Domain suffix matching: ".example.com" (matches sub.example.com)
    - IP addresses: "127.0.0.1"
    - CIDR ranges: "192.168.0.0/16", "10.0.0.0/8"
    """
    
    entries: List[str] = field(default_factory=list)
    _cidr_cache: dict = field(default_factory=dict)
    
    def add(self, entry: str) -> None:
        """Add an entry to the bypass list."""
        entry = entry.strip()
        if entry and entry not in self.entries:
            self.entries.append(entry)
            # Pre-compile CIDR ranges for faster matching
            if '/' in entry:
                try:
                    self._cidr_cache[entry] = ipaddress.ip_network(entry, strict=False)
                except ValueError:
                    pass
    
    def load_from_string(self, bypass_string: str) -> None:
        """Load bypass list from comma-separated string."""
        for entry in bypass_string.split(','):
            self.add(entry.strip())
    
    def load_from_file(self, filepath: str) -> bool:
        """Load bypass list from file (one entry per line)."""
        try:
            path = Path(filepath)
            if path.exists():
                with open(path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            self.add(line)
                return True
        except Exception as e:
            log.warning(f"Failed to load bypass list from {filepath}: {e}")
        return False
    
    def matches(self, host: str) -> bool:
        """
        Check if host matches any entry in the bypass list.
        
        Args:
            host: Hostname or IP address to check
            
        Returns:
            True if host should bypass Tor (use direct connection)
        """
        if not self.entries:
            return False
        
        # Normalize host
        host = host.lower().strip()
        
        for entry in self.entries:
            entry_lower = entry.lower()
            
            # Exact match
            if host == entry_lower:
                return True
            
            # Domain suffix match (.example.com matches sub.example.com)
            if entry_lower.startswith('.'):
                if host.endswith(entry_lower) or host == entry_lower[1:]:
                    return True
            
            # CIDR range match
            if '/' in entry_lower:
                try:
                    ip = ipaddress.ip_address(host)
                    cidr = self._cidr_cache.get(entry)
                    if cidr and ip in cidr:
                        return True
                except ValueError:
                    pass  # host is not an IP address
        
        return False


class FallbackRequestHandler(BaseHTTPRequestHandler):
    """Handle HTTP requests with fallback logic."""

    proxy: "FallbackProxy" = None  # Set by FallbackProxy
    _target_socket: Optional[socket.socket] = None

    def log_message(self, format, *args):
        """Override to use our logging system."""
        log.debug(f"FallbackProxy: {format % args}")

    def do_GET(self):
        self._handle_request()

    def do_POST(self):
        self._handle_request()

    def do_PUT(self):
        self._handle_request()

    def do_DELETE(self):
        self._handle_request()

    def do_HEAD(self):
        self._handle_request()

    def do_OPTIONS(self):
        self._handle_request()

    def do_PATCH(self):
        self._handle_request()

    def do_CONNECT(self):
        """Handle HTTPS tunneling (CONNECT method)."""
        self._handle_connect()

    def _handle_request(self):
        """Process request with direct/Tor fallback."""
        self.proxy._requests_total += 1

        # Extract host from URL for bypass check
        host = self._extract_host()

        # Check if host is in bypass list (always use direct)
        if host and self.proxy.should_bypass(host):
            log.debug(f"DirectFirstProxy: Bypassing Tor for {host}")
            self.proxy._requests_bypassed += 1
            response = self._try_direct()
            if response is None:
                self.proxy._requests_failed += 1
                self.send_error(502, "Bad Gateway")
            else:
                self._send_response(response)
            return

        # Special mode: max_failures = -1 means "always use Tor" (except bypass)
        if self.proxy.max_failures < 0:
            response = self._try_tor()
            if response is None:
                self.proxy._requests_failed += 1
                self.send_error(502, "Bad Gateway")
            else:
                with self.proxy._lock:
                    self.proxy._requests_tor += 1
                self._send_response(response)
            return

        # Check current mode
        with self.proxy._lock:
            use_direct = self.proxy._direct_mode

        response = None
        used_tor = False

        if use_direct:
            # Try direct request
            response = self._try_direct()
            if response is None or self.proxy._is_failure(response):
                with self.proxy._lock:
                    self.proxy._failure_count += 1
                    log.info(
                        f"DirectFirstProxy: Direct request failed "
                        f"({self.proxy._failure_count}/{self.proxy.max_failures})"
                    )
                    if self.proxy._failure_count >= self.proxy.max_failures:
                        self.proxy._direct_mode = False
                        log.info(
                            f"DirectFirstProxy: Switching to Tor mode after "
                            f"{self.proxy._failure_count} failures"
                        )
                # Retry with Tor if we just switched
                if not self.proxy._direct_mode:
                    response = self._try_tor()
                    used_tor = True
            else:
                # Success - reset failure count
                with self.proxy._lock:
                    self.proxy._failure_count = 0
                    self.proxy._requests_direct += 1
        else:
            # Tor mode
            response = self._try_tor()
            used_tor = True

            # On success, try to switch back to direct mode
            if response is not None and not self.proxy._is_failure(response):
                with self.proxy._lock:
                    self.proxy._direct_mode = True
                    self.proxy._failure_count = 0
                    self.proxy._requests_tor += 1
                    log.info("DirectFirstProxy: Switching back to direct mode")

        if response is None:
            self.proxy._requests_failed += 1
            self.send_error(502, "Bad Gateway")
        else:
            self._send_response(response)

    def _extract_host(self) -> Optional[str]:
        """Extract host from request URL."""
        try:
            url = self.path
            if not url.startswith(("http://", "https://")):
                url = f"http://{self.path}"
            parsed = urlparse(url)
            return parsed.hostname
        except Exception:
            return None

    def _try_direct(self) -> Optional[requests.Response]:
        """Try to make a direct request without proxy."""
        try:
            # Build the target URL
            url = self.path
            if not url.startswith(("http://", "https://")):
                url = f"http://{self.path}"

            # Get request body if present
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else None

            # Forward headers
            headers = {}
            for key, value in self.headers.items():
                # Skip hop-by-hop headers
                if key.lower() not in (
                    "host",
                    "connection",
                    "keep-alive",
                    "proxy-authenticate",
                    "proxy-authorization",
                    "te",
                    "trailers",
                    "transfer-encoding",
                    "upgrade",
                ):
                    headers[key] = value

            # Make the request
            response = requests.request(
                method=self.command,
                url=url,
                headers=headers,
                data=body,
                timeout=self.proxy.timeout,
                allow_redirects=False,
            )
            return response

        except RequestException as e:
            log.debug(f"FallbackProxy: Direct request failed: {e}")
            return None

    def _try_tor(self) -> Optional[requests.Response]:
        """Try to make a request through Tor (HAProxy)."""
        try:
            # Build the target URL
            url = self.path
            if not url.startswith(("http://", "https://")):
                url = f"http://{self.path}"

            # Get request body if present
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else None

            # Forward headers
            headers = {}
            for key, value in self.headers.items():
                if key.lower() not in (
                    "host",
                    "connection",
                    "keep-alive",
                    "proxy-authenticate",
                    "proxy-authorization",
                    "te",
                    "trailers",
                    "transfer-encoding",
                    "upgrade",
                ):
                    headers[key] = value

            # Make the request through HAProxy (SOCKS proxy)
            proxies = {
                "http": f"socks5://127.0.0.1:{self.proxy.haproxy_port}",
                "https": f"socks5://127.0.0.1:{self.proxy.haproxy_port}",
            }

            response = requests.request(
                method=self.command,
                url=url,
                headers=headers,
                data=body,
                timeout=self.proxy.timeout,
                allow_redirects=False,
                proxies=proxies,
            )
            return response

        except RequestException as e:
            log.debug(f"FallbackProxy: Tor request failed: {e}")
            return None

    def _send_response(self, response: requests.Response):
        """Send the response back to the client."""
        self.send_response(response.status_code)

        # Forward response headers
        for key, value in response.headers.items():
            if key.lower() not in (
                "connection",
                "keep-alive",
                "proxy-authenticate",
                "proxy-authorization",
                "te",
                "trailers",
                "transfer-encoding",
                "upgrade",
            ):
                self.send_header(key, value)

        self.end_headers()

        # Forward response body
        self.wfile.write(response.content)

    def _handle_connect(self):
        """Handle HTTPS CONNECT method for tunneling."""
        # Parse target host:port
        try:
            host, port = self.path.split(":")
            port = int(port)
        except ValueError:
            self.send_error(400, "Bad Request")
            return

        # Check if host is in bypass list (always use direct)
        if host and self.proxy.should_bypass(host):
            log.debug(f"DirectFirstProxy: Bypassing Tor for CONNECT {host}")
            self.proxy._requests_bypassed += 1
            try:
                target_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                target_sock.settimeout(self.proxy.timeout)
                target_sock.connect((host, port))
                self.send_response(200, "Connection Established")
                self.end_headers()
                self._tunnel(target_sock)
                return
            except (socket.error, socket.timeout) as e:
                log.debug(f"DirectFirstProxy: Bypass CONNECT failed: {e}")
                if target_sock:
                    target_sock.close()
                self.proxy._requests_failed += 1
                self.send_error(502, "Bad Gateway")
                return

        # Special mode: max_failures = -1 means "always use Tor" (except bypass)
        if self.proxy.max_failures < 0:
            try:
                target_sock = self._connect_via_tor_socks(host, port)
                if target_sock:
                    self.send_response(200, "Connection Established")
                    self.end_headers()
                    with self.proxy._lock:
                        self.proxy._requests_tor += 1
                    self._tunnel(target_sock)
                    return
            except Exception as e:
                log.debug(f"DirectFirstProxy: Tor CONNECT failed: {e}")
            self.proxy._requests_failed += 1
            self.send_error(502, "Bad Gateway")
            return

        with self.proxy._lock:
            use_direct = self.proxy._direct_mode

        target_sock = None

        if use_direct:
            # Try direct connection
            try:
                target_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                target_sock.settimeout(self.proxy.timeout)
                target_sock.connect((host, port))

                # Success - reset failure count
                with self.proxy._lock:
                    self.proxy._failure_count = 0
                    self.proxy._requests_direct += 1

                self.send_response(200, "Connection Established")
                self.end_headers()

                self._tunnel(target_sock)
                return

            except (socket.error, socket.timeout) as e:
                log.debug(f"FallbackProxy: Direct CONNECT failed: {e}")
                if target_sock:
                    target_sock.close()

                with self.proxy._lock:
                    self.proxy._failure_count += 1
                    log.info(
                        f"FallbackProxy: Direct CONNECT failed "
                        f"({self.proxy._failure_count}/{self.proxy.max_failures})"
                    )
                    if self.proxy._failure_count >= self.proxy.max_failures:
                        self.proxy._direct_mode = False
                        log.info(
                            f"FallbackProxy: Switching to Tor mode after "
                            f"{self.proxy._failure_count} failures"
                        )

        # Fallback to Tor
        if not self.proxy._direct_mode:
            try:
                target_sock = self._connect_via_tor_socks(host, port)

                if target_sock:
                    self.send_response(200, "Connection Established")
                    self.end_headers()

                    with self.proxy._lock:
                        self.proxy._requests_tor += 1
                        # Try to switch back to direct mode on success
                        self.proxy._direct_mode = True
                        self.proxy._failure_count = 0
                        log.info("FallbackProxy: Switching back to direct mode")

                    self._tunnel(target_sock)
                    return
            except Exception as e:
                log.debug(f"FallbackProxy: Tor CONNECT failed: {e}")

        # All attempts failed
        self.proxy._requests_failed += 1
        self.send_error(502, "Bad Gateway")

    def _connect_via_tor_socks(
        self, host: str, port: int
    ) -> Optional[socket.socket]:
        """Connect to target through Tor SOCKS proxy."""
        import socks

        try:
            target_sock = socks.socksocket(socket.AF_INET, socket.SOCK_STREAM)
            target_sock.set_proxy(
                socks.SOCKS5, "127.0.0.1", self.proxy.haproxy_port
            )
            target_sock.settimeout(self.proxy.timeout)
            target_sock.connect((host, port))
            return target_sock
        except (socket.error, socket.timeout) as e:
            log.debug(f"FallbackProxy: Tor SOCKS connect failed: {e}")
            if target_sock:
                target_sock.close()
            return None

    def _tunnel(self, target_sock: socket.socket):
        """Tunnel data between client and target."""
        # Set up for bidirectional tunneling
        client_sock = self.connection
        client_sock.setblocking(False)
        target_sock.setblocking(False)

        try:
            while not self.proxy._shutdown_event.is_set():
                # Check for data from client
                try:
                    data = client_sock.recv(65536)
                    if data:
                        target_sock.sendall(data)
                    else:
                        break
                except BlockingIOError:
                    pass
                except Exception:
                    break

                # Check for data from target
                try:
                    data = target_sock.recv(65536)
                    if data:
                        client_sock.sendall(data)
                    else:
                        break
                except BlockingIOError:
                    pass
                except Exception:
                    break

                # Small sleep to prevent busy loop
                time.sleep(0.001)

        finally:
            target_sock.close()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Thread-per-request HTTP server."""

    daemon_threads = True
    allow_reuse_address = True


class FallbackProxy(Service):
    """
    HTTP proxy with direct-first, Tor-fallback strategy.

    Behavior:
    - Starts in "direct mode" - all requests go directly to target
    - On consecutive failures (default: 2), switches to "Tor mode"
    - In Tor mode, requests are routed through existing HAProxy
    - Successful direct requests reset the failure counter
    - Hosts in bypass list ALWAYS use direct connection (never Tor)
    """

    executable = None  # Pure Python, no external executable

    def __init__(
        self,
        port: int = 9090,
        haproxy_port: int = 1080,
        max_failures: int = 2,
        timeout: float = 30.0,
        bypass_list: Optional[BypassList] = None,
    ):
        self.haproxy_port = haproxy_port
        self.max_failures = max_failures
        self.timeout = timeout
        self.bypass_list = bypass_list or BypassList()

        # State
        # If max_failures < 0, start in Tor mode (always use Tor except bypass)
        self._direct_mode = max_failures >= 0
        self._failure_count = 0
        self._lock = threading.Lock()

        # Statistics
        self._requests_total = 0
        self._requests_direct = 0
        self._requests_tor = 0
        self._requests_failed = 0
        self._requests_bypassed = 0

        # Shutdown event
        self._shutdown_event = threading.Event()

        # HTTP server
        self._server: Optional[ThreadedHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None

        # Initialize base class (but don't call start() yet)
        self.port = port
        self.PID = None
        # Note: self.name is a property from Service class, returns class name in lowercase

        bypass_count = len(self.bypass_list.entries)
        log.info(f"Starting DirectFirstProxy on port {self.port} (bypass: {bypass_count} entries)")

        # Create and configure server
        FallbackRequestHandler.proxy = self
        self._server = ThreadedHTTPServer(("0.0.0.0", self.port), FallbackRequestHandler)

        # Start server in background thread
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._server_thread.start()

    def should_bypass(self, host: str) -> bool:
        """Check if host should bypass Tor (use direct connection)."""
        return self.bypass_list.matches(host)

    def get_status(self) -> FallbackStatus:
        """Get current status of the direct-first proxy."""
        with self._lock:
            return FallbackStatus(
                enabled=True,
                port=self.port,
                direct_mode=self._direct_mode,
                failure_count=self._failure_count,
                max_failures=self.max_failures,
                requests_total=self._requests_total,
                requests_direct=self._requests_direct,
                requests_tor=self._requests_tor,
                requests_failed=self._requests_failed,
                bypass_count=len(self.bypass_list.entries),
                )
    
        def _is_failure(self, response_or_exception) -> bool:
            """Determine if the result should count as a failure."""
            if response_or_exception is None:
                return True
    
            if isinstance(response_or_exception, Exception):
                # Connection errors, timeouts, etc.
                return True
    
            response = response_or_exception
            # HTTP 5xx = server error = failure
            # HTTP 4xx = client error = not a failure
            return response.status_code >= 500
    
        def stop(self):
            """Stop the direct-first proxy."""
            log.info(f"Stopping DirectFirstProxy on port {self.port}")
            self._shutdown_event.set()
    
            if self._server:
                self._server.shutdown()
                self._server.server_close()
    
            if self._server_thread and self._server_thread.is_alive():
                self._server_thread.join(timeout=5)
    
        def restart(self):
            """Restart the direct-first proxy."""
            self.stop()
            self._shutdown_event.clear()
            self._server = ThreadedHTTPServer(("0.0.0.0", self.port), FallbackRequestHandler)
            self._server_thread = threading.Thread(
                target=self._server.serve_forever,
                daemon=True,
            )
            self._server_thread.start()
