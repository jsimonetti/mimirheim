"""Quick script to measure CPU utilisation during the 48h prosumer solve."""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import yaml

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import SolveBundle
from mimirheim.core.model_builder import build_and_solve

d = Path("tests/benchmarks/prosumer_ev_48h")
bundle = SolveBundle.model_validate(json.loads((d / "input.json").read_text()))
config = MimirheimConfig.model_validate(yaml.safe_load((d / "config.yaml").read_text()))

pid = os.getpid()
samples: list[float] = []
stop_flag = threading.Event()


def sampler() -> None:
    while not stop_flag.is_set():
        r = subprocess.run(["ps", "-p", str(pid), "-o", "%cpu="], capture_output=True, text=True)
        v = r.stdout.strip()
        if v:
            samples.append(float(v))
        time.sleep(0.25)


t = threading.Thread(target=sampler, daemon=True)
t.start()

t0 = time.perf_counter()
result = build_and_solve(bundle, config)
elapsed = time.perf_counter() - t0

stop_flag.set()
t.join(timeout=1.0)

non_zero = [s for s in samples if s > 0.5]
print(f"Status: {result.solve_status}  Wall time: {elapsed:.2f}s")
print(f"CPU samples (% of 1 core): peak={max(non_zero):.0f}  avg={sum(non_zero)/len(non_zero):.0f}")
print(f"On a 12-core Mac this = peak {max(non_zero)/12:.0f}%  avg {sum(non_zero)/len(non_zero)/12:.0f}% of total")
print(f"First 20 samples: {[round(s) for s in samples[:20]]}")
