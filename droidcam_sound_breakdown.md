# DroidCam Linux Client Audio Architecture & Technical Breakdown

This document provides a detailed technical breakdown of how **[droidcam-linux-client](https://github.com/dev47apps/droidcam-linux-client)** turns an Android/iOS smartphone into a recognized audio input source (virtual microphone) on Linux.

---

## 1. High-Level Architecture Overview

```
 ┌────────────────┐         UDP / TCP          ┌────────────────────────────────────────────────────────┐
 │   Smartphone   │ ───────────────────────────► DroidCam Linux Client (`AudioThreadProc`)                │
 │ (Mic Capture & │  (Speex WB Encoded Audio)  │   ├─ Socket Receiver (UDP/TCP)                         │
 │ Speex Encoder) │                            │   ├─ Speex Audio Decoder (`decode_speex_frame`)       │
 └────────────────┘                            │   └─ ALSA MMAP Writer (`snd_transfer_commit`)          │
                                               └──────────────────────────┬─────────────────────────────┘
                                                                          │ Direct MMAP Write to Playback Subdevice
                                                                          ▼
                                               ┌────────────────────────────────────────────────────────┐
                                               │ ALSA Virtual Loopback Driver (`snd-aloop`)             │
                                               │   ┌───────────────────────┬────────────────────────┐   │
                                               │   │ Playback (hw:Loopback,0,x) │ Capture (hw:Loopback,1,x) │   │
                                               │   └───────────────────────┴────────────────────────┘   │
                                               └──────────────────────────┬─────────────────────────────┘
                                                                          │ Virtual Hardware Bridge
                                                                          ▼
                                               ┌────────────────────────────────────────────────────────┐
                                               │ Linux Audio Subsystem & Applications                    │
                                               │ (PulseAudio / PipeWire / ALSA Capture)                 │
                                               │ Zoom, Discord, OBS, Web Browsers, etc.                 │
                                               └────────────────────────────────────────────────────────┘
```

---

## 2. Linux Kernel Virtual Audio Device Setup (`snd-aloop`)

Linux requires a driver to expose a virtual sound card that can receive audio playback data from one process and make it available as a sound capture device (microphone) to the rest of the OS.

1. **Kernel Driver**: The setup script [`install-sound`](file:///home/basel/Work/python/virtual_source/droidcam-linux-client/install-sound#L8-L20) loads the Linux kernel module `snd-aloop`:
   ```bash
   modprobe snd-aloop
   ```
2. **Loopback Card Architecture**:
   - `snd-aloop` creates a virtual sound card named `Loopback` in `/proc/asound/cards`.
   - Each loopback card has **two paired subdevices**:
     - **Subdevice 0 (Playback)**: `hw:Loopback,0,0` — Used by DroidCam client to output decoded audio.
     - **Subdevice 1 (Capture)**: `hw:Loopback,1,0` — Receives whatever was played to subdevice 0 and presents it as a microphone input to Linux audio servers (PulseAudio, PipeWire, ALSA).
3. **PulseAudio / PipeWire Integration**:
   - PulseAudio / PipeWire automatically detect the capture subdevice or load it using:
     ```bash
     pacmd load-module module-alsa-source device=hw:Loopback,1,0
     ```

---

## 3. Audio Network Streaming Protocol

The client requests and pulls audio from the DroidCam mobile app over Wi-Fi or USB (ADB).

1. **Connection Setup**:
   - In [`AudioThreadProc()`](file:///home/basel/Work/python/virtual_source/droidcam-linux-client/src/av.c#L195-L284), the client sends an initial HTTP-style request string `CMD /v2/audio` defined in [`common.h`](file:///home/basel/Work/python/virtual_source/droidcam-linux-client/src/common.h#L26).
   - **Transport Priority**: The client tries **UDP** stream mode first (port `4748` = default port + 1) for lower latency. If UDP packets fail or an ADB/iOS USB connection is used, it falls back to **TCP**.
2. **Stream Handshake Protocol**:
   - The phone responds with stream parameters: `-@v02` header followed by `CHUNKS_PER_PACKET = 2`.
   - The packet layout uses fixed parameters defined in [`decoder.h`](file:///home/basel/Work/python/virtual_source/droidcam-linux-client/src/decoder.h#L51-L59):
     - **Sample Rate**: 16,000 Hz (16 kHz Wideband).
     - **Channels**: 1 (Mono).
     - **Chunk Duration**: 20 ms per frame.
     - **Compressed Chunk Size**: 70 bytes per Speex chunk (`DROIDCAM_SPX_CHUNK_BYTES_2 = 70`).
     - **Decoded Frame Size**: 320 PCM 16-bit samples = 640 bytes (`DROIDCAM_PCM_CHUNK_BYTES_2 = 640`).

---

## 4. Audio Decoding & Packet Loss Concealment (PLC)

The core decoding logic is defined in [`src/decoder.c`](file:///home/basel/Work/python/virtual_source/droidcam-linux-client/src/decoder.c) and [`src/decoder_snd.c`](file:///home/basel/Work/python/virtual_source/droidcam-linux-client/src/decoder_snd.c):

1. **Speex Wideband Decoder Initialization**:
   In [`decoder_init()`](file:///home/basel/Work/python/virtual_source/droidcam-linux-client/src/decoder.c#L142-L155):
   ```c
   speex_bits_init(&spx_decoder.bits);
   spx_decoder.state = speex_decoder_init(speex_lib_get_mode(SPEEX_MODEID_WB));
   speex_decoder_ctl(spx_decoder.state, SPEEX_GET_FRAME_SIZE, &spx_decoder.frame_size);
   ```
2. **Frame Decoding**:
   When network audio bytes arrive, [`decode_speex_frame()`](file:///home/basel/Work/python/virtual_source/droidcam-linux-client/src/decoder.c#L466-L483) reads the 70-byte compressed chunks and decodes them into raw 16-bit signed PCM audio samples:
   ```c
   speex_bits_read_from(&spx_decoder.bits, &stream_buf[i * 70], 70);
   speex_decode_int(spx_decoder.state, &spx_decoder.bits, &decode_buf[output_used]);
   ```
3. **Packet Loss Concealment (PLC)**:
   If a network stutter occurs and no packet arrives in time, [`decoder_speex_plc()`](file:///home/basel/Work/python/virtual_source/droidcam-linux-client/src/decoder.c#L455-L464) invokes Speex PLC by passing `NULL` to `speex_decode_int`:
   ```c
   speex_decode_int(spx_decoder.state, NULL, &output_buffer[transfer->offset]);
   ```
   This generates smooth synthetic audio data instead of creating pops, clicks, or dropping frames.

---

## 5. Writing Audio to the ALSA Hardware Buffer (`snd_pcm_mmap`)

Rather than using high-latency `write()` calls, DroidCam opens the virtual ALSA playback device in **Direct Memory Access (MMAP)** mode for low audio latency.

1. **Finding and Opening the Device**:
   In [`find_snd_device()`](file:///home/basel/Work/python/virtual_source/droidcam-linux-client/src/decoder_snd.c#L248-L306):
   - Scans `/proc/asound/card*/id` for the text `"Loopback"`.
   - Opens playback hardware endpoint `hw:card_num,0,i` via `snd_pcm_open(..., SND_PCM_STREAM_PLAYBACK, 0)`.
   - Configures hardware params: 16 kHz sample rate, 1 channel, 16-bit signed PCM format (`SND_PCM_FORMAT_S16`), 20 ms period time (`PERIOD_TIME = 20000 us`), and `SND_PCM_ACCESS_MMAP_INTERLEAVED` access mode.
2. **Streaming Loop Execution**:
   Inside [`AudioThreadProc()`](file:///home/basel/Work/python/virtual_source/droidcam-linux-client/src/av.c#L286-L345):
   - Calls [`snd_transfer_check()`](file:///home/basel/Work/python/virtual_source/droidcam-linux-client/src/decoder_snd.c#L167-L229) to check available ring buffer space via `snd_pcm_avail_update()` and retrieve the MMAP buffer memory address via `snd_pcm_mmap_begin()`.
   - Copies decoded PCM samples directly into ALSA memory space using `memcpy`.
   - Calls [`snd_transfer_commit()`](file:///home/basel/Work/python/virtual_source/droidcam-linux-client/src/decoder_snd.c#L232-L245) (`snd_pcm_mmap_commit()`) to inform the kernel driver that new audio frames are ready.
   - Automatically handles buffer underruns (`-EPIPE`) via `xrun_recovery()`.

---

## 6. Execution Flow Summary Table

| Step | Location / Component | Function | Description |
|---|---|---|---|
| **1. Driver Setup** | `install-sound` | `modprobe snd-aloop` | Creates virtual ALSA loopback card (`Loopback`). |
| **2. Device Search** | [`src/decoder_snd.c`](file:///home/basel/Work/python/virtual_source/droidcam-linux-client/src/decoder_snd.c#L248) | `find_snd_device()` | Locates `hw:X,0,0` (Playback subdevice) and opens MMAP stream. |
| **3. Streaming** | [`src/av.c`](file:///home/basel/Work/python/virtual_source/droidcam-linux-client/src/av.c#L195) | `AudioThreadProc()` | Sends `CMD /v2/audio` over UDP/TCP socket to phone app. |
| **4. Decoding** | [`src/decoder.c`](file:///home/basel/Work/python/virtual_source/droidcam-linux-client/src/decoder.c#L466) | `decode_speex_frame()` | Decodes 70-byte Speex WB chunks to 16kHz 16-bit PCM. |
| **5. MMAP Push** | [`src/decoder_snd.c`](file:///home/basel/Work/python/virtual_source/droidcam-linux-client/src/decoder_snd.c#L232) | `snd_transfer_commit()` | Writes PCM data directly to ALSA Loopback playback buffer. |
| **6. OS Capture** | Kernel & PulseAudio | `hw:Loopback,1,0` | Virtual loopback bridge pipes playback audio into OS Microphone capture. |
