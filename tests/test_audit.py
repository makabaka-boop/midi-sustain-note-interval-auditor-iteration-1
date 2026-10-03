"""以逐音符对象模型（Note dataclass）对拍审计结果。

覆盖：同音重叠配对、踏板延音、跨通道隔离、同刻抬踏板的输入序语义、
endTime 截断、孤立 NOTE_OFF / 重复踏板整份拒绝、峰值同时发声数。
"""
import io
import json
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


def payload(events, end_time):
    return {"events": events, "endTime": end_time}


def note(id, ch, p, key_on, key_off, kreason, sound_end, sreason):
    """期望的逐音符对象。sound_start 恒等于 key_on。"""
    return Note(id=id, channel=ch, pitch=p, key_on=key_on, key_off=key_off,
                key_off_reason=kreason, sound_end=sound_end,
                sound_end_reason=sreason)


# ---------- 基础与踏板语义 ----------

class TestPedalSemantics:
    def test_note_off_without_pedal_ends_sound_immediately(self):
        r = audit(payload([on(0), off(5)], end_time=5))
        assert r.notes == [note(0, 0, 60, 0, 5, KOFF, 5, SOFF)]
        assert r.peak_concurrent == 1

    def test_pedal_extends_sound_beyond_key_off(self):
        # 松键 ≠ 停止发声：踏板保持到 PEDAL_UP
        r = audit(payload([on(0), pd(2), off(5), pu(10)], end_time=10))
        assert r.notes == [note(0, 0, 60, 0, 5, KOFF, 10, SPEDAL)]
        assert r.peak_concurrent == 1

    def test_pedal_up_does_not_end_still_held_note(self):
        # 抬踏板只结束已松键音符；仍按住的继续发声
        r = audit(payload(
            [on(0, p=60), pd(1), off(2, p=60),   # 已松键 → 延音
             on(3, p=64),                         # 一直按住
             pu(4)],
            end_time=8))
        assert r.notes == [
            note(0, 0, 60, 0, 2, KOFF, 4, SPEDAL),
            note(1, 0, 64, 3, 8, KEND, 8, SEND),
        ]
        assert r.peak_concurrent == 2

    def test_pedal_reapply_after_up(self):
        r = audit(payload(
            [on(0), pd(1), off(2), pu(3), pd(4), pu(5)],
            end_time=5))
        assert r.notes == [note(0, 0, 60, 0, 2, KOFF, 3, SPEDAL)]


# ---------- 同音重叠：多个 NOTE_ON 排队配对 ----------

class TestSamePitchOverlap:
    def test_note_off_pairs_earliest_held_note_on(self):
        r = audit(payload(
            [on(0), on(3), off(6), off(9)],
            end_time=9))
        assert r.notes == [
            note(0, 0, 60, 0, 6, KOFF, 6, SOFF),   # 第一个 OFF 配最早的 ON
            note(1, 0, 60, 3, 9, KOFF, 9, SOFF),
        ]
        assert r.peak_concurrent == 2               # [3,6) 同音双发

    def test_overlapping_same_pitch_sustained_by_pedal(self):
        r = audit(payload(
            [on(0), pd(1), on(3), off(4), off(5), pu(8)],
            end_time=8))
        assert r.notes == [
            note(0, 0, 60, 0, 4, KOFF, 8, SPEDAL),
            note(1, 0, 60, 3, 5, KOFF, 8, SPEDAL),
        ]
        assert r.peak_concurrent == 2

    def test_partial_overlap_second_note_survives_pedal_up(self):
        # 抬踏板时第二个同音音符仍按住 → 只结束第一个
        r = audit(payload(
            [on(0), pd(1), on(2), off(3), pu(4), off(5)],
            end_time=5))
        assert r.notes == [
            note(0, 0, 60, 0, 3, KOFF, 4, SPEDAL),
            note(1, 0, 60, 2, 5, KOFF, 5, SOFF),
        ]
        assert r.peak_concurrent == 2


# ---------- 跨通道隔离 ----------

class TestCrossChannel:
    def test_pedal_is_per_channel(self):
        r = audit(payload(
            [on(0, ch=0), on(0, ch=1),
             pd(1, ch=0),
             off(2, ch=0),          # ch0 延音
             off(2, ch=1),          # ch1 无踏板 → 立即结束
             pu(5, ch=0)],
            end_time=5))
        assert r.notes == [
            note(0, 0, 60, 0, 2, KOFF, 5, SPEDAL),
            note(1, 1, 60, 0, 2, KOFF, 2, SOFF),
        ]
        assert r.peak_concurrent == 2

    def test_same_pitch_on_different_channels_pair_independently(self):
        r = audit(payload(
            [on(0, ch=0, p=60), on(1, ch=1, p=60), off(2, ch=1, p=60)],
            end_time=4))
        assert r.notes == [
            note(0, 0, 60, 0, 4, KEND, 4, SEND),
            note(1, 1, 60, 1, 2, KOFF, 2, SOFF),
        ]
        # ch0 的 NOTE_ON 仍在按住，ch1 的 OFF 不能碰它
        assert r.peak_concurrent == 2


# ---------- 同刻抬踏板：同一时刻按输入序执行 ----------

class TestSameTimestampPedalUp:
    def test_off_before_pedal_up_at_same_time_ends_via_pedal(self):
        # OFF@5 时踏板仍踩下 → 转入延音；随后 PU@5 结束它
        r = audit(payload([pd(0), on(1), off(5), pu(5)], end_time=5))
        assert r.notes == [note(0, 0, 60, 1, 5, KOFF, 5, SPEDAL)]

    def test_pedal_up_before_off_at_same_time_ends_via_note_off(self):
        # PU@5 先执行 → OFF@5 时踏板已抬 → 发声立即结束
        r = audit(payload([pd(0), on(1), pu(5), off(5)], end_time=5))
        assert r.notes == [note(0, 0, 60, 1, 5, KOFF, 5, SOFF)]

    def test_same_time_on_and_off_zero_duration_note(self):
        r = audit(payload([on(3), off(3), on(3), off(7)], end_time=7))
        assert r.notes == [
            note(0, 0, 60, 3, 3, KOFF, 3, SOFF),   # 零时长，不计入峰值
            note(1, 0, 60, 3, 7, KOFF, 7, SOFF),
        ]
        assert r.peak_concurrent == 1


# ---------- endTime 截断 ----------

class TestEndTimeTruncation:
    def test_held_note_truncated_at_end_time(self):
        r = audit(payload([on(2)], end_time=10))
        assert r.notes == [note(0, 0, 60, 2, 10, KEND, 10, SEND)]
        assert r.peak_concurrent == 1

    def test_sustained_note_truncated_at_end_time(self):
        # 踏板一直未抬：按键区间在 OFF 结束，发声区间截断于 endTime
        r = audit(payload([pd(0), on(1), off(2)], end_time=10))
        assert r.notes == [note(0, 0, 60, 1, 2, KOFF, 10, SEND)]

    def test_end_time_equal_to_last_event_is_allowed(self):
        r = audit(payload([on(0), off(10)], end_time=10))
        assert r.notes == [note(0, 0, 60, 0, 10, KOFF, 10, SOFF)]


# ---------- 峰值同时发声数 ----------

class TestPeakConcurrent:
    def test_peak_across_overlapping_notes(self):
        r = audit(payload(
            [on(0, p=60), on(1, p=64), on(2, p=67),
             off(4, p=64), off(5, p=60), off(6, p=67)],
            end_time=6))
        assert r.peak_concurrent == 3

    def test_back_to_back_notes_do_not_overlap(self):
        # 左闭右开：[0,5) 与 [5,10) 不重叠
        r = audit(payload([on(0), off(5), on(5), off(10)], end_time=10))
        assert r.peak_concurrent == 1

    def test_peak_counts_sustained_notes(self):
        # 三个音符键都已松开但踏板延音中 → 峰值 3
        r = audit(payload(
            [pd(0), on(1, p=60), on(2, p=64), on(3, p=67),
             off(4, p=60), off(5, p=64), off(6, p=67), pu(9)],
            end_time=9))
        assert r.peak_concurrent == 3


# ---------- 整份拒绝 ----------

class TestRejection:
    def test_orphan_note_off_rejected(self):
        with pytest.raises(AuditError, match="orphan NOTE_OFF"):
            audit(payload([off(1)], end_time=2))

    def test_orphan_note_off_after_queue_drained(self):
        # 同音 ON/OFF 各一次后，再来一个 OFF → 孤立
        with pytest.raises(AuditError, match="orphan NOTE_OFF"):
            audit(payload([on(0), off(1), off(2)], end_time=3))

    def test_orphan_note_off_on_other_channel(self):
        with pytest.raises(AuditError, match="orphan NOTE_OFF"):
            audit(payload([on(0, ch=0), off(1, ch=1)], end_time=2))

    def test_repeated_pedal_down_rejected(self):
        with pytest.raises(AuditError, match="repeated PEDAL_DOWN"):
            audit(payload([pd(0), pd(1)], end_time=2))

    def test_pedal_up_without_down_rejected(self):
        with pytest.raises(AuditError, match="not down"):
            audit(payload([pu(0)], end_time=2))

    def test_pedal_state_is_per_channel_for_rejection(self):
        with pytest.raises(AuditError, match="not down"):
            audit(payload([pd(0, ch=0), pu(1, ch=1)], end_time=2))

    @pytest.mark.parametrize("bad", [
        payload([], end_time=0),                                  # 空事件
        payload([on(0)] * 5001, end_time=0),                      # 超过 5000
        payload([on(1), on(0)], end_time=2),                      # 时间回退
        payload([on(0, ch=16)], end_time=1),                      # 通道越界
        payload([on(0, ch=-1)], end_time=1),
        payload([on(0, p=128)], end_time=1),                      # 音高越界
        payload([on(0, p=-1)], end_time=1),
        payload([{"time": 0, "type": "NOTE_ON", "channel": 0}], end_time=1),  # 缺 pitch
        payload([{"time": 0, "type": "NOTE_ON", "pitch": 60}], end_time=1),   # 缺 channel
        payload([{"time": 0, "type": "NOTE_ON", "channel": 0, "pitch": 60.5}],
                end_time=1),                                      # pitch 非整数
        payload([{"time": "0", "type": "NOTE_ON", "channel": 0, "pitch": 60}],
                end_time=1),                                      # time 非数值
        payload([{"time": 0, "type": "PEDAL_DOWN", "channel": 0, "pitch": 1},
                 {"time": 0, "type": "WTF", "channel": 0}], end_time=1),      # 未知类型
        payload([on(2)], end_time=1),                             # endTime 早于末事件
        {"events": [on(0)]},                                      # 缺 endTime
        {"endTime": 1},                                           # 缺 events
        [on(0)],                                                  # 顶层非对象
    ])
    def test_invalid_inputs_rejected(self, bad):
        with pytest.raises(AuditError):
            audit(bad)

    def test_rejection_leaves_no_partial_output(self):
        # 前面全部合法、最后一个事件非法 → 仍整份拒绝
        with pytest.raises(AuditError):
            audit(payload([on(0), pd(1), off(2), pd(3)], end_time=4))


# ---------- CLI ----------

class TestCli:
    def test_roundtrip(self, monkeypatch, capsys):
        p = payload([on(0), pd(1), off(2), pu(5)], end_time=5)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(p)))
        assert main([]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out == {
            "notes": [{
                "id": 0, "channel": 0, "pitch": 60,
                "key": {"start": 0, "end": 2, "endReason": "NOTE_OFF"},
                "sound": {"start": 0, "end": 5, "endReason": "PEDAL_UP"},
            }],
            "peakConcurrent": 1,
        }

    def test_invalid_json_rejected(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin", io.StringIO("not json{"))
        assert main([]) == 1
        err = json.loads(capsys.readouterr().err)
        assert "error" in err

    def test_orphan_note_off_rejected_via_cli(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin",
                            io.StringIO(json.dumps(payload([off(1)], 2))))
        assert main([]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""                      # 整份拒绝，无部分输出
        assert "orphan NOTE_OFF" in json.loads(captured.err)["error"]

    def test_python_m_sustain_subprocess(self):
        p = payload([on(0), off(3)], end_time=3)
        proc = subprocess.run(
            [sys.executable, "-m", "sustain"],
            input=json.dumps(p), capture_output=True, text=True)
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["peakConcurrent"] == 1


def test_result_to_dict_matches_object_model():
    r = audit(payload([on(0), pd(1), off(2), pu(5)], end_time=5))
    d = result_to_dict(r)
    n = r.notes[0]
    assert d["notes"][0]["key"] == {
        "start": n.key_interval[0], "end": n.key_interval[1],
        "endReason": n.key_off_reason.value}
    assert d["notes"][0]["sound"] == {
        "start": n.sound_interval[0], "end": n.sound_interval[1],
        "endReason": n.sound_end_reason.value}
