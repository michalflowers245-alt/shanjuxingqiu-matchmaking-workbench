from __future__ import annotations

import json
import sys


payload = json.load(sys.stdin)
text = str(payload.get("text") or "")
paragraphs = [item for item in text.split("\n") if item.strip()]
result = {
    "characters": len(text),
    "paragraphs": len(paragraphs),
    "has_question": "？" in text or "?" in text,
    "has_clear_action": any(word in text for word in ("回复", "预约", "咨询", "领取", "点击")),
}
json.dump(result, sys.stdout, ensure_ascii=False)

