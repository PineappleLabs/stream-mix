#!/usr/bin/env python3
"""Studio mix: GoPro video + blended audio -> program / Twitch / YouTube."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GLib, GstVideo

APP_DIR = Path("/app")
STATE_PATH = Path(os.environ.get("STATE_PATH", "/config/mix-state.json"))
STATIC_DIR = APP_DIR / "static"
FALLBACK_VIDEO = Path(
    os.environ.get("FALLBACK_VIDEO_PATH", "/app/assets/citadelSkullVisualizer.mp4")
)

DEFAULT_STATE = {
    "gopro_video_delay_ms": 0,
    "gopro_audio_delay_ms": 0,
    "ableton_audio_delay_ms": 2500,
    "gopro_audio_level": 0.25,
    "ableton_audio_level": 1.0,
    "twitch_enabled": False,
    "youtube_enabled": False,
    "video_rotation": 0,
    "stream_video_bitrate_kbps": 2500,
    "stream_audio_bitrate_kbps": 160,
}

# UI degrees -> GStreamer videoflip method enum
VIDEO_ROTATION_METHODS = {
    0: 0,
    90: 1,
    180: 2,
    270: 3,
}

PLATFORM_SINKS = ("twitch", "youtube")
MAX_DELAY_MS = 60_000

MEDIAMTX_HOST = os.environ.get("MEDIAMTX_HOST", "mediamtx")
MEDIAMTX_RTSP_PORT = os.environ.get("MEDIAMTX_RTSP_PORT", "8554")
MEDIAMTX_RTMP_PORT = os.environ.get("MEDIAMTX_RTMP_PORT", "1935")

CAM_PATH = os.environ["CAM_PATH"]
ABLETON_PATH = os.environ["ABLETON_PATH"]
PROGRAM_PATH = os.environ["PROGRAM_PATH"]


def ms_to_ns(ms: int) -> int:
    return int(ms) * 1_000_000


class StudioMixer:
    def __init__(self) -> None:
        Gst.init(None)
        self.state = self.load_state()
        self.pipeline: Gst.Pipeline | None = None
        self.elements: dict[str, Gst.Element] = {}
        self.main_loop: GLib.MainLoop | None = None
        self.pipeline_running = False
        self.cam_ready = False
        self.ableton_ready = False
        self.cam_using_fallback = False
        self.lock = threading.Lock()
        self._stop = False
        self._monitor_stop = threading.Event()
        self._vselector_cam_pad: Gst.Pad | None = None
        self._vselector_fb_pad: Gst.Pad | None = None
        self._pipeline_thread_id: int | None = None

    def load_state(self) -> dict:
        if STATE_PATH.exists():
            with STATE_PATH.open() as fh:
                data = json.load(fh)
            return {**DEFAULT_STATE, **data}
        return dict(DEFAULT_STATE)

    def save_state(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with STATE_PATH.open("w") as fh:
            json.dump(self.state, fh, indent=2)
            fh.write("\n")

    def rtsp_url(self, path: str) -> str:
        return f"rtsp://{MEDIAMTX_HOST}:{MEDIAMTX_RTSP_PORT}/{path}"

    def probe_rtsp(self, path: str, timeout: int = 8) -> bool:
        url = self.rtsp_url(path)
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-rtsp_transport",
                    "tcp",
                    "-i",
                    url,
                    "-show_streams",
                    "-loglevel",
                    "error",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return result.returncode == 0 and "[STREAM]" in (result.stdout + result.stderr)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def fallback_available(self) -> bool:
        return FALLBACK_VIDEO.is_file()

    def check_sources(self) -> bool:
        self.cam_ready = self.probe_rtsp(CAM_PATH)
        self.ableton_ready = self.probe_rtsp(ABLETON_PATH)
        return self.cam_ready and self.ableton_ready

    def sources_ready(self) -> bool:
        self.cam_ready = self.probe_rtsp(CAM_PATH)
        self.ableton_ready = self.probe_rtsp(ABLETON_PATH)
        return self.ableton_ready and (self.cam_ready or self.fallback_available())

    def platform_configured(self, platform: str) -> bool:
        env_key = f"{platform.upper()}_STREAM_KEY"
        return bool(os.environ.get(env_key, "").strip())

    def platform_url(self, platform: str) -> str | None:
        if platform == "twitch":
            key = os.environ.get("TWITCH_STREAM_KEY", "").strip()
            return f"rtmp://live.twitch.tv/app/{key}" if key else None
        if platform == "youtube":
            key = os.environ.get("YOUTUBE_STREAM_KEY", "").strip()
            return f"rtmp://a.rtmp.youtube.com/live2/{key}" if key else None
        return None

    def get_rtmp_sinks(self) -> list[tuple[str, str]]:
        sinks = [
            (
                "program",
                f"rtmp://{MEDIAMTX_HOST}:{MEDIAMTX_RTMP_PORT}/{PROGRAM_PATH}",
            )
        ]
        for platform in PLATFORM_SINKS:
            url = self.platform_url(platform)
            if url:
                sinks.append((platform, url))
        return sinks

    def platform_streaming(self, platform: str) -> bool:
        if not self.platform_configured(platform):
            return False
        enabled_key = f"{platform}_enabled"
        return (
            bool(self.state.get(enabled_key))
            and self.pipeline_running
        )

    @staticmethod
    def make_queue(name: str) -> Gst.Element:
        queue = Gst.ElementFactory.make("queue", name)
        queue.set_property("max-size-buffers", 0)
        queue.set_property("max-size-bytes", 0)
        queue.set_property("max-size-time", 0)
        queue.set_property("leaky", 2)
        return queue

    @staticmethod
    def make_delay_queue(name: str) -> Gst.Element:
        queue = Gst.ElementFactory.make("queue", name)
        queue.set_property("max-size-buffers", 0)
        queue.set_property("max-size-bytes", 0)
        queue.set_property("max-size-time", 0)
        return queue

    @staticmethod
    def make_rtmp_queue(name: str) -> Gst.Element:
        """RTMP branch queues must not drop audio while flvmux waits for delayed video."""
        queue = Gst.ElementFactory.make("queue", name)
        queue.set_property("max-size-buffers", 0)
        queue.set_property("max-size-bytes", 0)
        queue.set_property("max-size-time", ms_to_ns(MAX_DELAY_MS))
        return queue

    def _flvmux_latency_ns(self) -> int:
        video_delay = int(self.state.get("gopro_video_delay_ms", 0))
        audio_delay = int(self.state.get("ableton_audio_delay_ms", 0))
        return ms_to_ns(max(video_delay, audio_delay, 0))

    def _apply_flvmux_latency(self) -> None:
        latency_ns = self._flvmux_latency_ns()
        for key, elem in self.elements.items():
            if key.startswith("mux_"):
                elem.set_property("latency", latency_ns)
                elem.set_property("min-upstream-latency", latency_ns)

    def apply_state_to_elements(self) -> None:
        delay_map = {
            "cam_video_queue": "gopro_video_delay_ms",
        }
        audio_delay_map = {
            "cam_mix_pad": "gopro_audio_delay_ms",
            "abl_mix_pad": "ableton_audio_delay_ms",
        }
        level_map = {
            "cam_vol": "gopro_audio_level",
            "abl_vol": "ableton_audio_level",
        }
        for elem_name, state_key in delay_map.items():
            elem = self.elements.get(elem_name)
            if elem:
                delay_ms = int(self.state[state_key])
                elem.set_property("min-threshold-time", ms_to_ns(delay_ms))
        for elem_name, state_key in audio_delay_map.items():
            mix_pad = self.elements.get(elem_name)
            if mix_pad:
                delay_ms = int(self.state[state_key])
                mix_pad.set_property("offset", ms_to_ns(delay_ms))
        for elem_name, state_key in level_map.items():
            elem = self.elements.get(elem_name)
            if elem:
                if elem_name == "cam_vol" and self.cam_using_fallback:
                    elem.set_property("volume", 0.0)
                else:
                    elem.set_property("volume", float(self.state[state_key]))
        for platform in PLATFORM_SINKS:
            enabled = bool(self.state.get(f"{platform}_enabled"))
            self._set_platform_valve(platform, drop=not enabled)
        videoflip = self.elements.get("videoflip")
        if videoflip:
            rotation = int(self.state.get("video_rotation", 0))
            method = VIDEO_ROTATION_METHODS.get(rotation, 0)
            videoflip.set_property("method", method)
        venc = self.elements.get("venc")
        if venc:
            venc.set_property("bitrate", int(self.state.get("stream_video_bitrate_kbps", 2500)))
        aenc = self.elements.get("aenc")
        if aenc:
            aenc.set_property("bitrate", int(self.state.get("stream_audio_bitrate_kbps", 160)) * 1000)
        self._apply_flvmux_latency()

    def update_state(self, new_state: dict) -> None:
        with self.lock:
            self.state.update(new_state)
            self.save_state()
            self.apply_state_to_elements()

    def _run_on_main(self, fn, timeout: float = 15.0):
        if threading.get_ident() == self._pipeline_thread_id:
            return fn()
        if not self.main_loop:
            raise RuntimeError("pipeline not running")
        done = threading.Event()
        result: list = []
        exc: list[BaseException] = []

        def invoke_fn() -> bool:
            try:
                result.append(fn())
            except BaseException as err:
                exc.append(err)
            finally:
                done.set()
            return False

        self.main_loop.get_context().invoke_full(GLib.PRIORITY_DEFAULT, invoke_fn)
        if not done.wait(timeout):
            raise RuntimeError("pipeline main loop timeout")
        if exc:
            raise exc[0]
        return result[0] if result else None

    def _set_platform_valve(self, platform: str, *, drop: bool) -> None:
        valve = self.elements.get(f"valve_{platform}")
        if valve:
            valve.set_property("drop", drop)

    def _force_video_keyframe(self) -> None:
        venc = self.elements.get("venc")
        if not venc:
            return
        pad = venc.get_static_pad("sink")
        if not pad:
            return
        event = GstVideo.video_event_new_downstream_force_key_unit(
            Gst.CLOCK_TIME_NONE,
            Gst.CLOCK_TIME_NONE,
            Gst.CLOCK_TIME_NONE,
            True,
            0,
        )
        pad.send_event(event)

    def _start_platform_output(self, platform: str) -> None:
        valve = self.elements.get(f"valve_{platform}")
        sink = self.elements.get(f"sink_{platform}")
        if not valve:
            raise RuntimeError(f"{platform} output branch not ready")
        valve.set_property("drop", True)
        if sink:
            sink.set_state(Gst.State.NULL)
            sink.set_state(Gst.State.PLAYING)
        self._force_video_keyframe()
        valve.set_property("drop", False)
        with self.lock:
            self.state[f"{platform}_last_error"] = None
        print(f"RTMP output started: {platform}", flush=True)

    def _start_platform_output_safe(self, platform: str) -> None:
        self._run_on_main(lambda: self._start_platform_output(platform))

    def _stop_platform_output(self, platform: str) -> None:
        self._set_platform_valve(platform, drop=True)
        sink = self.elements.get(f"sink_{platform}")
        if sink:
            sink.set_state(Gst.State.NULL)

    def _stop_platform_output_safe(self, platform: str) -> None:
        self._run_on_main(lambda: self._stop_platform_output(platform))

    def _platform_from_element(self, element: Gst.Element | None) -> str | None:
        if element is None:
            return None
        name = element.get_name()
        for platform in PLATFORM_SINKS:
            if platform in name:
                return platform
        return None

    def _handle_platform_error(self, platform: str, err_msg: str = "RTMP connection failed") -> None:
        print(f"RTMP output dropped: {platform} — {err_msg}", flush=True)
        self._stop_platform_output(platform)
        with self.lock:
            self.state[f"{platform}_enabled"] = False
            self.state[f"{platform}_last_error"] = err_msg
            self.save_state()

    def set_platform_enabled(self, platform: str, enabled: bool) -> None:
        if platform not in PLATFORM_SINKS:
            raise ValueError(f"unknown platform: {platform}")
        if enabled and not self.platform_configured(platform):
            raise ValueError(f"{platform} stream key not configured")
        key = f"{platform}_enabled"
        if enabled:
            self._start_platform_output_safe(platform)
            with self.lock:
                self.state[key] = True
                self.save_state()
        else:
            self._stop_platform_output_safe(platform)
            with self.lock:
                self.state[key] = False
                self.save_state()

    def public_state(self) -> dict:
        return {
            **self.state,
            "pipeline_running": self.pipeline_running,
            "cam_ready": self.cam_ready,
            "ableton_ready": self.ableton_ready,
            "cam_using_fallback": self.cam_using_fallback,
            "fallback_video_available": self.fallback_available(),
            "twitch_configured": self.platform_configured("twitch"),
            "youtube_configured": self.platform_configured("youtube"),
            "twitch_streaming": self.platform_streaming("twitch"),
            "youtube_streaming": self.platform_streaming("youtube"),
            "twitch_last_error": self.state.get("twitch_last_error"),
            "youtube_last_error": self.state.get("youtube_last_error"),
        }

    def select_video_source(self, use_fallback: bool) -> None:
        selector = self.elements.get("vselector")
        if not selector:
            return
        pad = self._vselector_fb_pad if use_fallback else self._vselector_cam_pad
        if pad:
            selector.set_property("active-pad", pad)
        self.cam_using_fallback = use_fallback
        cam_vol = self.elements.get("cam_vol")
        if cam_vol:
            level = 0.0 if use_fallback else float(self.state["gopro_audio_level"])
            cam_vol.set_property("volume", level)
        label = "fallback video" if use_fallback else "camera"
        print(f"video source: {label}", flush=True)

    def loop_fallback_video(self) -> None:
        filesrc = self.elements.get("fb_filesrc")
        if not filesrc:
            return
        filesrc.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            0,
        )

    def _monitor_sources(self) -> None:
        while not self._monitor_stop.wait(3):
            if not self.pipeline_running:
                continue
            cam_live = self.probe_rtsp(CAM_PATH, timeout=3)
            with self.lock:
                self.cam_ready = cam_live
                if not self.pipeline or not self.elements:
                    continue
                if cam_live and self.cam_using_fallback:
                    GLib.idle_add(self.select_video_source, False)
                elif not cam_live and not self.cam_using_fallback and self.fallback_available():
                    GLib.idle_add(self.select_video_source, True)

    def _start_monitor(self) -> None:
        self._monitor_stop.clear()
        threading.Thread(target=self._monitor_sources, daemon=True).start()

    def _stop_monitor(self) -> None:
        self._monitor_stop.set()

    def _is_cam_element(self, element: Gst.Element | None) -> bool:
        if element is None:
            return False
        name = element.get_name()
        # Any cam RTSP branch (video or mic) may error when the GoPro drops; keep
        # Ableton audio and fallback video running instead of stopping the pipeline.
        return name == "h264parse" or name.startswith("cam_")

    def _is_fallback_element(self, element: Gst.Element | None) -> bool:
        if element is None:
            return False
        return element.get_name().startswith("fb_")

    def _is_platform_sink_element(self, element: Gst.Element | None) -> bool:
        if element is None:
            return False
        name = element.get_name()
        return name.startswith(
            (
                "sink_twitch",
                "sink_youtube",
                "mux_twitch",
                "mux_youtube",
                "valve_twitch",
                "valve_youtube",
            )
        )

    @staticmethod
    def link_many(*elements: Gst.Element) -> None:
        for left, right in zip(elements, elements[1:]):
            if not left.link(right):
                raise RuntimeError(f"failed to link {left.name} -> {right.name}")

    @staticmethod
    def link_tee(tee: Gst.Element, target: Gst.Element) -> None:
        pad = tee.request_pad_simple("src_%u")
        if not pad:
            raise RuntimeError("failed to request tee pad")
        sink_pad = target.get_static_pad("sink")
        if pad.link(sink_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"failed to link tee -> {target.name}")

    def add_audio_branch(
        self,
        pipeline: Gst.Pipeline,
        rtp_pad: Gst.Pad,
        queue: Gst.Element,
        volume: Gst.Element,
        mix: Gst.Element,
        branch_prefix: str,
    ) -> None:
        depay = Gst.ElementFactory.make("rtpmp4adepay", f"{branch_prefix}_depay")
        parse = Gst.ElementFactory.make("aacparse", f"{branch_prefix}_parse")
        decode = Gst.ElementFactory.make("avdec_aac", f"{branch_prefix}_dec")
        convert = Gst.ElementFactory.make("audioconvert", f"{branch_prefix}_convert")
        resample = Gst.ElementFactory.make("audioresample", f"{branch_prefix}_resample")
        for elem in (depay, parse, decode, convert, resample):
            pipeline.add(elem)
            elem.sync_state_with_parent()
        if rtp_pad.link(depay.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            depay = Gst.ElementFactory.make("rtpmp4gdepay", f"{branch_prefix}_depay2")
            decode = Gst.ElementFactory.make("avdec_aac", f"{branch_prefix}_dec2")
            convert = Gst.ElementFactory.make("audioconvert", f"{branch_prefix}_convert2")
            resample = Gst.ElementFactory.make("audioresample", f"{branch_prefix}_resample2")
            for elem in (depay, decode, convert, resample):
                pipeline.add(elem)
                elem.sync_state_with_parent()
            rtp_pad.link(depay.get_static_pad("sink"))
            self.link_many(depay, decode, convert, resample, queue)
        else:
            self.link_many(depay, parse, decode, convert, resample, queue)
        self.link_many(queue, volume)
        mix_pad = mix.request_pad_simple("sink_%u")
        if not mix_pad:
            raise RuntimeError("failed to request audiomixer pad")
        volume.get_static_pad("src").link(mix_pad)
        self.elements[f"{branch_prefix}_mix_pad"] = mix_pad
        delay_key = (
            "ableton_audio_delay_ms" if branch_prefix == "abl" else "gopro_audio_delay_ms"
        )
        mix_pad.set_property("offset", ms_to_ns(int(self.state[delay_key])))

    @staticmethod
    def make_element(factory: str, name: str) -> Gst.Element:
        elem = Gst.ElementFactory.make(factory, name)
        if not elem:
            raise RuntimeError(f"failed to create GStreamer element {factory}")
        return elem

    def build_pipeline(self) -> Gst.Pipeline:
        pipeline = Gst.Pipeline.new("studio-mix")
        elements: dict[str, Gst.Element] = {}

        cam_src = self.make_element("rtspsrc", "cam_src")
        cam_src.set_property("location", self.rtsp_url(CAM_PATH))
        cam_src.set_property("protocols", "tcp")
        cam_src.set_property("latency", 200)
        cam_src.set_property("drop-on-latency", True)

        abl_src = self.make_element("rtspsrc", "abl_src")
        abl_src.set_property("location", self.rtsp_url(ABLETON_PATH))
        abl_src.set_property("protocols", "tcp")
        abl_src.set_property("latency", 200)
        abl_src.set_property("drop-on-latency", True)
        abl_src.set_property("do-rtsp-keep-alive", True)

        cam_video_queue = self.make_delay_queue("cam_video_queue")
        depay = self.make_element("rtph264depay", "cam_h264_depay")
        h264parse = self.make_element("h264parse", "h264parse")
        cam_sel_queue = self.make_queue("cam_sel_queue")
        vselector = self.make_element("input-selector", "vselector")

        fb_filesrc = self.make_element("filesrc", "fb_filesrc")
        fb_filesrc.set_property("location", str(FALLBACK_VIDEO))
        fb_demux = self.make_element("qtdemux", "fb_demux")
        fb_h264parse = self.make_element("h264parse", "fb_h264parse")
        fb_video_queue = self.make_queue("fb_video_queue")

        h264parse_post = self.make_element("h264parse", "h264parse_post")
        vdec = self.make_element("avdec_h264", "vdec")
        vconvert = self.make_element("videoconvert", "vconvert")
        videoflip = self.make_element("videoflip", "videoflip")
        vscale = self.make_element("videoscale", "vscale")
        vrate = self.make_element("videorate", "vrate")
        vcaps = self.make_element("capsfilter", "vcaps")
        vcaps.set_property(
            "caps",
            Gst.Caps.from_string("video/x-raw,width=1280,height=720,framerate=30/1"),
        )
        venc = self.make_element("x264enc", "venc")
        venc.set_property("speed-preset", "ultrafast")
        venc.set_property("tune", "zerolatency")
        venc.set_property("key-int-max", 60)
        venc.set_property("bitrate", int(self.state.get("stream_video_bitrate_kbps", 2500)))
        h264parse_out = self.make_element("h264parse", "h264parse_out")

        vtee = self.make_element("tee", "vtee")
        vtee.set_property("allow-not-linked", True)

        cam_audio_queue = self.make_queue("cam_audio_queue")
        cam_vol = self.make_element("volume", "cam_vol")
        abl_audio_queue = self.make_queue("abl_audio_queue")
        abl_vol = self.make_element("volume", "abl_vol")

        mix = self.make_element("audiomixer", "mix")
        mix.set_property("start-time-selection", 1)

        aconvert = self.make_element("audioconvert", "aconvert")
        aresample = self.make_element("audioresample", "aresample")
        aenc = self.make_element("avenc_aac", "aenc")
        aenc.set_property("bitrate", int(self.state.get("stream_audio_bitrate_kbps", 160)) * 1000)
        aacparse = self.make_element("aacparse", "aacparse")
        atee = self.make_element("tee", "atee")
        atee.set_property("allow-not-linked", True)

        elements.update(
            {
                "cam_video_queue": cam_video_queue,
                "cam_audio_queue": cam_audio_queue,
                "abl_audio_queue": abl_audio_queue,
                "cam_vol": cam_vol,
                "abl_vol": abl_vol,
                "vselector": vselector,
                "fb_filesrc": fb_filesrc,
                "videoflip": videoflip,
                "venc": venc,
                "aenc": aenc,
                "h264parse_out": h264parse_out,
            }
        )

        base_elems = [
            cam_src,
            abl_src,
            depay,
            h264parse,
            cam_sel_queue,
            vselector,
            cam_video_queue,
            h264parse_post,
            vdec,
            vconvert,
            videoflip,
            vscale,
            vrate,
            vcaps,
            venc,
            h264parse_out,
            fb_filesrc,
            fb_demux,
            fb_h264parse,
            fb_video_queue,
            vtee,
            cam_audio_queue,
            cam_vol,
            abl_audio_queue,
            abl_vol,
            mix,
            aconvert,
            aresample,
            aenc,
            aacparse,
            atee,
        ]
        for elem in base_elems:
            pipeline.add(elem)

        self.link_many(depay, h264parse, cam_sel_queue)
        self.link_many(fb_filesrc, fb_demux)
        self.link_many(fb_h264parse, fb_video_queue)
        self._vselector_cam_pad = vselector.request_pad_simple("sink_%u")
        self._vselector_fb_pad = vselector.request_pad_simple("sink_%u")
        if not self._vselector_cam_pad or not self._vselector_fb_pad:
            raise RuntimeError("failed to request input-selector pads")
        cam_sel_queue.get_static_pad("src").link(self._vselector_cam_pad)
        fb_video_queue.get_static_pad("src").link(self._vselector_fb_pad)
        self.link_many(
            vselector,
            cam_video_queue,
            h264parse_post,
            vdec,
            vconvert,
            videoflip,
            vscale,
            vrate,
            vcaps,
            venc,
            h264parse_out,
            vtee,
        )
        self.link_many(mix, aconvert, aresample, aenc, aacparse, atee)

        def on_fb_pad_added(_demux: Gst.Element, pad: Gst.Pad, _user_data: object) -> None:
            caps = pad.get_current_caps()
            if not caps:
                return
            media = caps.get_structure(0).get_name()
            if media.startswith("video/"):
                pad.link(fb_h264parse.get_static_pad("sink"))
            elif media.startswith("audio/"):
                # Drop the visualizer's embedded audio; program audio is Ableton + cam mic.
                fb_audio_sink = self.make_element("fakesink", "fb_audio_sink")
                pipeline.add(fb_audio_sink)
                fb_audio_sink.sync_state_with_parent()
                pad.link(fb_audio_sink.get_static_pad("sink"))

        fb_demux.connect("pad-added", on_fb_pad_added, None)

        for name, url in self.get_rtmp_sinks():
            vqueue = self.make_rtmp_queue(f"vqueue_{name}")
            aqueue = self.make_rtmp_queue(f"aqueue_{name}")
            mux = self.make_element("flvmux", f"mux_{name}")
            mux.set_property("streamable", True)
            latency_ns = self._flvmux_latency_ns()
            mux.set_property("latency", latency_ns)
            mux.set_property("min-upstream-latency", latency_ns)
            branch_elems = [vqueue, aqueue, mux]
            if name in PLATFORM_SINKS:
                valve = self.make_element("valve", f"valve_{name}")
                branch_elems.append(valve)
                elements[f"valve_{name}"] = valve
                elements[f"vqueue_{name}"] = vqueue
                elements[f"aqueue_{name}"] = aqueue
            sink = self.make_element("rtmpsink", f"sink_{name}")
            sink.set_property("location", url)
            sink.set_property("sync", False)
            sink.set_property("async", False)
            branch_elems.append(sink)
            elements[f"sink_{name}"] = sink
            elements[f"mux_{name}"] = mux
            for elem in branch_elems:
                pipeline.add(elem)
            self.link_tee(vtee, vqueue)
            self.link_tee(atee, aqueue)
            vqueue.get_static_pad("src").link(mux.request_pad_simple("video"))
            aqueue.get_static_pad("src").link(mux.request_pad_simple("audio"))
            if name in PLATFORM_SINKS:
                self.link_many(mux, valve, sink)
            else:
                self.link_many(mux, sink)

        def on_cam_pad_added(_src: Gst.Element, pad: Gst.Pad, _user_data: object) -> None:
            caps = pad.get_current_caps()
            if not caps:
                return
            media = caps.get_structure(0).get_string("media")
            if media == "video":
                pad.link(depay.get_static_pad("sink"))
            elif media == "audio":
                self.add_audio_branch(
                    pipeline, pad, cam_audio_queue, cam_vol, mix, "cam"
                )

        def on_abl_pad_added(_src: Gst.Element, pad: Gst.Pad, _user_data: object) -> None:
            caps = pad.get_current_caps()
            if not caps:
                return
            media = caps.get_structure(0).get_string("media")
            if media == "audio":
                self.add_audio_branch(
                    pipeline, pad, abl_audio_queue, abl_vol, mix, "abl"
                )

        cam_src.connect("pad-added", on_cam_pad_added, None)
        abl_src.connect("pad-added", on_abl_pad_added, None)

        self.elements = elements
        self.apply_state_to_elements()
        return pipeline

    def on_bus_message(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        msg_type = message.type
        src = message.src
        if msg_type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"pipeline error: {err} ({debug})", flush=True)
            if self._is_cam_element(src) and self.fallback_available():
                GLib.idle_add(self.select_video_source, True)
                if self.pipeline:
                    self.pipeline.set_state(Gst.State.PLAYING)
                return
            if self._is_platform_sink_element(src):
                platform = self._platform_from_element(src)
                if platform:
                    GLib.idle_add(self._handle_platform_error, platform, str(err))
                if self.pipeline:
                    self.pipeline.set_state(Gst.State.PLAYING)
                return
            self.pipeline_running = False
            if self.main_loop:
                self.main_loop.quit()
        elif msg_type == Gst.MessageType.EOS:
            if self._is_fallback_element(src):
                print("fallback video ended, looping", flush=True)
                GLib.idle_add(self.loop_fallback_video)
                return
            print("pipeline EOS", flush=True)
            self.pipeline_running = False
            if self.main_loop:
                self.main_loop.quit()

    def run_pipeline_once(self) -> None:
        if not self.sources_ready():
            return
        self.cam_using_fallback = not self.cam_ready
        self.pipeline = self.build_pipeline()
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_bus_message)
        self.pipeline.set_state(Gst.State.PLAYING)
        self.select_video_source(self.cam_using_fallback)
        self.pipeline_running = True
        for platform in PLATFORM_SINKS:
            if self.state.get(f"{platform}_enabled") and self.platform_configured(platform):
                try:
                    self._start_platform_output(platform)
                except RuntimeError as exc:
                    print(f"failed to start {platform}: {exc}", flush=True)
                    with self.lock:
                        self.state[f"{platform}_enabled"] = False
                        self.save_state()
            else:
                self._stop_platform_output(platform)
        self._start_monitor()
        assert self.main_loop is not None
        self.main_loop.run()
        self._stop_monitor()
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        self.pipeline = None
        self.elements = {}
        self._vselector_cam_pad = None
        self._vselector_fb_pad = None
        self.pipeline_running = False
        self.cam_using_fallback = False

    def pipeline_thread(self) -> None:
        self._pipeline_thread_id = threading.get_ident()
        while not self._stop:
            print("waiting for ableton (+ cam or fallback video)…", flush=True)
            while not self._stop and not self.sources_ready():
                time.sleep(2)
            if self._stop:
                return
            if self.cam_ready:
                print("sources live, starting pipeline with camera", flush=True)
            else:
                print("ableton live, starting pipeline with fallback video", flush=True)
            self.main_loop = GLib.MainLoop()
            self.run_pipeline_once()
            print("pipeline stopped, retrying in 3s", flush=True)
            time.sleep(3)


MIXER = StudioMixer()


class ControlHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            self.send_json(MIXER.public_state())
            return
        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        if parsed.path.startswith("/api/stream/"):
            parts = [p for p in parsed.path.split("/") if p]
            platform = parts[2] if len(parts) >= 3 else ""
            if platform not in PLATFORM_SINKS:
                self.send_error(404)
                return
            if "enabled" not in body:
                self.send_error(400, "missing enabled")
                return
            try:
                MIXER.set_platform_enabled(platform, bool(body["enabled"]))
            except (ValueError, RuntimeError) as exc:
                self.send_error(400, str(exc))
                return
            self.send_json({"ok": True, **MIXER.public_state()})
            return

        if parsed.path != "/api/state":
            self.send_error(404)
            return
        updates: dict = {}
        for key in DEFAULT_STATE:
            if key not in body:
                continue
            if key.endswith("_enabled"):
                updates[key] = bool(body[key])
            elif key == "video_rotation":
                rotation = int(body[key])
                updates[key] = rotation if rotation in VIDEO_ROTATION_METHODS else 0
            elif key == "stream_video_bitrate_kbps":
                updates[key] = max(800, min(4500, int(body[key])))
            elif key == "stream_audio_bitrate_kbps":
                updates[key] = max(96, min(320, int(body[key])))
            elif key.endswith("_level"):
                updates[key] = max(0.0, min(2.0, float(body[key])))
            else:
                updates[key] = max(0, min(MAX_DELAY_MS, int(body[key])))
        MIXER.update_state(updates)
        self.send_json({"ok": True})

    def send_json(self, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    threading.Thread(target=MIXER.pipeline_thread, daemon=True).start()
    port = int(os.environ.get("CONTROL_PORT", "8790"))
    server = HTTPServer(("0.0.0.0", port), ControlHandler)
    print(f"control UI listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
