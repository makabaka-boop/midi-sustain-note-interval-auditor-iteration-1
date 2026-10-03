# sustain-auditor

钢琴事件流踏板审计器。松键不一定等于停止发声：普通延音踏板踩下期间 NOTE_OFF
只释放琴键，音符转入延音；未被中踏板继续保持的音在抬普通踏板时结束。同音高
重复按下会形成多个待配对音符，每个 NOTE_OFF 释放该通道同音高**最早仍按住**
的 NOTE_ON。

中踏板（SOSTENUTO_DOWN/UP）按通道独立：踩下瞬间只捕获当时仍被琴键按住的
具体音符实例；踩下后新弹的音、以及已经松键而仅靠普通延音踏板发声的音不会被
捕获。松键后的音只要普通延音踏板或捕获它的中踏板仍生效就继续发声，二者都解除
时才结束；结束原因标记最后实际解除发声的踏板。

## 输入（stdin，仅 JSON）

```json
{
  "events": [
    {"time": 0, "type": "PEDAL_DOWN", "channel": 0},
    {"time": 1, "type": "NOTE_ON",  "channel": 0, "pitch": 60},
    {"time": 3, "type": "NOTE_OFF", "channel": 0, "pitch": 60},
    {"time": 6, "type": "PEDAL_UP", "channel": 0}
  ],
  "endTime": 10
}
```

中踏板事件同样只需要 `time`、`type`、`channel`：

```json
{"time": 4, "type": "SOSTENUTO_DOWN", "channel": 0}
{"time": 7, "type": "SOSTENUTO_UP", "channel": 0}
```

- `events`：1～5000 个，按 `time` 非降序；同一时刻按输入序执行。
- `channel` 0～15；`pitch` 0～127（仅 NOTE_ON/NOTE_OFF 需要）。
- `endTime` 不早于末事件；剩余发声音符在此截断并标记 `END_TIME`。
- 孤立 NOTE_OFF、重复 PEDAL_DOWN/SOSTENUTO_DOWN、未踩下的
  PEDAL_UP/SOSTENUTO_UP 等 → **整份拒绝**
  （stderr 输出 `{"error": ...}`，退出码 1，stdout 无部分输出）。

## 输出（stdout，JSON）

```json
{
  "notes": [
    {"id": 0, "channel": 0, "pitch": 60,
     "key":   {"start": 1, "end": 3, "endReason": "NOTE_OFF"},
     "sound": {"start": 1, "end": 6, "endReason": "PEDAL_UP"}}
  ],
  "peakConcurrent": 1
}
```

- `key`：按键区间；`sound`：实际发声区间（均为左闭右开）。
- `endReason`：`NOTE_OFF` / `PEDAL_UP` / `SOSTENUTO_UP` / `END_TIME`。
- `peakConcurrent`：全局峰值同时发声数。

## 运行

```bash
# 本地
python -m sustain < examples/sample.json

# Docker Compose（sustain 服务）
docker compose run --rm -T sustain < examples/sample.json

# 测试
python -m pytest -q
docker compose --profile test run --rm sustain-test
```
