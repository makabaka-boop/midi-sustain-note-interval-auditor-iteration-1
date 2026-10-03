"""命令行入口：stdin 只接收 JSON，stdout 输出 JSON 审计结果。

非法输入（含非 JSON、孤立 NOTE_OFF、重复踏板动作等）整份拒绝：
向 stderr 输出 {"error": ...} 并以退出码 1 结束。
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional

from .audit import AuditError, audit
from .model import AuditResult, Note


def _note_to_dict(note: Note) -> Dict[str, Any]:
    return {
        "id": note.id,
        "channel": note.channel,
        "pitch": note.pitch,
        "key": {
            "start": note.key_on,
            "end": note.key_off,
            "endReason": note.key_off_reason.value,
        },
        "sound": {
            "start": note.sound_start,
            "end": note.sound_end,
            "endReason": note.sound_end_reason.value,
        },
    }


def result_to_dict(result: AuditResult) -> Dict[str, Any]:
    return {
        "notes": [_note_to_dict(n) for n in result.notes],
        "peakConcurrent": result.peak_concurrent,
    }


def main(argv: Optional[List[str]] = None) -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"invalid JSON: {exc}"}, ensure_ascii=False),
              file=sys.stderr)
        return 1

    try:
        result = audit(payload)
    except AuditError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    json.dump(result_to_dict(result), sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
