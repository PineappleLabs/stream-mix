#!/usr/bin/env python3
"""Send an 8-channel audio feed to stream-mix over RTSP (48 kHz).

Runs on the Windows laptop with GStreamer installed (MSVC runtime installer
plus python3 bindings: `pip install PyGObject` via the gvsbuild/GStreamer
setup, or use the GStreamer "complete" installer's Python).

Sources:
  jack    JACK server ports (recommended). Connect Ableton's outputs or the
          interface inputs to "stream-mix-sender:in_1..8" in QjackCtl, or pass
          --jack-autoconnect to use interface inputs 1-8.
  wasapi  A WASAPI capture device that exposes 8 channels (--device).
  test    Eight sine tones, one per channel, for wiring checks.

Codecs:
  pcm     Uncompressed, 24-bit (~9.2 Mbps) or --bit-depth 16 (~6.1 Mbps).
  opus    8 independent mono Opus streams, --opus-kbps total (~0.6 Mbps).

Publishes to rtsp://<host>:8554/<path>; set TRACKS_PATH=<path> on the server.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

CHANNELS = 8
RATE = 48000
# >2 channels need an explicit mask or caps won't parse; 0x0 = unpositioned.
MASK = "channel-mask=(bitmask)0x0"
# Seconds of audio to ride out a network stall with, before anything is lost:
# first in the send queue, then in the capture ring buffer behind it.
SEND_BUFFER_S = 2
CAPTURE_BUFFER_S = 1

# rtpopuspay only takes channel-mapping-family 1, which for 8 channels means
# 7.1 surround, and a 7.1 encode low-passes the LFE channel. So encode family
# 255 (8 independent mono streams, no LFE) and relabel it as family 1 for the
# payloader. The receiver's decoder then reorders 7.1 Vorbis order into
# GStreamer order; pre-apply the inverse so tracks arrive as 1..8.
OPUS_ORDER = (0, 2, 1, 6, 7, 4, 5, 3)
OPUS_MATRIX = "<" + ",".join(
    "<" + ",".join("(float)1" if col == src else "(float)0" for col in range(CHANNELS)) + ">"
    for src in OPUS_ORDER
) + ">"


def build_source(args: argparse.Namespace) -> str:
    caps = f"audio/x-raw,rate={RATE},channels={CHANNELS},{MASK}"
    if args.source == "jack":
        # auto: wire in_1..8 to the first 8 physical capture ports on every
        # (re)start; none: leave patching to QjackCtl.
        connect = "auto" if args.jack_autoconnect else "none"
        return (
            f"jackaudiosrc client-name=stream-mix-sender connect={connect} "
            f"buffer-time={CAPTURE_BUFFER_S * 1_000_000} "
            f"! {caps} ! audioconvert"
        )
    if args.source == "wasapi":
        device = f' device="{args.device}"' if args.device else ""
        return f"wasapi2src{device} low-latency=true ! audioconvert ! audioresample ! {caps}"
    tones = " ".join(
        f"audiotestsrc is-live=true freq={200 + n * 100} volume=0.2 "
        f"! audio/x-raw,channels=1,rate={RATE} ! mix.sink_{n}"
        for n in range(CHANNELS)
    )
    return f"interleave name=mix {tones} mix."


def log(message: str) -> None:
    """Timestamped, so a crackle heard in the recording can be matched to a cause."""
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def build_encoder(args: argparse.Namespace) -> tuple[str, str]:
    """Return (raw audio -> encoded pipeline fragment, RTP payloader name)."""
    if args.codec == "opus":
        return (
            f'! audio/x-raw,format=F32LE,rate={RATE},channels={CHANNELS},{MASK} '
            f'! audioconvert mix-matrix="{OPUS_MATRIX}" '
            f"! audio/x-raw,channels={CHANNELS},{MASK} "
            f"! opusenc bitrate={args.opus_kbps * 1000} bitrate-type=vbr "
            # 10 ms frames keep packets under the MTU at full bitrate.
            f"audio-type=generic frame-size=10 "
            f'! capssetter caps="audio/x-opus,channel-mapping-family=(int)1" ',
            "rtpopuspay",
        )
    fmt, pay = ("S24BE", "rtpL24pay") if args.bit_depth == 24 else ("S16BE", "rtpL16pay")
    return f"! audio/x-raw,format={fmt},rate={RATE},channels={CHANNELS},{MASK} ", pay


def build_pipeline(args: argparse.Namespace) -> Gst.Pipeline:
    url = f"rtsp://{args.host}:{args.port}/{args.path}"
    encode, payloader = build_encoder(args)
    # Latency is not a concern (the stream runs seconds behind), so never drop:
    # a full queue blocks and the capture ring buffer absorbs the rest.
    desc = (
        f"{build_source(args)} {encode}"
        f"! queue name=sendq max-size-buffers=0 max-size-bytes=0 "
        f"max-size-time={SEND_BUFFER_S * Gst.SECOND} "
        f"! rtspclientsink name=sink location={url} protocols=tcp latency=200"
    )
    pipeline = Gst.parse_launch(desc)
    sink = pipeline.get_by_name("sink")
    # rtspclientsink can't auto-select a payloader for 8 channels.
    for pad in sink.sinkpads:
        pad.set_property("payloader", Gst.ElementFactory.make(payloader, "pay"))
    return pipeline


def run_once(args: argparse.Namespace) -> None:
    pipeline = build_pipeline(args)
    bus = pipeline.get_bus()

    # A full send queue means the network has stalled for SEND_BUFFER_S; nothing
    # is lost yet, but the capture buffer is now the last line of defence.
    last_overrun = 0.0

    def on_overrun(_queue: Gst.Element) -> None:
        nonlocal last_overrun
        now = time.monotonic()
        if now - last_overrun > 1:
            log(f"send queue full: network stalled >{SEND_BUFFER_S}s, capture buffer absorbing")
        last_overrun = now

    pipeline.get_by_name("sendq").connect("overrun", on_overrun)
    # rtspclientsink can block forever in set_state (e.g. mediamtx still holds a
    # stale publisher session after a crash); die so the supervisor restarts us.
    watchdog = threading.Timer(15, lambda: os._exit(2))
    watchdog.start()
    pipeline.set_state(Gst.State.PLAYING)
    pipeline.get_state(10 * Gst.SECOND)
    watchdog.cancel()
    codec = f"opus {args.opus_kbps} kbps" if args.codec == "opus" else f"pcm {args.bit_depth}-bit"
    log(f"sending {CHANNELS}ch {codec} to rtsp://{args.host}:{args.port}/{args.path}")
    try:
        while True:
            msg = bus.timed_pop_filtered(
                Gst.SECOND,
                Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.WARNING,
            )
            if msg is None:
                continue
            if msg.type == Gst.MessageType.WARNING:
                # e.g. the capture ring buffer overflowing: "Dropped N samples".
                warn, debug = msg.parse_warning()
                log(f"warning from {msg.src.get_name()}: {warn.message} ({debug})")
                continue
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                raise RuntimeError(f"{err.message} ({debug})")
            raise RuntimeError("unexpected end of stream")
    finally:
        pipeline.set_state(Gst.State.NULL)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", required=True, help="stream-mix / mediamtx host (big pineapple)")
    parser.add_argument("--port", type=int, default=8554, help="mediamtx RTSP port")
    parser.add_argument("--path", default="tracks", help="must match TRACKS_PATH on the server")
    parser.add_argument("--source", choices=("jack", "wasapi", "test"), default="jack")
    parser.add_argument("--device", help="WASAPI device id (wasapi source only)")
    parser.add_argument(
        "--jack-autoconnect",
        action="store_true",
        help="connect to system:capture_1..8 automatically (jack source only)",
    )
    parser.add_argument("--codec", choices=("pcm", "opus"), default="pcm")
    parser.add_argument(
        "--bit-depth", type=int, choices=(16, 24), default=24, help="pcm codec only"
    )
    parser.add_argument(
        "--opus-kbps",
        type=int,
        default=640,
        help="total Opus bitrate for all 8 tracks, 32-650 (opus codec only)",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.codec == "opus" and not 32 <= args.opus_kbps <= 650:
        parser.error("--opus-kbps must be 32-650 (opusenc's limit for all channels combined)")

    if not args.worker:
        cmd = [sys.executable, os.path.abspath(__file__), *sys.argv[1:], "--worker"]
        while True:
            try:
                subprocess.run(cmd, check=False)
                log("sender exited (audio gap); restarting in 3s")
                time.sleep(3)
            except KeyboardInterrupt:
                return

    Gst.init(None)
    codec_elems = (
        ("opusenc", "rtpopuspay", "capssetter")
        if args.codec == "opus"
        else ("rtpL24pay",) if args.bit_depth == 24 else ("rtpL16pay",)
    )
    for name in ("rtspclientsink", *codec_elems) + (
        ("jackaudiosrc",) if args.source == "jack" else ("wasapi2src",) if args.source == "wasapi" else ()
    ):
        if Gst.ElementFactory.find(name) is None:
            sys.exit(f"GStreamer element '{name}' not found; check your GStreamer install")

    try:
        run_once(args)
    except KeyboardInterrupt:
        return
    except RuntimeError as exc:
        sys.exit(f"stream error: {exc}")


if __name__ == "__main__":
    main()
