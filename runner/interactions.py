"""Map observed intents to scenario-owned simulated user replies."""

import re
from dataclasses import replace

from monitor import Message, Observation, Recognizer
from runner.scenarios import INTENTS
from runner.user import is_question

HISTORY_TURNS = 6
HISTORY_MESSAGE_CHARS = 1800
REPLY_CHARS = 6000
LEGACY_REPLY = "用现有的公司地址、默认搭配。请按我刚才提出的要求继续。"
POLICY_VERSION = 2
KEYWORDS = {
    "request_delivery_address": re.compile(r"地址|送到哪里|配送地点|送达地点|送哪|送到哪"),
    "confirm_delivery_address": re.compile(r"地址|送到哪里|配送地点|送达地点|送哪|送到哪|送到公司|送到家"),
    "request_meal_preference": re.compile(r"搭配|套餐偏好|饮料|喝什么|喝哪"),
}


def select_actions(actions: list[dict], intent_ids: set[str]) -> list[dict]:
    return [action for action in actions if intent_ids.intersection(action["intentIds"])]


def keyword_actions(reply: str, actions: list[dict]) -> list[dict]:
    if not is_question(reply):
        return []
    matches = {intent_id for intent_id, pattern in KEYWORDS.items() if pattern.search(reply)}
    return select_actions(actions, matches)


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
    selected = select_actions(actions, matched)
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
