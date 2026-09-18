"""Local hot-add proof: fake delta, shutdown cancel, no /data writes.

Does not start the production collector, does not write /data/live, and does
not enable VPS flags.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _forbid_data_writes() -> dict[str, object]:
    roots = [Path("/data/live"), Path("/data/spool"), Path("/data/bars"), Path("/data")]
    return {
        name: {"exists": path.exists(), "path": str(path)}
        for name, path in (
            ("live", roots[0]),
            ("spool", roots[1]),
            ("bars", roots[2]),
            ("data", roots[3]),
        )
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Local D0/D1 hot-add checks")
    parser.add_argument(
        "--live-rest",
        action="store_true",
        help="Also hit public Bybit/OKX REST into a tmp delta (optional)",
    )
    args = parser.parse_args()
    before = _forbid_data_writes()
    tests = [
        "tests/test_universe_delta.py",
        "tests/test_hot_add_supervisor.py",
        "tests/test_universe_discovery.py",
    ]
    cmd = [sys.executable, "-m", "unittest", *tests]
    print("running", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=str(REPO), check=False)
    compile_targets = [
        "app/screaner_b_o.py",
        "app/utils/hot_add.py",
        "app/utils/task_supervisor.py",
        "app/utils/universe_delta.py",
        "app/discovery/intersection.py",
        "app/discovery/__main__.py",
    ]
    compile_proc = subprocess.run(
        [sys.executable, "-m", "py_compile", *compile_targets],
        cwd=str(REPO),
        check=False,
    )
    after = _forbid_data_writes()
    live_summary = None
    live_rc = 0
    if args.live_rest:
        import tempfile

        from app.discovery.intersection import run_discovery

        tmp = Path(tempfile.mkdtemp(prefix="spread-discovery-"))
        delta = tmp / "hot_add_delta.csv"
        try:
            live_summary = run_discovery(
                universe_path=REPO / "bybit_okx_universe.csv",
                delta_path=delta,
                max_new=8,
            )
            print(json.dumps({"live_rest": live_summary}, ensure_ascii=False, indent=2))
        except Exception as exc:  # noqa: BLE001 — report, do not write /data
            live_rc = 1
            live_summary = {"error": str(exc)}
            print(json.dumps({"live_rest_failed": live_summary}, ensure_ascii=False))
    result = {
        "unittest_exit": proc.returncode,
        "py_compile_exit": compile_proc.returncode,
        "data_paths_before": before,
        "data_paths_after": after,
        "data_paths_unchanged": before == after,
        "live_rest": live_summary,
        "note": (
            "Local proof only. VPS SPREAD_HOT_ADD stays off. "
            "Do not restart spread-collector. Do not start a second collector on /data/live."
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if proc.returncode != 0 or compile_proc.returncode != 0:
        return 1
    if before != after:
        return 1
    return live_rc


if __name__ == "__main__":
    raise SystemExit(main())
