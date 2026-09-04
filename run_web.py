"""Entry point for the OpenManus web interface.

python run_web.py --host 0.0.0.0 --port 8000
"""

import argparse
import os

import uvicorn

from app.web.server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the OpenManus web interface")
    parser.add_argument(
        "--host",
        default=os.getenv("OPENMANUS_HOST", "127.0.0.1"),
        help="Interface to bind (default: 127.0.0.1, i.e. this machine only). "
        "docker-compose passes --host 0.0.0.0 so the container can be reached "
        "on its published 127.0.0.1 port; only widen this behind a trusted "
        "network, and set OPENMANUS_ALLOWED_HOSTS accordingly.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("OPENMANUS_PORT", "8000")),
        help="Port to listen on (default: 8000)",
    )
    parser.add_argument(
        "--reload", action="store_true", help="Reload on source changes (development)"
    )
    args = parser.parse_args()

    if args.reload:
        uvicorn.run(
            "app.web.server:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=True,
        )
    else:
        uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
