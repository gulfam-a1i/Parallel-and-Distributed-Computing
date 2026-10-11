# Wire protocol

The client and the worker use a small custom protocol over one TCP connection.
I didn't use HTTP: a persistent socket makes it easy for the worker to push
progress whenever it wants, and it is the same socket model we used in the
labs. The code is in [`common/protocol.py`](../common/protocol.py).

## Framing

There are two kinds of data on the wire.

**Control frames** are a 4-byte big-endian length followed by a UTF-8 JSON
object with a `type` field:

```
00 00 00 2a  {"type":"PING","t":1712345.123,"seq":0}
```

Frames larger than 1 MiB are rejected as garbage, so a desynchronised or
malicious peer can't make the other side allocate huge buffers.

**Bulk data** (input video, rendered output) is sent raw. First a control
frame announces it with the exact `size` and its `sha256`, then exactly
`size` bytes follow. The receiver:

1. writes into `<name>.part`,
2. hashes while it reads,
3. raises `ConnectionLost` if the socket closes or stalls before `size` bytes arrive,
4. raises `IntegrityError` if the digest doesn't match,
5. renames `.part` to the real name only after the hash checks out.

So a half-transferred or corrupted file never looks like a finished one.

## Handshake and availability check

```
client                                   worker
  | HELLO {version, client}  ------------>  |   version must match
  |  <------------  CHALLENGE {nonce, worker}|   random 128-bit nonce
  | AUTH {digest}  ---------------------->  |   digest = HMAC-SHA256(token, nonce)
  |  <------------  WELCOME {gpu, capabilities, accepting, queue, ...}
  | PING {t, seq}  x N  ----------------->  |
  |  <------------  PONG {t, seq, queue}     |   client computes min/avg/max/jitter
```

- The token itself never crosses the network. Only an HMAC of a fresh nonce
  does, so a captured handshake can't be replayed.
- After a wrong token the worker waits 1 s before replying, to slow down guessing.
- `WELCOME.capabilities` lists the job kinds and the encoder used for each codec
  (`h264 → h264_nvenc`). The worker test-encodes one frame at startup to confirm
  NVENC really works, because the encoder being listed in FFmpeg doesn't prove it.
- The client refuses to submit if `accepting` is false (queue full), if the job
  kind isn't supported, or if there is no encoder for the requested codec.

## Running a job

```
client                                         worker
  | SUBMIT {kind, config, file{name,size,sha256}} ->|  config checked against whitelists
  |  <------------------------  READY {job_id, upload}
  | <raw input bytes>  -------------------------->  |  hashed while receiving
  |  <------------------------  UPLOAD_OK {bytes, seconds}
  |  <------------------------  QUEUED {position}
  |  <------------------------  STARTED {queue_s}
  |  <------------------------  PROGRESS {percent, fps, speed, eta_s}   (~4/s)
  |  <------------------------  LOG {line}                              (ffmpeg warnings)
  |  <------------------------  HEARTBEAT {state, percent, position}    (every 2 s)
  |  <------------------------  COMPLETE {output{name,size,sha256}, result, timing}
  | FETCH {job_id}  ----------------------------->  |
  |  <------------------------  RESULT {size, sha256}
  |  <------------------------  <raw output bytes>
  | RECEIVED {job_id, ok}  ---------------------->  |  worker deletes the job files
  | BYE  ---------------------------------------->  |
```

`PROGRESS` comes from FFmpeg's `-progress pipe:1` output: `out_time_us` divided
by the input duration from `ffprobe`. For the CUDA job, progress is reported
per batch of matrix multiplications, with the running GFLOP/s.

`CANCEL {job_id}` can be sent at any time. The worker kills the FFmpeg process
(terminate, then kill after 3 s) and answers `CANCELLED`.

## Failure handling

| Situation | What happens |
|---|---|
| Worker not reachable | `connect()` fails with a specific message: refused / timed out / unreachable |
| Wrong token or protocol version | `ERROR {code}` and the connection is closed |
| Bad job settings | `ERROR {code: bad_config}` before any upload starts |
| Upload checksum mismatch | worker deletes the file, sends `ERROR {code: integrity}`, client re-sends (3 tries) |
| Connection drops during upload | worker discards the partial file, client reconnects and uploads again |
| Connection drops during render | **the job keeps running**. Client reconnects with back-off (1, 2, 4 s), sends `ATTACH {job_id}`, gets `JOB_STATE` and then the remaining progress |
| Worker goes silent | client treats 15 s without any frame as a dead link (the worker heartbeats every 2 s) |
| Download checksum mismatch / drop | client deletes the `.part` file and sends `FETCH` again |
| Client never comes back | finished outputs are kept for `--retention` seconds (default 30 min), then removed |
| Idle connection | worker closes sessions that send nothing for 10 min while watching no job |

TCP keep-alive is also enabled on both ends (30 s idle, 3 probes), so the OS
notices a pulled cable even when no application data is flowing.

## Message reference

| Type | Direction | Fields |
|---|---|---|
| `HELLO` | C→W | `version`, `client` |
| `CHALLENGE` | W→C | `nonce`, `worker` |
| `AUTH` | C→W | `digest` |
| `WELCOME` | W→C | `worker`, `gpu`, `system`, `capabilities`, `accepting`, `queue`, `uptime_s`, `max_upload_mb` |
| `PING` / `PONG` | both | `t`, `seq` (+ queue stats in PONG) |
| `SUBMIT` | C→W | `kind` (`transcode` / `cuda_matmul`), `config`, `file` |
| `READY` | W→C | `job_id`, `upload` |
| `UPLOAD_OK` | W→C | `job_id`, `bytes`, `seconds` |
| `QUEUED` | W→C | `job_id`, `position` |
| `STARTED` | W→C | `job_id`, `queue_s` |
| `PROGRESS` | W→C | `job_id`, `percent`, `fps`, `speed`, `frame`, `eta_s` (transcode) / `iteration`, `gflops` (compute) |
| `LOG` | W→C | `job_id`, `line` |
| `HEARTBEAT` | W→C | `job_id`, `state`, `percent`, `position` |
| `COMPLETE` | W→C | `job_id`, `output`, `result` (incl. `metrics`), `timing` |
| `CANCEL` / `CANCELLED` | C→W / W→C | `job_id` |
| `ATTACH` | C→W | `job_id` |
| `JOB_STATE` | W→C | `job_id`, `state`, `percent`, `progress`, `position` |
| `FETCH` | C→W | `job_id` |
| `RESULT` | W→C | `job_id`, `name`, `size`, `sha256`, then raw bytes |
| `RECEIVED` | C→W | `job_id`, `ok` |
| `ERROR` | W→C | `code`, `message`, optional `job_id` |
| `BYE` | C→W | none |
