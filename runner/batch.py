"""Batch collection: plan, freeze task definitions, and aggregate a batch manifest."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import yaml

from runner.tasks import TASKS_DIR, TaskSpec, load_tasks


def new_batch_id(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return f"b-{moment:%Y%m%d}-{uuid4().hex[:4]}"


@dataclass(frozen=True)
class BatchPlan:
    batch_id: str
    name: str
    task_set_id: str
    tasks: tuple[str, ...]
    seed: int | None = None


def load_plan(path: Path) -> BatchPlan:
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return BatchPlan(
        batch_id=str(config.get("batchId") or new_batch_id()),
        name=str(config.get("name") or path.stem),
        task_set_id=str(config.get("taskSetId") or path.stem),
        tasks=tuple(config.get("tasks") or ()),
        seed=config.get("seed"),
    )


def _spec_payload(spec: TaskSpec) -> dict:
    return spec.model_dump(mode="json", by_alias=True, exclude_none=True)


def prepare(plan: BatchPlan, tasks: dict[str, TaskSpec], out_root: Path) -> dict:
    """创建批次目录，冻结任务定义，写入初始清单。

    计划未显式列出任务时，使用任务集目录里的全部任务。
    """
    task_ids = plan.tasks or tuple(sorted(tasks))
    if not task_ids:
        raise ValueError("task set has no tasks")
    unknown = [task_id for task_id in task_ids if task_id not in tasks]
    if unknown:
        raise ValueError(f"unknown tasks: {', '.join(unknown)}")
    batch_dir = out_root / plan.batch_id
    specs_dir = batch_dir / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for task_id in task_ids:
        spec = tasks[task_id]
        (specs_dir / f"{task_id}.yaml").write_text(
            yaml.safe_dump(_spec_payload(spec), allow_unicode=True, sort_keys=False),
            encoding="utf-8")
        entries.append({"taskId": task_id, "specSha256": spec.digest()})
    manifest = {
        "batchId": plan.batch_id,
        "name": plan.name,
        "taskSetId": plan.task_set_id,
        "seed": plan.seed,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "tasks": entries,
        "runs": [],
        "counts": {"planned": len(task_ids), "completed": 0, "passed": 0,
                   "failed": 0, "unavailable": 0, "errored": 0},
    }
    _write(batch_dir / "batch.json", manifest)
    return manifest


def aggregate(batch_dir: Path) -> dict:
    """汇总各局 run.json，更新批次清单。"""
    manifest_path = batch_dir / "batch.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"batch manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runs = []
    for run_dir in sorted(batch_dir.iterdir()):
        if not run_dir.is_dir() or run_dir.name == "specs":
            continue
        run_path = run_dir / "run.json"
        if not run_path.is_file():
            runs.append({"runId": run_dir.name, "status": "missing-run-json"})
            continue
        runs.append(json.loads(run_path.read_text(encoding="utf-8")))
    counts = {
        "planned": len(manifest["tasks"]),
        "completed": len(runs),
        "passed": sum(1 for run in runs if run.get("verdict") == "passed"),
        "failed": sum(1 for run in runs if run.get("verdict") == "failed"),
        "unavailable": sum(1 for run in runs if run.get("verdict") == "unavailable"),
        "errored": sum(1 for run in runs if run.get("status") == "error"),
    }
    manifest["runs"] = runs
    manifest["counts"] = counts
    manifest["updatedAt"] = datetime.now(timezone.utc).isoformat()
    _write(manifest_path, manifest)
    return manifest


def run_manifest(run_id: str, task: TaskSpec, evidence: dict) -> dict:
    verdict = (evidence.get("verdict") or {}).get("status")
    checks = (evidence.get("verdict") or {}).get("checks") or {}
    provenance = evidence.get("provenance") or {}
    return {
        "runId": run_id,
        "taskId": task.taskId,
        "verdict": verdict,
        "status": evidence.get("status"),
        "failedChecks": sorted(name for name, ok in checks.items() if ok is False),
        "specSha256": provenance.get("specSha256"),
        "envId": provenance.get("envId"),
        "seed": evidence.get("seed"),
        "startedAt": evidence.get("startedAt"),
        "finishedAt": evidence.get("finishedAt"),
    }


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runner.batch", description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)

    prepare_parser = sub.add_parser("prepare", help="创建批次目录并冻结任务定义")
    prepare_parser.add_argument("--plan", type=Path, required=True)
    prepare_parser.add_argument("--tasks-dir", type=Path, default=TASKS_DIR)
    prepare_parser.add_argument("--out", type=Path, default=Path("runs"))

    aggregate_parser = sub.add_parser("aggregate", help="汇总批次内各局结果")
    aggregate_parser.add_argument("--batch", type=Path, required=True,
                                  help="批次目录，例如 runs/b-20261119-abcd")

    args = parser.parse_args(argv)
    if args.action == "prepare":
        plan = load_plan(args.plan)
        manifest = prepare(plan, load_tasks(args.tasks_dir), args.out)
        print(f"{plan.batch_id} planned={manifest['counts']['planned']} -> {args.out / plan.batch_id}")
    else:
        manifest = aggregate(args.batch)
        print(json.dumps(manifest["counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
