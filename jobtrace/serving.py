"""Loopback-only background servers for demo and tests."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from flask import Flask
from werkzeug.serving import make_server


@dataclass
class RunningServer:
    server: object
    thread: threading.Thread
    host: str
    port: int

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)


def start_server(app: Flask, host: str = "127.0.0.1", port: int = 8765) -> RunningServer:
    if host != "127.0.0.1":
        raise ValueError("background demo servers must bind to 127.0.0.1")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    server = make_server(host, port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, name=f"jobtrace-{port}", daemon=True)
    thread.start()
    return RunningServer(server=server, thread=thread, host=host, port=int(server.server_port))


def wait_forever() -> None:
    event = threading.Event()
    try:
        event.wait()
    except KeyboardInterrupt:
        return
