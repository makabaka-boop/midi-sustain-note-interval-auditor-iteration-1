"""中踏板（SOSTENUTO_DOWN/UP）语义测试。

测试分两层：
1. 场景测试：硬编码期望的逐音符对象（与既有 test_audit.py 同风格）。
2. 独立逐事件参考模型：只用普通 dict/list 逐个事件模拟同一份语义，
   与 sustain.audit 的实现互不共享代码；手工序列 + 固定种子随机合法
   事件流上交叉对拍（音符区间、结束原因、峰值并发数完全一致）。

语义要点（见 sustain/audit.py docstring）：
- SOSTENUTO_DOWN 只捕获该通道当时仍被琴键按住的音符实例；此后新弹的、
  此前已松键仅靠普通踏板发声的都不被捕获。
- 两种踏板各自只保持自己捕获的音符，互不追溯；已松键音符只要任一保持
  生效就继续发声，两种保持都解除时才结束，结束原因由后处理的抬踏板决定。
"""
import io
import json
import random
import subprocess
import sys

import pytest

from sustain import (
    AuditError,
    KeyOffReason,
    Note,
    SoundEndReason,
    audit,
)
from sustain.cli import main, result_to_dict

KOFF = KeyOffReason.NOTE_OFF
KEND = KeyOffReason.END_TIME
SOFF = SoundEndReason.NOTE_OFF
SPEDAL = SoundEndReason.PEDAL_UP
SSOS = SoundEndReason.SOSTENUTO_UP
SEND = SoundEndReason.END_TIME


# ---------- 构造输入的辅助函数 ----------

def on(t, ch=0, p=60):
    return {"time": t, "type": "NOTE_ON", "channel": ch, "pitch": p}


def off(t, ch=0, p=60):
    return {"time": t, "type": "NOTE_OFF", "channel": ch, "pitch": p}


def pd(t, ch=0):
    return {"time": t, "type": "PEDAL_DOWN", "channel": ch}


def pu(t, ch=0):
    return {"time": t, "type": "PEDAL_UP", "channel": ch}


def sd(t, ch=0):
    return {"time": t, "type": "SOSTENUTO_DOWN", "channel": ch}


def su(t, ch=0):
    return {"time": t, "type": "SOSTENUTO_UP", "channel": ch}


def payload(events, end_time):
    return {"events": events, "endTime": end_time}


def note(id, ch, p, key_on, key_off, kreason, sound_end, sreason):
    """期望的逐音符对象。sound_start 恒等于 key_on。"""
    return Note(id=id, channel=ch, pitch=p, key_on=key_on, key_off=key_off,
                key_off_reason=kreason, sound_end=sound_end,
                sound_end_reason=sreason)


# ---------- 独立逐事件参考模型（不导入 sustain 的任何实现） ----------

def reference_audit(events, end_time):
    """纯 dict/list 的逐事件模拟，返回 (notes, peak)，字段与 audited() 对齐。"""
    notes = []
    held = {}       # (ch, pitch) -> [note, ...] 仍按住，队首最早
    sustained = {}  # ch -> [note, ...] 普通踏板保持中
    captured = {}   # ch -> [note, ...] 中踏板锁住中
    pedal = set()   # 普通踏板踩下的通道
    sos = set()     # 中踏板踩下的通道

    for ev in events:
        t, ch, ty = ev["time"], ev["channel"], ev["type"]
        if ty == "NOTE_ON":
            n = {"id": len(notes), "channel": ch, "pitch": ev["pitch"],
                 "key_on": t, "key_off": None, "kreason": None,
                 "sound_end": None, "sreason": None,
                 "pedal": False, "sos": False}
            notes.append(n)
            held.setdefault((ch, ev["pitch"]), []).append(n)
        elif ty == "NOTE_OFF":
            q = held.get((ch, ev["pitch"]), [])
            assert q, "reference model: orphan NOTE_OFF"
            n = q.pop(0)  # 最早仍按住的同音 NOTE_ON
            n["key_off"] = t
            n["kreason"] = "NOTE_OFF"
            n["pedal"] = ch in pedal
            if n["pedal"]:
                sustained.setdefault(ch, []).append(n)
            if not n["pedal"] and not n["sos"]:
                n["sound_end"] = t
                n["sreason"] = "NOTE_OFF"
        elif ty == "PEDAL_DOWN":
            assert ch not in pedal, "reference model: repeated PEDAL_DOWN"
            pedal.add(ch)
        elif ty == "PEDAL_UP":
            assert ch in pedal, "reference model: PEDAL_UP while not down"
            pedal.discard(ch)
            for n in sustained.pop(ch, []):
                n["pedal"] = False
                if not n["sos"]:
                    n["sound_end"] = t
                    n["sreason"] = "PEDAL_UP"
        elif ty == "SOSTENUTO_DOWN":
            assert ch not in sos, "reference model: repeated SOSTENUTO_DOWN"
            sos.add(ch)
            for (c, _p), q in held.items():
                if c == ch:
                    for n in q:
                        n["sos"] = True
                        captured.setdefault(ch, []).append(n)
        elif ty == "SOSTENUTO_UP":
            assert ch in sos, "reference model: SOSTENUTO_UP while not down"
            sos.discard(ch)
            for n in captured.pop(ch, []):
                n["sos"] = False
                if n["key_off"] is not None and not n["pedal"]:
                    n["sound_end"] = t
                    n["sreason"] = "SOSTENUTO_UP"
        else:
            raise AssertionError(f"reference model: unknown type {ty}")

    for n in notes:
        if n["key_off"] is None:
            n["key_off"] = end_time
            n["kreason"] = "END_TIME"
        if n["sound_end"] is None:
            n["sound_end"] = end_time
            n["sreason"] = "END_TIME"

    deltas = {}
    for n in notes:
        deltas[n["key_on"]] = deltas.get(n["key_on"], 0) + 1
        deltas[n["sound_end"]] = deltas.get(n["sound_end"], 0) - 1
    peak = current = 0
    for t in sorted(deltas):
        current += deltas[t]
        if current > peak:
            peak = current
    return notes, peak


def audited(events, end_time):
    """被测实现的结果，投影成与参考模型相同的可比较形状。"""
    r = audit(payload(events, end_time))
    notes = [{
        "id": n.id, "channel": n.channel, "pitch": n.pitch,
        "key_on": n.key_on, "key_off": n.key_off,
        "kreason": n.key_off_reason.value,
        "sound_end": n.sound_end, "sreason": n.sound_end_reason.value,
    } for n in r.notes]
    return notes, r.peak_concurrent


def projected(ref_notes):
    keys = ("id", "channel", "pitch", "key_on", "key_off",
            "kreason", "sound_end", "sreason")
    return [{k: n[k] for k in keys} for n in ref_notes]


def check(events, end_time):
    """被测实现与独立参考模型必须完全一致。"""
    ref_notes, ref_peak = reference_audit(events, end_time)
    assert audited(events, end_time) == (projected(ref_notes), ref_peak)


# ---------- 中踏板基础语义 ----------

class TestSostenutoBasics:
    def test_capture_held_note_ends_at_sostenuto_up(self):
        ev = [on(0), sd(1), off(2), su(6)]
        check(ev, 6)
        r = audit(payload(ev, 6))
        assert r.notes == [note(0, 0, 60, 0, 2, KOFF, 6, SSOS)]
        assert r.peak_concurrent == 1

    def test_note_played_after_sostenuto_down_not_captured(self):
        ev = [sd(0), on(1), off(2), su(5)]
        check(ev, 5)
        r = audit(payload(ev, 5))
        assert r.notes == [note(0, 0, 60, 1, 2, KOFF, 2, SOFF)]

    def test_pedal_sustained_note_not_captured(self):
        # 已松键、仅靠普通踏板发声的音不被中踏板捕获
        ev = [pd(0), on(1), off(2), sd(3), su(4), pu(6)]
        check(ev, 6)
        r = audit(payload(ev, 6))
        assert r.notes == [note(0, 0, 60, 1, 2, KOFF, 6, SPEDAL)]

    def test_captures_all_currently_held_instances(self):
        ev = [on(0, p=60), on(1, p=64), sd(2),
              off(3, p=60), off(4, p=64), su(8)]
        check(ev, 8)
        r = audit(payload(ev, 8))
        assert r.notes == [
            note(0, 0, 60, 0, 3, KOFF, 8, SSOS),
            note(1, 0, 64, 1, 4, KOFF, 8, SSOS),
        ]
        assert r.peak_concurrent == 2

    def test_sostenuto_up_while_key_held_has_no_audible_effect(self):
        # 抬中踏板时琴键仍按住：捕获悄悄失效，之后正常松键结束
        ev = [on(0), sd(1), su(2), off(3)]
        check(ev, 3)
        r = audit(payload(ev, 3))
        assert r.notes == [note(0, 0, 60, 0, 3, KOFF, 3, SOFF)]

    def test_recapture_after_sostenuto_up(self):
        # 中踏板抬起再踩下：重新捕获当时仍按住的音符
        ev = [on(0), sd(1), su(2), sd(3), off(4), su(6)]
        check(ev, 6)
        r = audit(payload(ev, 6))
        assert r.notes == [note(0, 0, 60, 0, 4, KOFF, 6, SSOS)]

    def test_released_note_not_recaptured_by_later_sostenuto_down(self):
        ev = [on(0), sd(1), off(2), su(3), sd(4), su(5)]
        check(ev, 5)
        r = audit(payload(ev, 5))
        assert r.notes == [note(0, 0, 60, 0, 2, KOFF, 3, SSOS)]

    def test_float_times(self):
        ev = [on(0.5), sd(1.5), off(2.5), su(3.5)]
        check(ev, 3.5)
        r = audit(payload(ev, 3.5))
        assert r.notes == [note(0, 0, 60, 0.5, 2.5, KOFF, 3.5, SSOS)]


# ---------- 双踏板交叠 ----------

class TestDualPedalOverlap:
    def test_dual_hold_pedal_lifted_first_ends_via_sostenuto(self):
        # 松键时双保持；先抬普通踏板不结束，抬中踏板才结束
        ev = [on(0), sd(1), pd(2), off(3), pu(4), su(5)]
        check(ev, 5)
        r = audit(payload(ev, 5))
        assert r.notes == [note(0, 0, 60, 0, 3, KOFF, 5, SSOS)]

    def test_dual_hold_sostenuto_lifted_first_ends_via_pedal(self):
        ev = [on(0), sd(1), pd(2), off(3), su(4), pu(5)]
        check(ev, 5)
        r = audit(payload(ev, 5))
        assert r.notes == [note(0, 0, 60, 0, 3, KOFF, 5, SPEDAL)]

    def test_same_time_lifts_pedal_up_first_in_input(self):
        # 同刻两次抬踏板：输入序后处理的（中踏板）决定结束原因
        ev = [on(0), sd(1), pd(2), off(3), pu(5), su(5)]
        check(ev, 5)
        r = audit(payload(ev, 5))
        assert r.notes == [note(0, 0, 60, 0, 3, KOFF, 5, SSOS)]

    def test_same_time_lifts_sostenuto_up_first_in_input(self):
        ev = [on(0), sd(1), pd(2), off(3), su(5), pu(5)]
        check(ev, 5)
        r = audit(payload(ev, 5))
        assert r.notes == [note(0, 0, 60, 0, 3, KOFF, 5, SPEDAL)]

    def test_late_pedal_down_does_not_acquire_sostenuto_note(self):
        # 普通踏板只保持在其踩下期间松键的音：不追溯已靠中踏板发声的音
        ev = [on(0), sd(1), off(2), pd(3), su(4), pu(6)]
        check(ev, 6)
        r = audit(payload(ev, 6))
        assert r.notes == [note(0, 0, 60, 0, 2, KOFF, 4, SSOS)]

    def test_lifts_while_key_held_have_no_audible_effect(self):
        ev = [on(0), sd(1), pd(2), su(3), pu(4), off(5)]
        check(ev, 5)
        r = audit(payload(ev, 5))
        assert r.notes == [note(0, 0, 60, 0, 5, KOFF, 5, SOFF)]

    def test_dual_hold_with_same_pitch_overlap_lift_order_per_instance(self):
        # 同音双实例：id0 双保持、id1 仅普通踏板；抬踏板顺序决定各自结局
        ev = [pd(0), on(1), sd(2), on(3), off(4), off(5), pu(6), su(7)]
        check(ev, 7)
        r = audit(payload(ev, 7))
        assert r.notes == [
            note(0, 0, 60, 1, 4, KOFF, 7, SSOS),    # 普通踏板先抬，中踏板保持
            note(1, 0, 60, 3, 5, KOFF, 6, SPEDAL),  # 仅普通踏板保持
        ]
        assert r.peak_concurrent == 2

    def test_dual_hold_with_same_pitch_overlap_reverse_lift_order(self):
        ev = [pd(0), on(1), sd(2), on(3), off(4), off(5), su(6), pu(7)]
        check(ev, 7)
        r = audit(payload(ev, 7))
        assert r.notes == [
            note(0, 0, 60, 1, 4, KOFF, 7, SPEDAL),  # 中踏板先抬，普通踏板保持
            note(1, 0, 60, 3, 5, KOFF, 7, SPEDAL),
        ]


# ---------- 同音重触发：FIFO 配对与中踏板捕获各自独立 ----------

class TestSamePitchRetrigger:
    def test_both_overlapping_instances_captured(self):
        ev = [on(0), on(1), sd(2), off(3), off(4), su(8)]
        check(ev, 8)
        r = audit(payload(ev, 8))
        assert r.notes == [
            note(0, 0, 60, 0, 3, KOFF, 8, SSOS),
            note(1, 0, 60, 1, 4, KOFF, 8, SSOS),
        ]
        assert r.peak_concurrent == 2

    def test_retrigger_after_capture_new_instance_not_captured(self):
        ev = [on(0), sd(1), off(2), on(3), off(4), su(9)]
        check(ev, 9)
        r = audit(payload(ev, 9))
        assert r.notes == [
            note(0, 0, 60, 0, 2, KOFF, 9, SSOS),   # 捕获的实例延到中踏板抬起
            note(1, 0, 60, 3, 4, KOFF, 4, SOFF),   # 捕获后新弹的实例不被捕获
        ]
        assert r.peak_concurrent == 2

    def test_fifo_pairing_unaffected_by_capture(self):
        # 第一个 OFF 仍配对最早按住的（被捕获的）实例
        ev = [on(0), sd(1), on(2), off(3), su(4), off(5)]
        check(ev, 5)
        r = audit(payload(ev, 5))
        assert r.notes == [
            note(0, 0, 60, 0, 3, KOFF, 4, SSOS),
            note(1, 0, 60, 2, 5, KOFF, 5, SOFF),
        ]
        assert r.peak_concurrent == 2


# ---------- 跨通道隔离 ----------

class TestCrossChannel:
    def test_sostenuto_is_per_channel(self):
        ev = [on(0, ch=0), on(0, ch=1), sd(1, ch=0),
              off(2, ch=0), off(2, ch=1), su(6, ch=0)]
        check(ev, 6)
        r = audit(payload(ev, 6))
        assert r.notes == [
            note(0, 0, 60, 0, 2, KOFF, 6, SSOS),
            note(1, 1, 60, 0, 2, KOFF, 2, SOFF),
        ]
        assert r.peak_concurrent == 2

    def test_sostenuto_up_on_other_channel_does_not_end(self):
        ev = [on(0, ch=0), on(0, ch=1), sd(1, ch=0), sd(1, ch=1),
              off(2, ch=0), off(2, ch=1), su(5, ch=0)]
        check(ev, 8)
        r = audit(payload(ev, 8))
        assert r.notes == [
            note(0, 0, 60, 0, 2, KOFF, 5, SSOS),
            note(1, 1, 60, 0, 2, KOFF, 8, SEND),   # ch1 中踏板未抬 → 截断
        ]

    def test_two_pedals_on_different_channels_independently(self):
        # ch0 普通踏板延音、ch1 中踏板锁住，互不干扰
        ev = [on(0, ch=1), pd(0, ch=0), sd(1, ch=1), on(1, ch=0),
              off(2, ch=0), off(2, ch=1), pu(5, ch=0), su(6, ch=1)]
        check(ev, 6)
        r = audit(payload(ev, 6))
        assert r.notes == [
            note(0, 1, 60, 0, 2, KOFF, 6, SSOS),   # ch1：中踏板捕获
            note(1, 0, 60, 1, 2, KOFF, 5, SPEDAL),  # ch0：普通踏板延音
        ]
        assert r.peak_concurrent == 2


# ---------- 同刻事件：严格按输入序逐事件处理 ----------

class TestSameTimestampOrdering:
    def test_sostenuto_down_before_note_on_same_time_not_captured(self):
        ev = [sd(3), on(3), off(5), su(7)]
        check(ev, 7)
        r = audit(payload(ev, 7))
        assert r.notes == [note(0, 0, 60, 3, 5, KOFF, 5, SOFF)]

    def test_note_on_before_sostenuto_down_same_time_captured(self):
        ev = [on(3), sd(3), off(5), su(7)]
        check(ev, 7)
        r = audit(payload(ev, 7))
        assert r.notes == [note(0, 0, 60, 3, 5, KOFF, 7, SSOS)]

    def test_note_off_before_sostenuto_down_same_time_not_captured(self):
        ev = [on(0), off(3), sd(3), su(5)]
        check(ev, 5)
        r = audit(payload(ev, 5))
        assert r.notes == [note(0, 0, 60, 0, 3, KOFF, 3, SOFF)]

    def test_sostenuto_down_before_note_off_same_time_captured(self):
        ev = [on(0), sd(3), off(3), su(5)]
        check(ev, 5)
        r = audit(payload(ev, 5))
        assert r.notes == [note(0, 0, 60, 0, 3, KOFF, 5, SSOS)]

    def test_sostenuto_up_before_note_off_same_time(self):
        # SU 先执行 → 捕获已解除 → OFF 时无任何保持 → 立即结束
        ev = [on(0), sd(1), su(4), off(4)]
        check(ev, 4)
        r = audit(payload(ev, 4))
        assert r.notes == [note(0, 0, 60, 0, 4, KOFF, 4, SOFF)]

    def test_note_off_before_sostenuto_up_same_time(self):
        # OFF 先执行 → 转入中踏板保持 → 随后的 SU 结束它
        ev = [on(0), sd(1), off(4), su(4)]
        check(ev, 4)
        r = audit(payload(ev, 4))
        assert r.notes == [note(0, 0, 60, 0, 4, KOFF, 4, SSOS)]

    def test_note_off_and_pedal_up_same_time_sostenuto_still_holds(self):
        # OFF 与 PU 同刻：无论先后，中踏板都继续保持该音
        for ev in ([on(0), sd(1), pd(2), off(5), pu(5), su(6)],
                   [on(0), sd(1), pd(2), pu(5), off(5), su(6)]):
            check(ev, 6)
            r = audit(payload(ev, 6))
            assert r.notes == [note(0, 0, 60, 0, 5, KOFF, 6, SSOS)]


# ---------- endTime 截断 ----------

class TestEndTimeTruncation:
    def test_sostenuto_held_note_truncated_at_end_time(self):
        ev = [on(0), sd(1), off(2)]
        check(ev, 10)
        r = audit(payload(ev, 10))
        assert r.notes == [note(0, 0, 60, 0, 2, KOFF, 10, SEND)]

    def test_dual_held_note_truncated_at_end_time(self):
        ev = [on(0), sd(1), pd(2), off(3), su(4)]
        check(ev, 9)
        r = audit(payload(ev, 9))
        assert r.notes == [note(0, 0, 60, 0, 3, KOFF, 9, SEND)]

    def test_key_held_and_sostenuto_down_at_end_time(self):
        ev = [on(2), sd(3)]
        check(ev, 8)
        r = audit(payload(ev, 8))
        assert r.notes == [note(0, 0, 60, 2, 8, KEND, 8, SEND)]


# ---------- 峰值同时发声数 ----------

class TestPeakConcurrent:
    def test_peak_counts_sostenuto_held_notes(self):
        ev = [on(1, p=60), on(2, p=64), on(3, p=67), sd(4),
              off(5, p=60), off(6, p=64), off(7, p=67), su(9)]
        check(ev, 9)
        r = audit(payload(ev, 9))
        assert r.peak_concurrent == 3

    def test_peak_with_mixed_holds(self):
        # id0 双保持、id1 仅普通踏板：两者都发声到 PU@7
        ev = [on(0, p=60), sd(1), pd(2), on(3, p=64),
              off(4, p=60), off(5, p=64), su(6), pu(7)]
        check(ev, 7)
        r = audit(payload(ev, 7))
        assert r.notes == [
            note(0, 0, 60, 0, 4, KOFF, 7, SPEDAL),
            note(1, 0, 64, 3, 5, KOFF, 7, SPEDAL),
        ]
        assert r.peak_concurrent == 2


# ---------- 非法中踏板序列：整份拒绝 ----------

class TestRejection:
    def test_repeated_sostenuto_down_rejected(self):
        with pytest.raises(AuditError, match="repeated SOSTENUTO_DOWN"):
            audit(payload([sd(0), sd(1)], 2))

    def test_sostenuto_up_without_down_rejected(self):
        with pytest.raises(AuditError, match="sostenuto is not down"):
            audit(payload([su(0)], 2))

    def test_sostenuto_state_is_per_channel_for_rejection(self):
        with pytest.raises(AuditError, match="sostenuto is not down"):
            audit(payload([sd(0, ch=0), su(1, ch=1)], 2))

    def test_sostenuto_does_not_satisfy_pedal_validation(self):
        with pytest.raises(AuditError, match="pedal is not down"):
            audit(payload([sd(0), pu(1)], 2))

    def test_pedal_does_not_satisfy_sostenuto_validation(self):
        with pytest.raises(AuditError, match="sostenuto is not down"):
            audit(payload([pd(0), su(1)], 2))

    def test_sostenuto_down_up_cycle_is_legal(self):
        r = audit(payload([sd(0), su(1), sd(2), su(3)], 3))
        assert r.notes == []
        assert r.peak_concurrent == 0

    def test_rejection_leaves_no_partial_output(self):
        # 前面全部合法、最后一个事件非法 → 仍整份拒绝
        with pytest.raises(AuditError, match="repeated SOSTENUTO_DOWN"):
            audit(payload([on(0), sd(1), off(2), sd(3)], 4))


# ---------- CLI ----------

class TestCli:
    def test_roundtrip_with_sostenuto(self, monkeypatch, capsys):
        p = payload([on(0), sd(1), off(2), su(5)], 5)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(p)))
        assert main([]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out == {
            "notes": [{
                "id": 0, "channel": 0, "pitch": 60,
                "key": {"start": 0, "end": 2, "endReason": "NOTE_OFF"},
                "sound": {"start": 0, "end": 5, "endReason": "SOSTENUTO_UP"},
            }],
            "peakConcurrent": 1,
        }

    def test_sostenuto_rejection_via_cli_no_partial_output(
            self, monkeypatch, capsys):
        p = payload([on(0), sd(1), off(2), sd(3)], 4)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(p)))
        assert main([]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""                      # 整份拒绝，无部分输出
        assert "repeated SOSTENUTO_DOWN" in json.loads(captured.err)["error"]

    def test_python_m_sustain_subprocess_with_sostenuto(self):
        p = payload([on(0), sd(1), pd(2), off(3), su(4), pu(5)], 5)
        proc = subprocess.run(
            [sys.executable, "-m", "sustain"],
            input=json.dumps(p), capture_output=True, text=True)
        assert proc.returncode == 0
        out = json.loads(proc.stdout)
        assert out["notes"][0]["sound"] == {
            "start": 0, "end": 5, "endReason": "PEDAL_UP"}


# ---------- 独立逐事件参考模型对拍 ----------

def _random_valid_events(rng, count):
    """生成合法事件流：同刻事件、同音重触发、双踏板交叠高频出现。"""
    events = []
    t = 0
    held = {}    # (ch, pitch) -> 按住中的实例数
    pedal = set()
    sos = set()
    for _ in range(count):
        t += rng.choice((0, 0, 0, 1, 1, 2))
        ch = rng.randrange(4)
        legal = ["NOTE_ON", "NOTE_ON"]
        off_pitches = [p for (c, p), n in held.items() if c == ch and n > 0]
        if off_pitches:
            legal += ["NOTE_OFF", "NOTE_OFF"]
        legal.append("PEDAL_UP" if ch in pedal else "PEDAL_DOWN")
        legal.append("SOSTENUTO_UP" if ch in sos else "SOSTENUTO_DOWN")
        kind = rng.choice(legal)
        if kind == "NOTE_ON":
            p = rng.choice((60, 60, 61, 62, 64))
            events.append(on(t, ch, p))
            held[(ch, p)] = held.get((ch, p), 0) + 1
        elif kind == "NOTE_OFF":
            p = rng.choice(off_pitches)
            events.append(off(t, ch, p))
            held[(ch, p)] -= 1
        elif kind == "PEDAL_DOWN":
            events.append(pd(t, ch))
            pedal.add(ch)
        elif kind == "PEDAL_UP":
            events.append(pu(t, ch))
            pedal.discard(ch)
        elif kind == "SOSTENUTO_DOWN":
            events.append(sd(t, ch))
            sos.add(ch)
        else:
            events.append(su(t, ch))
            sos.discard(ch)
    end_time = t + rng.choice((0, 0, 1, 3))
    return events, end_time


class TestReferenceModel:
    @pytest.mark.parametrize("events,end_time", [
        # 双踏板多轮交叠 + 同音重触发 + 多通道混合
        ([on(0), sd(1), pd(2), on(3), off(4), su(5), off(6), pu(7),
          sd(8), on(9), off(10), su(11)], 11),
        ([pd(0, ch=0), sd(0, ch=1), on(1, ch=0), on(1, ch=1), on(2, ch=0),
          off(3, ch=0), off(4, ch=1), su(5, ch=1), pu(6, ch=0),
          off(7, ch=0)], 9),
        # 同刻密集：同时间戳上多种事件按输入序处理
        ([on(5), sd(5), pd(5), off(5), on(5), su(5), pu(5), off(5)], 5),
        ([sd(2), on(2), on(2), off(2), su(2), off(2)], 4),
        # 中踏板套中踏板（非法？不：每通道各自一轮完整 DOWN/UP）
        ([sd(0, ch=0), sd(1, ch=1), on(2, ch=0), on(2, ch=1),
          su(3, ch=0), su(4, ch=1), off(5, ch=0), off(5, ch=1)], 6),
        # 普通踏板反复踩抬，中踏板一直锁住
        ([on(0), sd(1), off(2), pd(3), pu(4), pd(5), pu(6), su(7)], 7),
    ])
    def test_crafted_sequences_match_reference(self, events, end_time):
        check(events, end_time)

    @pytest.mark.parametrize("seed", range(300))
    def test_random_valid_streams_match_reference(self, seed):
        rng = random.Random(seed)
        events, end_time = _random_valid_events(rng, 200)
        check(events, end_time)


def test_result_to_dict_with_sostenuto_matches_object_model():
    r = audit(payload([on(0), sd(1), off(2), su(5)], 5))
    d = result_to_dict(r)
    n = r.notes[0]
    assert d["notes"][0]["sound"] == {
        "start": n.sound_interval[0], "end": n.sound_interval[1],
        "endReason": n.sound_end_reason.value}
    assert d["notes"][0]["sound"]["endReason"] == "SOSTENUTO_UP"
