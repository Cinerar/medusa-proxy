# Medusa Rotating Tor Proxy

<img src="medusa-banner.webp">

The Docker image is based on the following:

- Python — 3.13-alpine
- Privoxy — 3.0.34
- HAProxy — 2.8.11-01c1056
- Tor — 0.4.8.13

## HAProxy

[HAProxy](https://www.haproxy.com/) is a high availability load balancer and proxy server that spreads requests across multiple services.

Here we are using HAProxy to distribute requests across a selection of Tor instances.

HAProxy exposes a SOCKS proxy.

## Privoxy

Privoxy exposes an HTTP proxy.

## Environment Variables

- `HAPROXY_LOGIN` — Username for HAProxy (default: `admin`).
- `HAPROXY_PASSWORD` — Password for HAProxy (default: `admin`).
- `HEADS` — Number of Privoxy instances (default: `1`).
- `PROXY_CHECK_INTERVAL` — Interval at which health checks are performed (default: `15m`).
- `PROXY_ROTATE_INTERVAL` — Interval at which Tor circuits are rotated (default: `1h`).
- `TORS` — Number of Tor instances (default: `5`).
- `TOR_BRIDGES` — Bridge multiline string with bridges records (default: `""`).
- `TOR_EXIT_NODES` — Tor exit nodes config (default: `""`, for example `TOR_EXIT_NODES=ru` or `TOR_EXIT_NODES=ru,en`).
- `PROXY_STARTUP_TIMEOUT` — Maximum time to wait for at least one Tor instance to become available (default: `2m`).
- `UI_MODE` — UI display mode: `none` (legacy), `status` (single line), `full` (split screen) (default: `full`).
- `UI_REFRESH_INTERVAL` — UI refresh interval in seconds for TTY mode (default: `1`).

## Tor Bridges

Tor bridges are private entry points to the Tor network that are not publicly listed. They are special Tor relays that act like normal entry nodes but are not included in the public Tor directory. They are typically distributed privately or through controlled channels.

Why are they useful? They can help you to bypass censorship in countries or networks that block access to the public Tor network. They also make it harder for governments, ISPs, or firewalls to detect or block Tor usage.

In summary, Tor bridges allow users to access Tor anonymously when regular access is blocked or surveilled. More information can be found in the [Tor Manual](https://torproject.github.io/manual/bridges/).

To enable the bridges feature, you must specify one or more bridges as follows (in order of decreasing priority):

1. Create a `bridges.lst` file. See `sample-bridges.lst` for an example.
2. Specify the `TOR_BRIDGES` environment variable. If you're adding multiple bridges then simply separate them with commas. You can do this either by (i) setting an environment variable in your shell prior to executing `docker run`, (ii) using the `-e` argument with `docker run` or (iii) by setting it in the `docker-compose.yaml` file (see `sample-docker-compose.yaml`).

Notes:

1. If no bridges are configured then the Tor bridges feature will be disabled.
2. The bridges in the examples may not work. Get working bridges from the Tor Project's [BridgeDB](https://bridges.torproject.org/options).

## Ports

- 1080 — HAProxy port
- 2090 — HAProxy statistics port
- 8888 — Privoxy port

## Usage

The Docker image is available from [Docker Hub](https://hub.docker.com/repository/docker/datawookie/medusa-proxy/) but you can also build it locally.

```bash
# Build Docker image
docker build -t datawookie/medusa-proxy .

# Pull Docker image
docker pull datawookie/medusa-proxy:latest

# Start docker container
docker run --rm --name medusa-proxy -e TORS=3 -e HEADS=2 \
    -p 8888:8888 \
    -p 1080:1080 \
    -p 2090:2090 \
    datawookie/medusa-proxy

# Test SOCKS proxy
curl --socks5 localhost:1080 http://httpbin.org/ip

# Test HTTP proxy
curl --proxy localhost:8888 http://httpbin.org/ip

# Run Chromium through the proxy
chromium --proxy-server="http://localhost:8888" \
    --host-resolver-rules="MAP * 0.0.0.0 , EXCLUDE localhost"

# Access monitor
#
# auth login:admin
# auth pass:admin
#
http://localhost:2090 or http://admin:admin@localhost:2090
```

## Split Terminal UI

Medusa Proxy features a split-terminal UI (similar to `htop`) for real-time monitoring of Tor instances. The UI displays:

- **Header**: Version, uptime, instance counts, next rotation time
- **Status Table**: Port, status, IP, country, city, uptime, latency
- **Log Panel**: Last 20 log messages

### Running with Full UI

To use the split-terminal UI, you need to allocate a TTY. Use one of these methods:

```bash
# Using docker compose (recommended)
docker compose run --rm -it --service-ports medusa-proxy

# Using docker run
docker run --rm -it -e UI_MODE=full \
    -p 8888:8888 -p 1080:1080 -p 2090:2090 \
    datawookie/medusa-proxy

# Using docker compose with explicit TTY
# Add 'tty: true' to your docker-compose.yaml and run:
docker compose up
```

### UI Modes

- `UI_MODE=none` — Legacy output (no UI)
- `UI_MODE=status` — Single-line status display
- `UI_MODE=full` — Split-screen UI with status table and logs (default)

### UI Controls

- Press `Ctrl+C` to exit (terminal will be restored automatically)

### Liveness Monitoring

The UI includes a liveness checker that verifies each working Tor instance can reach a target URL (default: `https://api.telegram.org`). This helps identify instances that are "working" but blocked by the target service.

Liveness parameters:
- `PROXY_LIVENESS_INTERVAL` — How often to check (default: `30s`)
- `PROXY_LIVENESS_URL` — URL to test (default: `https://api.telegram.org`)
- `PROXY_LIVENESS_TIMEOUT` — Request timeout in seconds (default: `10`)
- `PROXY_LIVENESS_JITTER` — Random delay percentage to avoid predictable patterns (default: `20`)
