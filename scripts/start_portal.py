"""
Helper to launch and manage the Mock Customer Portal process for tests and CLI.
"""
import os
import subprocess
import time
import urllib.request
from typing import Optional


class PortalProcess:
    def __init__(self, port: int = 4000, faults: Optional[list[str]] = None, env_overrides: Optional[dict] = None):
        self.port = port
        self.faults = faults or []
        self.env_overrides = env_overrides or {}
        self.proc: Optional[subprocess.Popen] = None

    def start(self, timeout: float = 10.0) -> str:
        portal_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "portal"))
        env = os.environ.copy()
        env["PORT"] = str(self.port)
        if self.faults:
            env["PORTAL_FAULTS"] = ",".join(self.faults)
        env.update(self.env_overrides)

        self.proc = subprocess.Popen(
            ["node", "server.js"],
            cwd=portal_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        base_url = f"http://127.0.0.1:{self.port}"
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{base_url}/login", timeout=1.0) as resp:
                    if resp.status == 200:
                        return base_url
            except Exception:
                time.sleep(0.2)

        self.stop()
        raise TimeoutError(f"Failed to start portal on {base_url} within {timeout}s")

    def stop(self) -> None:
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2.0)
            except Exception:
                self.proc.kill()
            finally:
                self.proc = None


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
    p = PortalProcess(port=port)
    print(f"Starting portal on port {port}...")
    url = p.start()
    print(f"Portal running at {url}. Press Ctrl+C to exit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        p.stop()
        print("Portal stopped.")

