"""逐音符对象模型：按键区间与实际发声区间。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple, Union

Number = Union[int, float]


class EventType(str, Enum):
    NOTE_ON = "NOTE_ON"
    NOTE_OFF = "NOTE_OFF"
    PEDAL_DOWN = "PEDAL_DOWN"
    PEDAL_UP = "PEDAL_UP"


class KeyOffReason(str, Enum):
    NOTE_OFF = "NOTE_OFF"    # 琴键被正常松开
    END_TIME = "END_TIME"    # 直到 endTime 仍未松键，按键区间在此截断


class SoundEndReason(str, Enum):
    NOTE_OFF = "NOTE_OFF"    # 松键时踏板未踩下，发声随即结束
    PEDAL_UP = "PEDAL_UP"    # 松键后由踏板延音，抬踏板时结束
    END_TIME = "END_TIME"    # 直到 endTime 仍在发声，被截断


@dataclass(frozen=True)
class Event:
    time: Number
    type: EventType
    channel: int
    pitch: Optional[int] = None  # 仅 NOTE_ON / NOTE_OFF 携带


@dataclass
class Note:
    """一个 NOTE_ON 派生的音符。

    按键区间 [key_on, key_off)：琴键物理按下的区间。
    发声区间 [key_on, sound_end)：实际发声区间，可因踏板越过 key_off。
    两个区间均为左闭右开。
    """

    id: int                     # 按 NOTE_ON 输入顺序编号
    channel: int
    pitch: int
    key_on: Number
    key_off: Optional[Number] = None
    key_off_reason: Optional[KeyOffReason] = None
    sound_end: Optional[Number] = None
    sound_end_reason: Optional[SoundEndReason] = None

    @property
    def sound_start(self) -> Number:
        return self.key_on

    @property
    def key_interval(self) -> Tuple[Number, Number]:
        return (self.key_on, self.key_off)

    @property
    def sound_interval(self) -> Tuple[Number, Number]:
        return (self.key_on, self.sound_end)


@dataclass
class AuditResult:
    notes: list                 # list[Note]，按 NOTE_ON 输入顺序
    peak_concurrent: int        # 全局峰值同时发声数
