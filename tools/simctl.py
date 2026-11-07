"""Start/stop a simulator process for local runs, tests and the crash-recovery demo.

    with Sim("scenarios/public_01_dev.yaml", "runs/mine01") as sim:
        print(sim.url)

Cross-platform (Windows + Linux). Uses only the documented CLI (`python -m sim serve`).
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Sim:
    def __init__(self, scenario: str, run_dir: str, port: int | None = None, log: str | None = None):
        self.scenario, self.run_dir = scenario, run_dir
        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.log = log or f"{run_dir}.sim.log"
        self.proc: subprocess.Popen | None = None

    def start(self, timeout: float = 900) -> "Sim":
        Path(self.log).parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "PYTHONUTF8": "1"}
        self._fh = open(self.log, "w", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-m", "sim", "serve", "--scenario", self.scenario,
                                      "--run-dir", self.run_dir, "--port", str(self.port), "--force"],
                                     cwd=ROOT, stdout=self._fh, stderr=subprocess.STDOUT, env=env)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f"simulator exited early; see {self.log}")
            try:
                if httpx.get(self.url + "/health", timeout=2).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise TimeoutError("simulator did not come up")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if getattr(self, "_fh", None):
            self._fh.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--port", type=int, default=None)
    a = ap.parse_args()
    s = Sim(a.scenario, a.run_dir, a.port).start()
    print(s.url, flush=True)
    try:
        s.proc.wait()
    except KeyboardInterrupt:
        s.stop()
