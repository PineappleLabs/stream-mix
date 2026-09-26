#!/usr/bin/env python3
"""Send an 8-channel audio feed to stream-mix over RTSP (48 kHz, 16-bit, uncompressed).

Runs on the Windows laptop with GStreamer installed (MSVC runtime installer
plus python3 bindings: `pip install PyGObject` via the gvsbuild/GStreamer
setup, or use the GStreamer "complete" installer's Python).

Sources:
  jack    JACK server ports (recommended). Connect Ableton's outputs or the
          interface inputs to "stream-mix-sender:in_1..8" in QjackCtl.
  wasapi  A WASAPI capture device that exposes 8 channels (--device).
  test    Eight sine tones, one per channel, for wiring checks.

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


def build_source(args: argparse.Namespace) -> str:
    caps = f"audio/x-raw,rate={RATE},channels={CHANNELS}"
    if args.source == "jack":
        return (
            "jackaudiosrc client-name=stream-mix-sender connect=none "
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


def build_pipeline(args: argparse.Namespace) -> Gst.Pipeline:
    url = f"rtsp://{args.host}:{args.port}/{args.path}"
    desc = (
        f"{build_source(args)} "
        f"! audio/x-raw,format=S16BE,rate={RATE},channels={CHANNELS} "
        f"! queue max-size-time=200000000 leaky=downstream "
        f"! rtspclientsink name=sink location={url} protocols=tcp latency=200"
    )
    pipeline = Gst.parse_launch(desc)
    sink = pipeline.get_by_name("sink")
    # rtspclientsink can't auto-select a payloader for 8 channels.
    for pad in sink.sinkpads:
        pad.set_property("payloader", Gst.ElementFactory.make("rtpL16pay", "pay"))
    return pipeline


def run_once(args: argparse.Namespace) -> None:
    pipeline = build_pipeline(args)
    bus = pipeline.get_bus()
    # rtspclientsink can block forever in set_state (e.g. mediamtx still holds a
    # stale publisher session after a crash); die so the supervisor restarts us.
    watchdog = threading.Timer(15, lambda: os._exit(2))
    watchdog.start()
    pipeline.set_state(Gst.State.PLAYING)
    pipeline.get_state(10 * Gst.SECOND)
    watchdog.cancel()
    print(f"sending {CHANNELS}ch to rtsp://{args.host}:{args.port}/{args.path}", flush=True)
    try:
        while True:
            msg = bus.timed_pop_filtered(
                Gst.SECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS
            )
            if msg is None:
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
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if not args.worker:
        cmd = [sys.executable, os.path.abspath(__file__), *sys.argv[1:], "--worker"]
        while True:
            try:
                subprocess.run(cmd, check=False)
                print("sender exited; restarting in 3s", flush=True)
                time.sleep(3)
            except KeyboardInterrupt:
                return

    Gst.init(None)
    for name in ("rtspclientsink", "rtpL16pay") + (
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
