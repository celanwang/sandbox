"""Map observed intents to scenario-owned simulated user replies."""

from dataclasses import replace

from monitor import Message, Observation, Recognizer
from runner.scenarios import INTENTS

HISTORY_TURNS = 6
HISTORY_MESSAGE_CHARS = 1800
REPLY_CHARS = 6000
LEGACY_REPLY = "用现有的公司地址、默认搭配。请按我刚才提出的要求继续。"


def observe(recognizer: Recognizer, transcript: list[dict], actions: list[dict]) -> tuple[dict, list[dict]]:
    def message(row: dict, limit: int) -> Message:
        return Message(row["messageId"], row["role"], row["text"][-limit:])

    history = transcript[-HISTORY_TURNS - 1:-1]
    latest = transcript[-1]
    candidates = tuple(INTENTS[intent_id] for intent_id in dict.fromkeys(
        intent_id for action in actions for intent_id in action["intentIds"]
    ))
    observation = Observation(
        reply=message(latest, REPLY_CHARS),
        history=tuple(message(row, HISTORY_MESSAGE_CHARS) for row in history),
        candidates=candidates,
    )
    result = recognizer.recognize(observation)
    if result.decision:
        try:
            result.decision.verify(observation)
        except ValueError:
            result = replace(result, decision=None, error="invalid_output")
    matched = {match.intentId for match in result.decision.matches} if result.decision else set()
    selected = [action for action in actions if matched.intersection(action["intentIds"])]
    record = {
        "messageId": latest["messageId"],
        "input": observation.as_dict(),
        "inputSha256": observation.digest(),
        "contextTruncated": len(transcript) > HISTORY_TURNS + 1 or len(latest["text"]) > REPLY_CHARS
        or any(len(row["text"]) > HISTORY_MESSAGE_CHARS for row in history),
        "recognition": result.as_dict(),
        "proposedActionIds": [action["actionId"] for action in selected],
    }
    return record, selected
