"""Build Iris once and run the scripted Sandbox scenarios against fresh Iris processes."""

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import re
from secrets import token_hex
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep

import httpx

from monitor.qwen import api_key
from runner.__main__ import run_case, save, source_revision
from runner.report import save_report, summarize
from runner.scenarios import SCENARIOS


SANDBOX_ROOT = Path(__file__).resolve().parents[1]
SANDBOX_PORT = re.compile(r"Uvicorn running on http://127\.0\.0\.1:(\d+)")
IRIS_PORT = re.compile(r"Tomcat started on port (\d+)")
IRIS_PROBE = "/api/agent/state?userId=regression-health&sessionId=regression-health"


@contextmanager
def running(command, *, cwd: Path, env: dict, log: Path):
    with log.open("wb") as output:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def wait_ready(process, log: Path, pattern: re.Pattern, path: str, *, headers=None) -> str:
    deadline = monotonic() + 90
    address = None
    with httpx.Client(timeout=1, trust_env=False) as client:
        while monotonic() < deadline and process.poll() is None:
            if address is None:
                match = pattern.search(log.read_text(encoding="utf-8", errors="replace"))
                if match:
                    address = f"http://127.0.0.1:{match[1]}"
            if address is not None:
                try:
                    if client.get(address + path, headers=headers).status_code == 200:
                        return address
                except httpx.TransportError:
                    pass
            sleep(0.2)
    raise RuntimeError(f"Service did not become ready. See {log}.")


def check_dependency(label: str, port: int) -> None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            pass
    except OSError as error:
        raise RuntimeError(f"{label} is unavailable at 127.0.0.1:{port}.") from error


def build_iris(root: Path, log: Path) -> Path:
    with log.open("wb") as output:
        result = subprocess.run(["mvn", "-B", "-pl", ":iris-app", "-am", "-DskipTests", "package"],
                                cwd=root, stdout=output, stderr=subprocess.STDOUT, timeout=900)
    jars = sorted((root / "iris-app" / "target").glob("iris-app-*.jar"))
    if result.returncode or len(jars) != 1:
        raise RuntimeError(f"Could not build a unique Iris application JAR. See {log}.")
    return jars[0]


def run_suite(args, batch: Path, key: str) -> int:
    iris_root = args.iris_repo.resolve()
    source = source_revision()
    admin_token = token_hex(24)
    env = dict(os.environ, SANDBOX_ADMIN_TOKEN=admin_token)
    iris_env = dict(env, DB_URL=f"jdbc:postgresql://127.0.0.1:{args.pg_port}/{args.database}",
                    DB_USERNAME=args.db_user, DB_PASSWORD=os.environ.get("IRIS_REGRESSION_DB_PASSWORD", ""),
                    IRIS_REDIS_HOST="127.0.0.1", IRIS_REDIS_PORT=str(args.redis_port),
                    IRIS_REDIS_PASSWORD=os.environ.get("IRIS_REGRESSION_REDIS_PASSWORD", ""),
                    SPRING_DATA_REDIS_DATABASE=str(args.redis_db), IRIS_AUTO_REVIEW="false",
                    IRIS_SKILLS_DIR=str(iris_root / "skills"), SPRING_SQL_INIT_MODE="always",
                    SPRING_SQL_INIT_SCHEMA_LOCATIONS=(iris_root / "docs/schema.sql").as_uri(),
                    SPRING_SQL_INIT_ENCODING="UTF-8",
                    SPRING_CONFIG_IMPORT=f"optional:{(iris_root / '.env').as_uri()}[.properties]")
    report = {"sandboxSource": source, "seed": args.seed, "requestedScenarios": args.scenarios,
              "cases": [], "status": "running"}
    report_path = batch / "summary.json"
    save_report(report_path, report)
    try:
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=iris_root, text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=iris_root, text=True).strip())
        report.update(irisRevision=revision, irisWorkingTreeDirty=dirty)
        save_report(report_path, report)
        check_dependency("PostgreSQL", args.pg_port)
        check_dependency("Redis", args.redis_port)
        jar = build_iris(iris_root, batch / "build.log")
        sandbox_log = batch / "sandbox.log"
        with running([sys.executable, "-m", "sandbox.server", "--port", "0"],
                     cwd=SANDBOX_ROOT, env=env, log=sandbox_log) as sandbox:
            sandbox_url = wait_ready(sandbox, sandbox_log, SANDBOX_PORT, "/admin/catalog",
                                     headers={"Authorization": f"Bearer {admin_token}"})
            for index, scenario in enumerate(args.scenarios, 1):
                label = f"{index:02d}-{scenario}"
                episode_id, mcp_token = token_hex(16), token_hex(24)
                mode = "active" if SCENARIOS[scenario].get("interactions") else "off"
                case_env = dict(iris_env, MCD_MCP_URL=f"{sandbox_url}/mcp/{episode_id}/1", MCD_MCP_TOKEN=mcp_token)
                log = batch / f"{label}.log"
                print(f"Running {scenario} with Monitor {mode}", flush=True)
                try:
                    with TemporaryDirectory(prefix="iris-regression-") as work, running(
                        ["java", "-Dfile.encoding=UTF-8", "-jar", str(jar), "--server.address=127.0.0.1",
                         "--server.port=0", "--iris.memory.daily-note.cron=-"],
                        cwd=Path(work), env=case_env, log=log,
                    ) as iris:
                        iris_url = wait_ready(iris, log, IRIS_PORT, IRIS_PROBE)
                        evidence = run_case(scenario, iris_url=iris_url + "/api", sandbox_url=sandbox_url,
                                            admin_token=admin_token, mcp_token=mcp_token, episode_id=episode_id,
                                            seed=args.seed, monitor_mode=mode, key=key if mode == "active" else "")
                        evidence["provenance"].update(irisRevision=revision, irisWorkingTreeDirty=dirty,
                                                      runnerSource=source)
                        save(batch / label, evidence)
                except (OSError, RuntimeError) as error:
                    report["cases"].append({"scenario": scenario, "verdict": "unavailable", "status": "setup_error",
                                            "errors": {"setup": str(error)}, "log": log.name})
                    save_report(report_path, report)
                    print(f"{scenario}: unavailable. See {log}.", file=sys.stderr, flush=True)
                    if sandbox.poll() is not None:
                        raise RuntimeError(f"Sandbox stopped during the suite. See {sandbox_log}.") from error
                    continue
                summary = summarize(evidence)
                report["cases"].append({field: summary[field] for field in
                                        ("scenario", "verdict", "status", "failedChecks", "errors", "diagnostics")}
                                       | {"evidence": f"{label}/evidence.json", "summary": f"{label}/summary.json"})
                save_report(report_path, report)
                print(f"{scenario}: {summary['verdict']} {' '.join(summary['failedChecks'])}".rstrip(), flush=True)
                if evidence["status"] == "interrupted":
                    raise KeyboardInterrupt
        verdicts = {case["verdict"] for case in report["cases"]}
        report["status"] = "failed" if "failed" in verdicts else "unavailable" if "unavailable" in verdicts else "passed"
        return 0 if report["status"] == "passed" else 1
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        return 130
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        report.update(status="unavailable", setupError=str(error))
        print(error, file=sys.stderr)
        return 1
    finally:
        save_report(report_path, report)
        print(f"Regression summary: {report_path}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenarios", nargs="*", metavar="SCENARIO", help="default: all eight scenarios")
    parser.add_argument("--iris-repo", type=Path, required=True)
    parser.add_argument("--pg-port", type=int, default=54328)
    parser.add_argument("--database", default="iris_eval")
    parser.add_argument("--db-user", default="postgres")
    parser.add_argument("--redis-port", type=int, default=59000)
    parser.add_argument("--redis-db", type=int, choices=range(16), default=13, metavar="0..15")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, default=Path("runs/regression"))
    args = parser.parse_args()
    args.scenarios = args.scenarios or list(SCENARIOS)
    for scenario in args.scenarios:
        if scenario not in SCENARIOS:
            parser.error(f"unknown scenario {scenario!r}; choose from {', '.join(SCENARIOS)}")
    if not (args.iris_repo / "docs/schema.sql").is_file() or not (args.iris_repo / "pom.xml").is_file():
        parser.error("--iris-repo must point to an Iris checkout")
    if not shutil.which("java") or not shutil.which("mvn"):
        parser.error("Java and Maven must be available on PATH")
    if any(not 1 <= port <= 65535 for port in (args.pg_port, args.redis_port)):
        parser.error("PostgreSQL and Redis ports must be between 1 and 65535")
    if not re.fullmatch(r"[A-Za-z0-9_]+", args.database):
        parser.error("--database must contain only letters, digits, or underscores")
    key = api_key()
    if any(SCENARIOS[case].get("interactions") for case in args.scenarios) and not key:
        parser.error("set MONITOR_DASHSCOPE_API_KEY or DASHSCOPE_API_KEY for the clarification scenarios")
    batch = (args.out / token_hex(16)).resolve()
    batch.parent.mkdir(parents=True, exist_ok=True)
    batch.mkdir(mode=0o700)
    return run_suite(args, batch, key)


if __name__ == "__main__":
    raise SystemExit(main())
