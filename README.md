# sustain-auditor

钢琴事件流踏板审计器。松键不一定等于停止发声：踏板踩下期间 NOTE_OFF 只释放琴键，
音符转入延音；抬踏板才结束该通道全部已松键音符。同音高重复按下会形成多个待配对
音符，每个 NOTE_OFF 释放该通道同音高**最早仍按住**的 NOTE_ON。

中踏板（SOSTENUTO_DOWN/UP，每通道独立）在踩下瞬间只捕获该通道当时仍被琴键按住的
音符实例：此后新弹的音、以及此前已松键仅靠普通踏板发声的音都不被捕获。两种踏板
各自只保持自己捕获的音符，互不追溯；已松键音符只要任一保持生效就继续发声，两种
保持都解除时才结束，结束原因由后处理的那次抬踏板决定（同一时间戳按输入序逐事件
处理）。

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
- `type`：`NOTE_ON` / `NOTE_OFF` / `PEDAL_DOWN` / `PEDAL_UP` /
  `SOSTENUTO_DOWN` / `SOSTENUTO_UP`。
- `channel` 0～15；`pitch` 0～127（仅 NOTE_ON/NOTE_OFF 需要）。
- `endTime` 不早于末事件；剩余发声音符在此截断并标记 `END_TIME`。
- 孤立 NOTE_OFF、重复 PEDAL_DOWN / SOSTENUTO_DOWN、未踩下的
  PEDAL_UP / SOSTENUTO_UP 等 → **整份拒绝**
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
- 未使用中踏板事件的输入，输出与纯普通踏板语义完全一致。

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
