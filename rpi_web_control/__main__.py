"""Start the Raspberry Pi camera service or its local camera simulator."""

import argparse
import ipaddress
import logging
import os
import secrets
import signal
import ssl
from pathlib import Path

from .application import Application
from .web import Server


def options():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulate", action="store_true", help="Use a generated camera test pattern.")
    parser.add_argument("--host", default=os.getenv("RPI_CONTROL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=os.getenv("RPI_CONTROL_PORT", "8080"))
    parser.add_argument("--state-dir", type=Path, default=os.getenv("RPI_CONTROL_STATE"))
    return parser.parse_args()


def tls_context(host):
    cert = os.getenv("RPI_CONTROL_TLS_CERT")
    key = os.getenv("RPI_CONTROL_TLS_KEY")
    if cert and key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        return context
    if cert or key:
        raise ValueError("Configure both the TLS certificate and private key.")
    if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
        raise ValueError("A network listener requires a TLS certificate and private key.")
    return None


def read_token(path, generate):
    if generate and not path.exists():
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w") as file:
                file.write(secrets.token_urlsafe(32) + "\n")
            logging.info("Development access token saved in %s", path)
    token = path.read_text(encoding="ascii").strip()
    if len(token) < 24 or not token.isprintable():
        raise ValueError("The access token must contain at least 24 printable characters.")
    return token


def configured_token(state, simulate):
    mode = os.getenv("RPI_CONTROL_REQUIRE_TOKEN", "0")
    if mode == "0":
        return None
    if mode != "1":
        raise ValueError("RPI_CONTROL_REQUIRE_TOKEN must be 0 or 1.")
    token_file = os.getenv("RPI_CONTROL_TOKEN_FILE")
    if token_file:
        return read_token(Path(token_file), generate=False)
    path = state / "token" if simulate else Path("/etc/rpi-web-control/token")
    return read_token(path, generate=simulate)


def main():
    args = options()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    state = args.state_dir or (Path(".dev-state") if args.simulate else Path("/var/lib/rpi-web-control"))
    state = state.resolve()
    tls = tls_context(args.host)
    state.mkdir(mode=0o750, parents=True, exist_ok=True)
    token = configured_token(state, args.simulate)
    root_spec = os.getenv("RPI_CONTROL_STORAGE_ROOTS")
    if root_spec is None:
        roots = [state / "recordings"]
        roots[0].mkdir(mode=0o750, exist_ok=True)
    else:
        roots = [Path(item) for item in root_spec.split(os.pathsep) if item]
        if not roots or not all(root.is_absolute() for root in roots):
            raise ValueError("RPI_CONTROL_STORAGE_ROOTS requires absolute directories.")
    required_mounts = [Path(item) for item in os.getenv("RPI_CONTROL_REQUIRED_MOUNTS", "").split(os.pathsep) if item]
    if not all(path.is_absolute() for path in required_mounts):
        raise ValueError("RPI_CONTROL_REQUIRED_MOUNTS requires absolute mount points.")
    app = Application(state, roots, "simulated" if args.simulate else "picamera2", required_mounts=required_mounts)
    try:
        with Server((args.host, args.port), app, token, tls) as server:
            server.timeout = 0.5

            def stop(signum, frame):
                server.stopping.set()

            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, stop)
            logging.info("Listening on %s port %s using %s", args.host, server.server_port,
                         "HTTPS" if tls else "HTTP")
            while not server.stopping.is_set():
                server.handle_request()
    finally:
        app.close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from None
