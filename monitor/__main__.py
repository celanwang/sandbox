"""Replay recorded intent decisions without invoking Iris or Sandbox."""

import argparse
import json
from contextlib import nullcontext
from pathlib import Path

from monitor import Decision, Observation
from monitor.qwen import QwenRecognizer, api_key, model_client
from runner.interactions import LEGACY_REPLY, POLICY_VERSION, keyword_actions, select_actions


def replay(path: Path, recognizer: QwenRecognizer | None) -> list[dict]:
    evidence = json.loads(path.read_text(encoding="utf-8"))
    rows = [row for row in evidence["transcript"] if "messageId" in row]
    transcript = {row["messageId"]: (index, row) for index, row in enumerate(rows)}
    if len(transcript) != len(rows):
        raise ValueError("duplicate transcript message ID")
    monitor = evidence["monitor"]
    version = monitor.get("policyVersion", 1)
    if version not in (1, POLICY_VERSION):
        raise ValueError("unsupported interaction policy version")
    actions = evidence["provenance"]["scenario"].get("interactions", [])
    disclosed = {}
    previous_index = -1
    reports = []
    for record in monitor["decisions"]:
        observation = Observation.from_dict(record["input"])
        if observation.reply.messageId != record["messageId"] or observation.digest() != record["inputSha256"]:
            raise ValueError(f"recorded input does not match its fingerprint for {record['messageId']}")
        current_index, current_reply = transcript[record["messageId"]]
        if current_index <= previous_index or current_reply["role"] != "iris":
            raise ValueError("decisions must refer to Iris replies in transcript order")
        history_indexes = [transcript[message.messageId][0] for message in observation.history]
        if history_indexes != list(range(current_index - len(history_indexes), current_index)):
            raise ValueError("monitor history must be the preceding dialogue")
        previous_index = current_index
        for message in (*observation.history, observation.reply):
            _, original = transcript[message.messageId]
            if original["role"] != message.role or not original["text"].endswith(message.text):
                raise ValueError(f"recorded input does not match transcript message {message.messageId}")
        action = record["action"]
        result = record["recognition"]
        recorded = {"status": result["status"], "matches": result["matches"]}
        if recorded["status"] == "unavailable":
            if recorded["matches"]:
                raise ValueError("an unavailable decision cannot have matches")
        else:
            Decision.model_validate(recorded).verify(observation)
        available = [item for item in actions if item["actionId"] not in disclosed]
        matched = {match["intentId"] for match in recorded["matches"]}
        candidates = {intent_id for item in available for intent_id in item["intentIds"]}
        if {intent.intentId for intent in observation.candidates} != candidates:
            raise ValueError("monitor candidates do not match available scenario actions")
        proposed = select_actions(available, matched)
        if record["proposedActionIds"] != [item["actionId"] for item in proposed]:
            raise ValueError("proposed actions do not match the recognized intents")
        kind = action["kind"]
        expected = []
        expected_text = None
        if version == POLICY_VERSION:
            baseline = keyword_actions(current_reply["text"], available)
            if record["baselineActionIds"] != [item["actionId"] for item in baseline]:
                raise ValueError("baseline actions do not match the recorded Iris reply")
        else:
            baseline = []
        if kind == "scripted_reply" and monitor["mode"] in {"active", "shadow"}:
            expected = proposed if monitor["mode"] == "active" else baseline
            if not expected:
                raise ValueError("scripted reply has no eligible action")
            expected_text = " ".join(item["reply"] for item in expected)
        elif kind == "legacy_reply" and version == 1 and monitor["mode"] == "shadow" and record["legacyWouldReply"]:
            expected_text = LEGACY_REPLY
        elif kind != "stop":
            raise ValueError("invalid recorded action")
        expected_ids = [item["actionId"] for item in expected]
        if action["actionIds"] != expected_ids:
            raise ValueError("recorded action does not match the scenario policy")
        if expected_text is None and ("messageId" in action or action.get("acknowledged")):
            raise ValueError("a stopped decision cannot send a message")
        if expected_text is not None and "messageId" in action:
            sent_index, sent = transcript[action["messageId"]]
            if (sent_index != current_index + 1 or sent["role"] != "user" or sent["text"] != expected_text
                    or sent["round"] != current_reply["round"] + 1):
                raise ValueError("recorded action does not match the next user message")
            if action.get("acknowledged"):
                if (sent_index + 1 >= len(rows) or rows[sent_index + 1]["role"] != "iris"
                        or rows[sent_index + 1]["round"] != sent["round"]):
                    raise ValueError("acknowledged action is missing its Iris reply")
                disclosed.update(dict.fromkeys(expected_ids, sent["round"]))
        elif action.get("acknowledged"):
            raise ValueError("acknowledged action is missing its user message")
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
    if monitor["mode"] != "off" and sorted(disclosed) != sorted(monitor["deliveredActionIds"]):
        raise ValueError("delivered actions do not match replayed decisions")
    if version == POLICY_VERSION and monitor["mode"] != "off" and disclosed != monitor.get("deliveredRounds", {}):
        raise ValueError("delivered rounds do not match replayed decisions")
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
