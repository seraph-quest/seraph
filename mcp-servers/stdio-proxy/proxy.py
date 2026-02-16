"""Seraph Stdio-to-HTTP MCP Proxy — exposes stdio MCP servers over HTTP.

Reads config from data/stdio-proxies.json, spawns each enabled server as a
subprocess via FastMCP's proxy mechanism, and serves each on its own HTTP port.

Runs natively on macOS (not Docker) so tools like Things3 can access
system APIs (AppleScript, etc.).

Usage:
    python proxy.py [--config PATH] [--verbose]
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

logger = logging.getLogger("seraph_proxy")

DEFAULT_CONFIG = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "stdio-proxies.json"
)


def load_config(config_path: str) -> dict:
    """Load and validate the stdio-proxies.json config file."""
    path = Path(config_path).resolve()
    if not path.exists():
        logger.warning("Config file not found: %s — no proxies to start", path)
        return {}

    with open(path) as f:
        data = json.load(f)

    proxies = data.get("proxies", {})
    if not proxies:
        logger.info("No proxies defined in config")
        return {}

    return proxies


def validate_config(proxies: dict) -> dict:
    """Validate config and return only enabled, valid entries."""
    enabled = {}
    seen_ports: dict[int, str] = {}

    for name, cfg in proxies.items():
        if not cfg.get("enabled", False):
            logger.info("Skipping disabled proxy: %s", name)
            continue

        # Required fields
        if "command" not in cfg:
            logger.error("Proxy %r missing required field 'command' — skipping", name)
            continue
        if "port" not in cfg:
            logger.error("Proxy %r missing required field 'port' — skipping", name)
            continue

        port = cfg["port"]
        if not isinstance(port, int) or port < 1024 or port > 65535:
            logger.error("Proxy %r has invalid port %r — skipping", name, port)
            continue

        if port in seen_ports:
            logger.error(
                "Port %d conflict: %r and %r — skipping %r",
                port, seen_ports[port], name, name,
            )
            continue

        seen_ports[port] = name
        enabled[name] = cfg

    return enabled


async def run_proxy(
    name: str,
    cfg: dict,
    stop_event: asyncio.Event,
    verbose: bool,
) -> None:
    """Run a single stdio-to-HTTP proxy with retry logic."""
    from fastmcp import Client, FastMCP
    from fastmcp.client.transports import StdioTransport

    command = cfg["command"]
    args = cfg.get("args", [])
    port = cfg["port"]
    env = cfg.get("env", {})

    # Build environment: inherit os.environ, overlay config env vars
    proc_env = dict(os.environ)
    proc_env.update(env)

    backoff = 5
    max_backoff = 60

    while not stop_event.is_set():
        try:
            logger.info(
                "Starting proxy %r: %s %s → localhost:%d",
                name, command, " ".join(args), port,
            )

            transport = StdioTransport(
                command=command,
                args=args,
                env=proc_env,
            )
            client = Client(transport)

            proxy = FastMCP.as_proxy(client, name=name)

            # Run the proxy server — this blocks until stopped
            # We run it in a task so we can cancel on stop_event
            server_task = asyncio.create_task(
                asyncio.to_thread(
                    proxy.run,
                    transport="streamable-http",
                    host="0.0.0.0",
                    port=port,
                )
            )

            # Wait for either the server to finish or stop signal
            stop_task = asyncio.create_task(stop_event.wait())
            done, pending = await asyncio.wait(
                [server_task, stop_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

            if stop_event.is_set():
                logger.info("Proxy %r stopping (shutdown signal)", name)
                return

            # Server exited unexpectedly
            logger.warning("Proxy %r exited unexpectedly — retrying in %ds", name, backoff)

        except Exception:
            logger.exception("Proxy %r crashed — retrying in %ds", name, backoff)

        # Wait before retry (interruptible by stop_event)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            return  # stop_event was set during wait
        except asyncio.TimeoutError:
            pass  # timeout elapsed, retry

        backoff = min(backoff * 2, max_backoff)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Seraph stdio-to-HTTP MCP proxy")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"Config file path (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
    )
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    # Suppress noisy library logs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # Load config
    proxies = load_config(args.config)
    if not proxies:
        logger.info("No proxies configured — exiting")
        return

    enabled = validate_config(proxies)
    if not enabled:
        logger.info("No enabled proxies — exiting")
        return

    # Signal handling
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown(sig: int) -> None:
        logger.info("Received %s — shutting down", signal.Signals(sig).name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    logger.info(
        "Seraph stdio proxy started — %d proxy(ies): %s",
        len(enabled),
        ", ".join(f"{n} (:{c['port']})" for n, c in enabled.items()),
    )

    # Launch all proxies concurrently
    tasks = [
        asyncio.create_task(run_proxy(name, cfg, stop_event, args.verbose))
        for name, cfg in enabled.items()
    ]

    # Wait for stop signal
    await stop_event.wait()

    # Cancel all proxy tasks
    for t in tasks:
        t.cancel()

    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    logger.info("All proxies stopped")


if __name__ == "__main__":
    asyncio.run(main())
