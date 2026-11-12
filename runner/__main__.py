"""Run scripted McDelivery episodes against Iris and write local evidence."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx

from runner.clients import Iris, Sandbox
from runner.episode import run_episode
from runner.scenarios import SCENARIOS
from sandbox.storage import write_atomic


def save(directory: Path, evidence: dict) -> None:
    directory.mkdir()
    snapshot = evidence["sandbox"]
    if snapshot is not None:
        lines = (json.dumps(event, ensure_ascii=False) + "\n" for event in snapshot["calls"])
        write_atomic(directory / "events.jsonl", lines)
    write_atomic(directory / "evidence.json", (json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",))


def source_revision() -> dict | None:
    try:
        settings = {"cwd": Path(__file__).resolve().parents[1], "capture_output": True, "text": True,
                    "check": True, "timeout": 3}
        revision = subprocess.run(["git", "rev-parse", "HEAD"], **settings).stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain"], **settings).stdout
        return {"commit": revision, "workingTreeDirty": bool(status.strip())}
    except (OSError, subprocess.SubprocessError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenarios", nargs="*", metavar="SCENARIO", help=", ".join(SCENARIOS))
    parser.add_argument("--iris", default="http://127.0.0.1:8080/api")
    parser.add_argument("--sandbox", default="http://127.0.0.1:8100")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--iris-revision", help="Iris build revision to include in the evidence")
    parser.add_argument("--out", type=Path, default=Path("runs"))
    args = parser.parse_args()
    names = args.scenarios or list(SCENARIOS)
    if unknown := set(names) - SCENARIOS.keys():
        parser.error(f"unknown scenarios: {', '.join(sorted(unknown))}")
    admin_token = os.environ.get("SANDBOX_ADMIN_TOKEN")
    mcp_token = os.environ.get("MCD_MCP_TOKEN")
    if not admin_token or not mcp_token:
        parser.error("set SANDBOX_ADMIN_TOKEN and MCD_MCP_TOKEN")
    batch = args.out / uuid4().hex
    batch.mkdir(parents=True)
    source = source_revision()
    passed = True
    with (
        httpx.Client(base_url=args.iris.rstrip("/"), timeout=httpx.Timeout(10, read=300), trust_env=False) as iris_http,
        httpx.Client(base_url=args.sandbox.rstrip("/") + "/admin", timeout=10, trust_env=False,
                     headers={"Authorization": f"Bearer {admin_token}"}) as sandbox_http,
    ):
        iris, sandbox = Iris(iris_http), Sandbox(sandbox_http, mcp_token)
        for index, name in enumerate(names, start=1):
            evidence = run_episode(name, iris, sandbox, seed=args.seed)
            evidence["provenance"].update(runnerSource=source, irisRevision=args.iris_revision)
            directory = batch / f"{index:02d}-{name}"
            save(directory, evidence)
            verdict = evidence["verdict"]["status"]
            print(f"{name} {verdict} {directory.resolve()}")
            if evidence["status"] == "interrupted":
                return 130
            passed &= verdict == "passed"
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
