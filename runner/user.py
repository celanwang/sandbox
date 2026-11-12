"""Detect a follow-up question in an Iris reply."""

import re

QUESTION_WORDS = (
    "请问", "请确认", "是否", "需要我", "要不要", "您想", "您要", "哪个", "哪家", "哪一",
    "确认一下", "可以吗", "好吗", "吗？", "呢？",
)
_PENDING_LINE = re.compile(r"^(待确认：|请通过聊天输入框上方).*$", re.M)
_URL = re.compile(r"https?://\S+")


def is_question(text: str) -> bool:
    text = _URL.sub("", _PENDING_LINE.sub("", text))
    return "？" in text or "?" in text or any(word in text for word in QUESTION_WORDS)
