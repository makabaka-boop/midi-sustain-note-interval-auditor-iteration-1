"""事件流校验与审计核心逻辑。

语义约定：
- 事件按 time 非降序排列；同一时刻的事件严格按输入顺序执行。
- NOTE_OFF 释放该通道该音高“最早仍按住”的 NOTE_ON（同音重叠时排队配对）。
- 普通踏板（PEDAL_DOWN/UP）踩下期间 NOTE_OFF 只释放琴键，音符转入延音；
  抬踏板时结束该通道全部已由它保持的已松键音符，仍按住的音符不受影响。
- 中踏板（SOSTENUTO_DOWN/UP，每通道独立）在踩下瞬间只捕获该通道当时仍被
  琴键按住的音符实例：此后新弹的音、以及此前已松键仅靠普通踏板发声的音
  都不被捕获。被捕获的音符松键后由中踏板继续保持，直到 SOSTENUTO_UP。
- 两种踏板都只保持各自捕获的音符，互不追溯：普通踏板不接手已靠中踏板
  发声的音，中踏板也不捕获已靠普通踏板发声的音。一个已松键音符可同时
  被两者保持；只要任一保持仍生效就继续发声，两种保持都解除时才结束，
  结束原因由后处理的那次抬踏板决定。
- endTime 不早于末事件；所有剩余发声音符在 endTime 截断并标记原因。
- 孤立 NOTE_OFF、重复踏板动作等任何非法情况均整份拒绝（抛 AuditError）。
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Tuple

from .model import (
    AuditResult,
    Event,
    EventType,
    KeyOffReason,
    Note,
    SoundEndReason,
)

MAX_EVENTS = 5000
CHANNEL_RANGE = range(0, 16)
PITCH_RANGE = range(0, 128)


class AuditError(ValueError):
    """输入非法，整份拒绝。"""


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _is_int_in(x: Any, lo: int, hi: int) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and lo <= x <= hi


def _parse_events(raw: Any) -> List[Event]:
    if not isinstance(raw, list):
        raise AuditError('"events" must be a JSON array')
    if not 1 <= len(raw) <= MAX_EVENTS:
        raise AuditError(
            f'"events" must contain 1..{MAX_EVENTS} events, got {len(raw)}'
        )

    events: List[Event] = []
    prev_time = None
    for i, item in enumerate(raw):
        where = f"events[{i}]"
        if not isinstance(item, dict):
            raise AuditError(f"{where}: must be an object")

        t = item.get("time")
        if not _is_number(t):
            raise AuditError(f"{where}.time: must be a number")
        if prev_time is not None and t < prev_time:
            raise AuditError(
                f"{where}.time: events must be in non-decreasing time order"
            )
        prev_time = t

        try:
            etype = EventType(item.get("type"))
        except ValueError:
            raise AuditError(
                f"{where}.type: unknown event type {item.get('type')!r}"
            ) from None

        ch = item.get("channel")
        if not _is_int_in(ch, 0, 15):
            raise AuditError(f"{where}.channel: must be an integer in 0..15")

        pitch = None
        if etype in (EventType.NOTE_ON, EventType.NOTE_OFF):
            pitch = item.get("pitch")
            if not _is_int_in(pitch, 0, 127):
                raise AuditError(f"{where}.pitch: must be an integer in 0..127")

        events.append(Event(time=t, type=etype, channel=ch, pitch=pitch))
    return events


def _peak_concurrent(notes: List[Note]) -> int:
    """发声区间为左闭右开 [start, end)：同一时刻先结算结束再计入开始，
    用扫描线净增量天然满足该语义；零时长音符不影响峰值。"""
    deltas: Dict[Any, int] = defaultdict(int)
    for n in notes:
        deltas[n.sound_start] += 1
        deltas[n.sound_end] -= 1
    peak = current = 0
    for t in sorted(deltas):
        current += deltas[t]
        if current > peak:
            peak = current
    return peak


def audit(payload: Any) -> AuditResult:
    """审计一份 JSON 负载，非法输入抛 AuditError（整份拒绝）。"""
    if not isinstance(payload, dict):
        raise AuditError('input must be a JSON object with "events" and "endTime"')

    events = _parse_events(payload.get("events"))

    end_time = payload.get("endTime")
    if not _is_number(end_time):
        raise AuditError('"endTime" must be a number')
    if end_time < events[-1].time:
        raise AuditError('"endTime" must not be earlier than the last event time')

    notes: List[Note] = []
    held: Dict[Tuple[int, int], Deque[Note]] = defaultdict(deque)  # 仍按住
    sustained: Dict[int, List[Note]] = defaultdict(list)  # 已松键、普通踏板保持中
    captured: Dict[int, List[Note]] = defaultdict(list)   # 被中踏板锁住的音符
    pedal_down = [False] * 16
    sostenuto_down = [False] * 16

    for ev in events:
        if ev.type is EventType.NOTE_ON:
            note = Note(id=len(notes), channel=ev.channel, pitch=ev.pitch,
                        key_on=ev.time)
            notes.append(note)
            held[(ev.channel, ev.pitch)].append(note)

        elif ev.type is EventType.NOTE_OFF:
            queue = held[(ev.channel, ev.pitch)]
            if not queue:
                raise AuditError(
                    f"orphan NOTE_OFF at time {ev.time} on channel {ev.channel}"
                    f" pitch {ev.pitch}: no held NOTE_ON to release"
                )
            note = queue.popleft()  # 最早仍按住的同音 NOTE_ON
            note.key_off = ev.time
            note.key_off_reason = KeyOffReason.NOTE_OFF
            note.pedal_held = pedal_down[ev.channel]
            if note.pedal_held:
                sustained[ev.channel].append(note)  # 普通踏板接手保持
            if not note.pedal_held and not note.sostenuto_held:
                note.sound_end = ev.time            # 无任何保持 → 立即结束
                note.sound_end_reason = SoundEndReason.NOTE_OFF

        elif ev.type is EventType.PEDAL_DOWN:
            if pedal_down[ev.channel]:
                raise AuditError(
                    f"repeated PEDAL_DOWN at time {ev.time} on channel {ev.channel}"
                )
            pedal_down[ev.channel] = True

        elif ev.type is EventType.PEDAL_UP:
            if not pedal_down[ev.channel]:
                raise AuditError(
                    f"PEDAL_UP at time {ev.time} on channel {ev.channel}"
                    " while pedal is not down"
                )
            pedal_down[ev.channel] = False
            for note in sustained[ev.channel]:
                note.pedal_held = False
                if not note.sostenuto_held:  # 中踏板仍锁住 → 继续发声
                    note.sound_end = ev.time
                    note.sound_end_reason = SoundEndReason.PEDAL_UP
            sustained[ev.channel].clear()

        elif ev.type is EventType.SOSTENUTO_DOWN:
            if sostenuto_down[ev.channel]:
                raise AuditError(
                    f"repeated SOSTENUTO_DOWN at time {ev.time}"
                    f" on channel {ev.channel}"
                )
            sostenuto_down[ev.channel] = True
            # 只捕获此刻仍被琴键按住的音符实例；已松键（仅靠普通踏板发声）
            # 的和此后新弹的都不被捕获。
            for (ch, _pitch), queue in held.items():
                if ch != ev.channel:
                    continue
                for note in queue:
                    note.sostenuto_held = True
                    captured[ev.channel].append(note)

        elif ev.type is EventType.SOSTENUTO_UP:
            if not sostenuto_down[ev.channel]:
                raise AuditError(
                    f"SOSTENUTO_UP at time {ev.time} on channel {ev.channel}"
                    " while sostenuto is not down"
                )
            sostenuto_down[ev.channel] = False
            for note in captured[ev.channel]:
                note.sostenuto_held = False
                # 已松键且普通踏板未保持 → 由这次抬中踏板结束；
                # 仍按住或仍被普通踏板保持的音符不受影响。
                if note.key_off is not None and not note.pedal_held:
                    note.sound_end = ev.time
                    note.sound_end_reason = SoundEndReason.SOSTENUTO_UP
            captured[ev.channel].clear()

    # endTime 截断：仍未松键 / 仍在发声的音符
    for note in notes:
        if note.key_off is None:
            note.key_off = end_time
            note.key_off_reason = KeyOffReason.END_TIME
        if note.sound_end is None:
            note.sound_end = end_time
            note.sound_end_reason = SoundEndReason.END_TIME

    return AuditResult(notes=notes, peak_concurrent=_peak_concurrent(notes))
