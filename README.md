# sustain-auditor

钢琴事件流踏板审计器。松键不一定等于停止发声：踏板踩下期间 NOTE_OFF 只释放琴键，
音符转入延音；抬踏板才结束该通道全部已松键音符。同音高重复按下会形成多个待配对
音符，每个 NOTE_OFF 释放该通道同音高**最早仍按住**的 NOTE_ON。

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

- `events`：1～5000 个，按 `time` 非降序；同一时刻按输入序执行。
- `channel` 0～15；`pitch` 0～127（仅 NOTE_ON/NOTE_OFF 需要）。
- `endTime` 不早于末事件；剩余发声音符在此截断并标记 `END_TIME`。
- 孤立 NOTE_OFF、重复 PEDAL_DOWN、未踩下的 PEDAL_UP 等 → **整份拒绝**
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
- `endReason`：`NOTE_OFF` / `PEDAL_UP` / `END_TIME`。
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
