"""Replay recorded intent decisions without invoking Iris or Sandbox."""

import argparse
import json
from contextlib import nullcontext
from pathlib import Path

from monitor import Decision, Observation
from monitor.qwen import QwenRecognizer, api_key, model_client


def replay(path: Path, recognizer: QwenRecognizer | None) -> list[dict]:
    evidence = json.loads(path.read_text(encoding="utf-8"))
    transcript = {row["messageId"]: row for row in evidence["transcript"] if "messageId" in row}
    reports = []
    for record in evidence["monitor"]["decisions"]:
        observation = Observation.from_dict(record["input"])
        if observation.reply.messageId != record["messageId"] or observation.digest() != record["inputSha256"]:
            raise ValueError(f"recorded input does not match its fingerprint for {record['messageId']}")
        for message in (*observation.history, observation.reply):
            original = transcript[message.messageId]
            if original["role"] != message.role or not original["text"].endswith(message.text):
                raise ValueError(f"recorded input does not match transcript message {message.messageId}")
        action = record["action"]
        if action.get("acknowledged") and transcript[action["messageId"]]["role"] != "user":
            raise ValueError("acknowledged action must reference a user message")
        result = record["recognition"]
        recorded = {"status": result["status"], "matches": result["matches"]}
        if recorded["status"] == "unavailable":
            if recorded["matches"]:
                raise ValueError("an unavailable decision cannot have matches")
        else:
            Decision.model_validate(recorded).verify(observation)
        report = {
            "messageId": record["messageId"],
            "recorded": recorded,
            "recordedAction": record["action"],
        }
        if recognizer is not None:
            current = recognizer.recognize(observation)
            report["current"] = current.as_dict()
            report["sameIntents"] = recorded["status"] == report["current"]["status"] and (
                {match["intentId"] for match in recorded["matches"]}
                == {match["intentId"] for match in report["current"]["matches"]}
            )
        reports.append(report)
    return reports


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--reclassify", action="store_true", help="send captured monitor inputs to Qwen again")
    args = parser.parse_args()
    key = api_key() if args.reclassify else ""
    if args.reclassify and not key:
        parser.error("set MONITOR_DASHSCOPE_API_KEY or DASHSCOPE_API_KEY")
    try:
        with (model_client(key) if key else nullcontext(None)) as http:
            reports = replay(args.evidence, QwenRecognizer(http) if http is not None else None)
    except (KeyError, OSError, TypeError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
