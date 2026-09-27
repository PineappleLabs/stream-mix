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
gi.require_version("GstAudio", "1.0")
from gi.repository import Gst, GLib, GstAudio, GstVideo

APP_DIR = Path("/app")
STATE_PATH = Path(os.environ.get("STATE_PATH", "/config/mix-state.json"))
STATIC_DIR = APP_DIR / "static"
FALLBACK_VIDEO = Path(
    os.environ.get("FALLBACK_VIDEO_PATH", "/app/assets/fallback.mp4")
)

NUM_TRACKS = 8

DEFAULT_STATE = {
    "gopro_video_delay_ms": 0,
    "gopro_audio_delay_ms": 0,
    "ableton_audio_delay_ms": 2500,
    "tracks_audio_delay_ms": 2500,
    "gopro_audio_level": 0.25,
    "ableton_audio_level": 1.0,
    "gopro_audio_muted": False,
    "ableton_audio_muted": False,
    **{f"track_{i}_level": 1.0 for i in range(1, NUM_TRACKS + 1)},
    **{f"track_{i}_name": f"Track {i}" for i in range(1, NUM_TRACKS + 1)},
    **{f"track_{i}_muted": False for i in range(1, NUM_TRACKS + 1)},
    # 0 = no solo; otherwise the one track number that plays alone.
    "solo_track": 0,
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

# Master bus: drop the summed mix 6 dB, then catch the remaining overs with a
# look-ahead limiter (gain reduction, not waveshaping) just under full scale.
MASTER_HEADROOM = 0.5

# A missing Ableton feed is only rebuilt by restarting the pipeline; don't do that
# more often than this.
ABLETON_RESTART_COOLDOWN_S = 60
LIMIT_DB = -1.0
LIMIT_RELEASE_S = 0.2
# One float format for every summing stage, so nothing clips before the limiter.
MIX_CAPS = "audio/x-raw,format=F32LE,rate=48000,channels=2"
# How often to log packet loss and tracks clock drift.
HEALTH_LOG_INTERVAL_S = 60

MEDIAMTX_HOST = os.environ.get("MEDIAMTX_HOST", "mediamtx")
MEDIAMTX_RTSP_PORT = os.environ.get("MEDIAMTX_RTSP_PORT", "8554")
MEDIAMTX_RTMP_PORT = os.environ.get("MEDIAMTX_RTMP_PORT", "1935")

CAM_PATH = os.environ["CAM_PATH"]
ABLETON_PATH = os.environ["ABLETON_PATH"]
TRACKS_PATH = os.environ.get("TRACKS_PATH", "").strip()
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
        self.tracks_ready = False
        self._tracks_in_pipeline = False
        self._tracks_lost = False
        self._tracks_misses = 0
        self._tracks_elems: list[Gst.Element] = []
        self._abl_in_pipeline = False
        self._abl_lost = False
        self._abl_misses = 0
        self._abl_last_restart = 0.0
        self._mix: Gst.Element | None = None
        self._cam_src_dead = False
        self.cam_using_fallback = False
        self.lock = threading.Lock()
        self._stop = False
        self._monitor_stop = threading.Event()
        self._vselector_cam_pad: Gst.Pad | None = None
        self._vselector_fb_pad: Gst.Pad | None = None
        self._fb_parse_src: Gst.Pad | None = None
        self._pipeline_thread_id: int | None = None
        # "<feed>/<session>" -> [rtpjitterbuffer, stats at the last log]
        self._jitterbuffers: dict[str, list] = {}
        # [first pts, frames seen, rate, bytes per frame, latest drift ns]
        self._tracks_drift: list[int] | None = None

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
        self.tracks_ready = bool(TRACKS_PATH) and self.probe_rtsp(TRACKS_PATH)
        # The GoPro mic is the last-resort audio source.
        audio_ready = self.ableton_ready or self.tracks_ready or self.cam_ready
        return audio_ready and (self.cam_ready or self.fallback_available())

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

    def watch_jitterbuffers(self, src: Gst.Element, feed: str) -> None:
        """Keep each rtspsrc jitterbuffer so its loss/late counters can be logged."""

        def on_new_jitterbuffer(_bin, jitterbuffer, session, _ssrc) -> None:
            self._jitterbuffers[f"{feed}/{session}"] = [jitterbuffer, None]

        src.connect(
            "new-manager",
            lambda _src, manager: manager.connect("new-jitterbuffer", on_new_jitterbuffer),
        )

    def _measure_tracks_drift(self, pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        """Compare the feed's sample count with its timestamps (the server clock).

        audiomixer counts samples and resyncs to the timestamps (dropping or
        inserting audio, an audible click) once the two differ by more than its
        alignment-threshold, so a steady drift here predicts periodic clicks.
        """
        buf = info.get_buffer()
        if buf is None or buf.pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK
        drift = self._tracks_drift
        if drift is None:
            caps = pad.get_current_caps()
            audio = GstAudio.AudioInfo.new_from_caps(caps) if caps else None
            if not audio:
                return Gst.PadProbeReturn.OK
            drift = self._tracks_drift = [buf.pts, 0, audio.rate, audio.bpf, 0]
        first_pts, frames, rate, bpf, _ = drift
        drift[4] = buf.pts - (first_pts + frames * Gst.SECOND // rate)
        drift[1] = frames + buf.get_size() // bpf
        return Gst.PadProbeReturn.OK

    def log_audio_health(self) -> None:
        for key, entry in list(self._jitterbuffers.items()):
            jitterbuffer, last = entry
            stats = jitterbuffer.get_property("stats")
            now = {f: stats.get_value(f) for f in ("num-lost", "num-late")}
            if last is not None:
                lost = now["num-lost"] - last["num-lost"]
                late = now["num-late"] - last["num-late"]
                if lost or late:
                    print(
                        f"{key}: {lost} RTP packets lost, {late} arrived too late "
                        f"in the last {HEALTH_LOG_INTERVAL_S}s (audible gaps)",
                        flush=True,
                    )
            entry[1] = now
        drift = self._tracks_drift
        if drift is None or self._mix is None:
            return
        _, frames, rate, _, drift_ns = drift
        elapsed_s = frames / rate
        if elapsed_s < HEALTH_LOG_INTERVAL_S:
            return
        # Positive drift: fewer samples than the server clock expects (slow feed).
        ppm = -drift_ns / (elapsed_s * Gst.SECOND) * 1e6
        threshold_s = self._mix.get_property("alignment-threshold") / Gst.SECOND
        every = (
            f"a resync click every ~{threshold_s / (abs(ppm) * 1e-6) / 60:.0f} min"
            if abs(ppm) >= 0.5
            else "no resync clicks expected"
        )
        print(
            f"tracks clock drift: {drift_ns / 1e6:+.1f} ms over {elapsed_s / 60:.0f} min "
            f"({ppm:+.1f} ppm vs server) -> {every}",
            flush=True,
        )

    def make_master_limiter(self) -> list[Gst.Element]:
        """Headroom trim + look-ahead limiter, or the old soft clipper if it's not installed."""
        factory = next(
            (
                f
                for f in Gst.ElementFactory.list_get_elements(
                    Gst.ELEMENT_FACTORY_TYPE_ANY, Gst.Rank.NONE
                )
                if f.get_name().lower().startswith("ladspa-")
                and f.get_name().lower().endswith("fastlookaheadlimiter")
            ),
            None,
        )
        trim = self.make_element("volume", "limiter_trim")
        trim.set_property("volume", MASTER_HEADROOM)
        if factory is None:
            print("look-ahead limiter (swh-plugins) not found; using rglimiter", flush=True)
            # rglimiter soft-clips up to 0 dBFS; AAC overshoots a signal limited
            # that hard, so the trim after it leaves 6 dB for the encoder.
            return [self.make_element("rglimiter", "limiter"), trim]
        limiter = factory.create("limiter")
        # LADSPA properties are named after the port ("Input gain (dB)" -> input-gain).
        for prefix, value in (
            ("input-gain", 0.0),
            ("limit", LIMIT_DB),
            ("release", LIMIT_RELEASE_S),
        ):
            spec = next(
                (p for p in limiter.list_properties() if p.name.lower().startswith(prefix)),
                None,
            )
            if spec is None:
                raise RuntimeError(f"{factory.get_name()} has no '{prefix}' property")
            limiter.set_property(spec.name, value)
        print(f"master limiter: {factory.get_name()} at {LIMIT_DB} dBFS", flush=True)
        return [trim, limiter]

    def _flvmux_latency_ns(self) -> int:
        video_delay = int(self.state.get("gopro_video_delay_ms", 0))
        audio_delay = max(
            int(self.state.get("ableton_audio_delay_ms", 0)),
            int(self.state.get("tracks_audio_delay_ms", 0)),
        )
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
            "tracks_mix_pad": "tracks_audio_delay_ms",
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
        for elem_name in ("cam_vol", "abl_vol"):
            elem = self.elements.get(elem_name)
            if elem:
                elem.set_property("volume", self.source_volume(elem_name))
        for i in range(1, NUM_TRACKS + 1):
            elem = self.elements.get(f"track_vol_{i}")
            if elem:
                elem.set_property("volume", self.track_volume(i))
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

    def source_volume(self, elem_name: str) -> float:
        """Effective gain for the GoPro mic (cam_vol) or Ableton stereo (abl_vol)."""
        if elem_name == "cam_vol":
            if self.cam_using_fallback or self.state.get("gopro_audio_muted"):
                return 0.0
            return float(self.state["gopro_audio_level"])
        if self.state.get("ableton_audio_muted"):
            return 0.0
        return float(self.state["ableton_audio_level"])

    def live_audio_source(self) -> str:
        """Highest-priority audio source that is arriving: tracks > ableton > gopro mic."""
        if self._tracks_in_pipeline and not self._tracks_lost:
            return "tracks"
        if self._abl_in_pipeline and not self._abl_lost and self._abl_misses < 2:
            return "ableton"
        if self.cam_ready and not self.cam_using_fallback:
            return "gopro"
        return "none"

    def _update_audio_source(self) -> None:
        """On a change of live source, unmute it and mute the others. Call with the lock held.

        Mutes are only flipped on a change, so a manual unmute (e.g. the GoPro mic
        layered over the tracks) sticks until the next failover. The last source is
        persisted so a restart doesn't undo manual mutes either.
        """
        source = self.live_audio_source()
        if source == self.state.get("audio_source"):
            return
        print(f"audio source: {self.state.get('audio_source')} -> {source}", flush=True)
        self.state["audio_source"] = source
        if source != "none":
            self.state["ableton_audio_muted"] = source != "ableton"
            self.state["gopro_audio_muted"] = source != "gopro"
        self.save_state()
        self.apply_state_to_elements()

    def track_volume(self, n: int) -> float:
        """Effective gain for track n: its saved level unless muted or soloed out."""
        solo = int(self.state.get("solo_track", 0))
        if self.state.get(f"track_{n}_muted") or (solo and solo != n):
            return 0.0
        return float(self.state[f"track_{n}_level"])

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
            "tracks_configured": bool(TRACKS_PATH),
            "tracks_ready": self.tracks_ready,
            "cam_using_fallback": self.cam_using_fallback,
            "audio_source": self.state.get("audio_source", "none") if self.pipeline_running else "none",
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
            cam_vol.set_property("volume", self.source_volume("cam_vol"))
        label = "fallback video" if use_fallback else "camera"
        print(f"video source: {label}", flush=True)

    @staticmethod
    def drop_eos(_pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        """A lost input must not send EOS into the shared encoder/mixer and end the program."""
        event = info.get_event()
        if event is not None and event.type == Gst.EventType.EOS:
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.OK

    def align_fallback_clock(self) -> None:
        """The file's timestamps start at 0; shift them onto the live pipeline clock."""
        pad = self._fb_parse_src
        clock = self.pipeline.get_clock() if self.pipeline else None
        if pad is None or clock is None:
            return
        now = clock.get_time() - self.pipeline.get_base_time()
        pad.set_offset(now)

    def loop_fallback_video(self) -> bool:
        """Restart the file source instead of seeking (qtdemux seeks stall on fragmented MP4)."""
        filesrc = self.elements.get("fb_filesrc")
        demux = self.elements.get("fb_demux")
        if not (filesrc and demux):
            return False
        filesrc.set_state(Gst.State.NULL)
        demux.set_state(Gst.State.NULL)
        self.align_fallback_clock()
        filesrc.set_state(Gst.State.READY)
        demux.set_state(Gst.State.PLAYING)
        filesrc.set_state(Gst.State.PLAYING)
        return False

    def _check_tracks_feed(self, live: bool) -> None:
        """rtspsrc never reconnects, so rebuild just the tracks branch when the feed returns."""
        self.tracks_ready = live
        if not self._tracks_in_pipeline:
            if live:
                print("tracks feed appeared, attaching", flush=True)
                self._schedule(self._rebuild_tracks_branch)
            return
        if live:
            self._tracks_misses = 0
            if self._tracks_lost:
                print("tracks feed returned, reconnecting", flush=True)
                self._schedule(self._rebuild_tracks_branch)
        else:
            self._tracks_misses += 1
            if self._tracks_misses >= 2:
                self._tracks_lost = True

    def _check_ableton_feed(self, live: bool) -> None:
        """Track Ableton liveness; restart to pick it up if it's needed but not in the pipeline."""
        self.ableton_ready = live
        self._abl_misses = 0 if live else self._abl_misses + 1
        usable = self._abl_in_pipeline and not self._abl_lost
        tracks_up = self._tracks_in_pipeline and not self._tracks_lost
        if not live or usable or tracks_up:
            return
        if time.monotonic() - self._abl_last_restart < ABLETON_RESTART_COOLDOWN_S:
            return
        print("tracks down and ableton is back, restarting pipeline to attach it", flush=True)
        self._abl_last_restart = time.monotonic()
        self._restart_pipeline()

    def _schedule(self, fn) -> None:
        GLib.idle_add(lambda: (fn(), False)[1])

    def _in_tracks_branch(self, element: Gst.Element | None) -> bool:
        while element is not None:
            if element in self._tracks_elems:
                return True
            element = element.get_parent()
        return False

    def _teardown_tracks_branch(self) -> None:
        if not self.pipeline:
            return
        mix_pad = self.elements.pop("tracks_mix_pad", None)
        for elem in reversed(self._tracks_elems):
            elem.set_state(Gst.State.NULL)
            self.pipeline.remove(elem)
        self._tracks_elems = []
        if mix_pad is not None and self._mix is not None:
            self._mix.release_request_pad(mix_pad)
        for i in range(1, NUM_TRACKS + 1):
            self.elements.pop(f"track_vol_{i}", None)

    def _rebuild_tracks_branch(self) -> None:
        if not self.pipeline or self._mix is None or not self.pipeline_running:
            return
        self._teardown_tracks_branch()
        self.add_tracks_branch(self.pipeline, self._mix, self.elements)
        for elem in reversed(self._tracks_elems):
            elem.set_state(Gst.State.PLAYING)
        self._tracks_in_pipeline = True
        self._tracks_lost = False
        self._tracks_misses = 0
        self.apply_state_to_elements()

    def _restart_pipeline(self) -> None:
        if self.main_loop:
            GLib.idle_add(self.main_loop.quit)

    def _monitor_sources(self) -> None:
        last_health_log = time.monotonic()
        while not self._monitor_stop.wait(3):
            if not self.pipeline_running:
                continue
            if time.monotonic() - last_health_log >= HEALTH_LOG_INTERVAL_S:
                last_health_log = time.monotonic()
                self.log_audio_health()
            cam_live = self.probe_rtsp(CAM_PATH, timeout=3)
            if TRACKS_PATH:
                self._check_tracks_feed(self.probe_rtsp(TRACKS_PATH, timeout=3))
            self._check_ableton_feed(self.probe_rtsp(ABLETON_PATH, timeout=3))
            with self.lock:
                self.cam_ready = cam_live
                if not self.pipeline or not self.elements:
                    continue
                self._update_audio_source()
                if cam_live and self._cam_src_dead:
                    print("camera came back, restarting pipeline", flush=True)
                    self._cam_src_dead = False
                    self._restart_pipeline()
                elif cam_live and self.cam_using_fallback:
                    GLib.idle_add(self.select_video_source, False)
                elif not cam_live and not self.cam_using_fallback and self.fallback_available():
                    self._cam_src_dead = True
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

    @staticmethod
    def _is_abl_element(element: Gst.Element | None) -> bool:
        while element is not None:
            if element.get_name().startswith("abl_"):
                return True
            element = element.get_parent()
        return False

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

    def add_tracks_branch(
        self,
        pipeline: Gst.Pipeline,
        mix: Gst.Element,
        elements: dict[str, Gst.Element],
    ) -> None:
        """N-channel RTP audio feed (L16/L24/Opus) -> per-channel volume -> submix -> master mix."""
        src = self.make_element("rtspsrc", "tracks_src")
        src.set_property("location", self.rtsp_url(TRACKS_PATH))
        src.set_property("protocols", "tcp")
        # The uncompressed feed arrives >200 ms late on a loaded host, and
        # drop-on-latency then discards most of it (silent program). The
        # tracks are already delayed seconds for A/V sync, so buffer generously.
        # TCP never loses packets, only bunches them up after a stall; keep the
        # whole burst instead of dropping it (each drop is an audible click).
        src.set_property("latency", 2000)
        src.set_property("drop-on-latency", False)
        src.set_property("do-rtsp-keep-alive", True)
        self.watch_jitterbuffers(src, "tracks")
        decode = self.make_element("decodebin", "tracks_decode")
        convert = self.make_element("audioconvert", "tracks_convert")
        float_caps = self.make_element("capsfilter", "tracks_float")
        float_caps.set_property("caps", Gst.Caps.from_string("audio/x-raw,format=F32LE"))
        deint = self.make_element("deinterleave", "tracks_deint")
        submix = self.make_element("audiomixer", "tracks_submix")
        submix.set_property("start-time-selection", 1)
        sub_caps = self.make_element("capsfilter", "tracks_submix_caps")
        sub_caps.set_property("caps", Gst.Caps.from_string(MIX_CAPS))
        sub_queue = self.make_queue("tracks_queue")
        self._tracks_elems = [src, decode, convert, float_caps, deint, submix, sub_caps, sub_queue]
        for elem in self._tracks_elems:
            pipeline.add(elem)
        self.link_many(convert, float_caps, deint)
        self.link_many(submix, sub_caps, sub_queue)
        self._tracks_drift = None
        convert.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, self._measure_tracks_drift
        )
        mix_pad = mix.request_pad_simple("sink_%u")
        if not mix_pad:
            raise RuntimeError("failed to request audiomixer pad")
        sub_src = sub_queue.get_static_pad("src")

        sub_src.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self.drop_eos)
        sub_src.link(mix_pad)
        mix_pad.set_property("offset", ms_to_ns(int(self.state["tracks_audio_delay_ms"])))
        elements["tracks_mix_pad"] = mix_pad

        def on_src_pad_added(_src: Gst.Element, pad: Gst.Pad, _user_data: object) -> None:
            caps = pad.get_current_caps()
            if not caps:
                return
            if caps.get_structure(0).get_string("media") == "audio":
                pad.link(decode.get_static_pad("sink"))

        def on_decoded_pad_added(_dec: Gst.Element, pad: Gst.Pad, _user_data: object) -> None:
            caps = pad.get_current_caps() or pad.query_caps(None)
            if caps and caps.get_structure(0).get_name().startswith("audio/x-raw"):
                pad.link(convert.get_static_pad("sink"))

        def on_channel_pad_added(_deint: Gst.Element, pad: Gst.Pad, _user_data: object) -> None:
            idx = int(pad.get_name().split("_")[-1])
            if idx >= NUM_TRACKS:
                sink = self.make_element("fakesink", f"tracks_extra_sink_{idx}")
                pipeline.add(sink)
                sink.sync_state_with_parent()
                self._tracks_elems.append(sink)
                pad.link(sink.get_static_pad("sink"))
                return
            n = idx + 1
            queue = self.make_queue(f"tracks_ch{n}_queue")
            vol = self.make_element("volume", f"track_vol_{n}")
            conv = self.make_element("audioconvert", f"tracks_ch{n}_convert")
            caps = self.make_element("capsfilter", f"tracks_ch{n}_caps")
            caps.set_property("caps", Gst.Caps.from_string("audio/x-raw,channels=2"))
            chain = (queue, vol, conv, caps)
            for elem in chain:
                pipeline.add(elem)
                elem.sync_state_with_parent()
                self._tracks_elems.append(elem)
            self.link_many(*chain)
            pad.link(queue.get_static_pad("sink"))
            sub_pad = submix.request_pad_simple("sink_%u")
            caps.get_static_pad("src").link(sub_pad)
            vol.set_property("volume", self.track_volume(n))
            self.elements[f"track_vol_{n}"] = vol

        src.connect("pad-added", on_src_pad_added, None)
        decode.connect("pad-added", on_decoded_pad_added, None)
        deint.connect("pad-added", on_channel_pad_added, None)

    @staticmethod
    def make_element(factory: str, name: str) -> Gst.Element:
        elem = Gst.ElementFactory.make(factory, name)
        if not elem:
            raise RuntimeError(f"failed to create GStreamer element {factory}")
        return elem

    def build_pipeline(self) -> Gst.Pipeline:
        pipeline = Gst.Pipeline.new("stream-mix")
        elements: dict[str, Gst.Element] = {}

        cam_src = self.make_element("rtspsrc", "cam_src")
        cam_src.set_property("location", self.rtsp_url(CAM_PATH))
        cam_src.set_property("protocols", "tcp")
        # Kept short: this latency also delays the camera video. Late packets
        # are still played rather than dropped (TCP only delays, never loses).
        cam_src.set_property("latency", 200)
        cam_src.set_property("drop-on-latency", False)
        self.watch_jitterbuffers(cam_src, "cam")

        abl_src = None
        if self.ableton_ready:
            abl_src = self.make_element("rtspsrc", "abl_src")
            abl_src.set_property("location", self.rtsp_url(ABLETON_PATH))
            abl_src.set_property("protocols", "tcp")
            # Same as the tracks feed: audio only and delayed seconds for sync
            # anyway, so buffer generously and never drop.
            abl_src.set_property("latency", 2000)
            abl_src.set_property("drop-on-latency", False)
            abl_src.set_property("do-rtsp-keep-alive", True)
            self.watch_jitterbuffers(abl_src, "ableton")

        cam_video_queue = self.make_delay_queue("cam_video_queue")
        depay = self.make_element("rtph264depay", "cam_h264_depay")
        h264parse = self.make_element("h264parse", "h264parse")
        cam_sel_queue = self.make_queue("cam_sel_queue")
        vselector = self.make_element("input-selector", "vselector")

        fb_filesrc = self.make_element("filesrc", "fb_filesrc")
        fb_filesrc.set_property("location", str(FALLBACK_VIDEO))
        fb_demux = self.make_element("qtdemux", "fb_demux")
        fb_h264parse = self.make_element("h264parse", "fb_h264parse")
        fb_sync = self.make_element("identity", "fb_sync")
        fb_sync.set_property("sync", True)
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
        mix_caps = self.make_element("capsfilter", "mix_caps")
        mix_caps.set_property("caps", Gst.Caps.from_string(MIX_CAPS))

        # The summed program regularly exceeds full scale; limit it before AAC,
        # where hard digital clipping is heard as crackle.
        lim_convert = self.make_element("audioconvert", "lim_convert")
        limiter_chain = self.make_master_limiter()
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
                "fb_demux": fb_demux,
                "videoflip": videoflip,
                "venc": venc,
                "aenc": aenc,
                "h264parse_out": h264parse_out,
            }
        )

        base_elems = [
            cam_src,
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
            fb_sync,
            fb_video_queue,
            vtee,
            cam_audio_queue,
            cam_vol,
            abl_audio_queue,
            abl_vol,
            mix,
            mix_caps,
            lim_convert,
            *limiter_chain,
            aconvert,
            aresample,
            aenc,
            aacparse,
            atee,
        ]
        if abl_src:
            base_elems.append(abl_src)
        for elem in base_elems:
            pipeline.add(elem)

        self.link_many(depay, h264parse, cam_sel_queue)
        self.link_many(fb_filesrc, fb_demux)
        self.link_many(fb_h264parse, fb_sync, fb_video_queue)
        fb_parse_src = fb_h264parse.get_static_pad("src")
        self._fb_parse_src = fb_parse_src
        self._vselector_cam_pad = vselector.request_pad_simple("sink_%u")
        self._vselector_fb_pad = vselector.request_pad_simple("sink_%u")
        if not self._vselector_cam_pad or not self._vselector_fb_pad:
            raise RuntimeError("failed to request input-selector pads")
        cam_sel_src = cam_sel_queue.get_static_pad("src")
        cam_sel_src.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self.drop_eos)
        cam_sel_src.link(self._vselector_cam_pad)
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
        self.link_many(
            mix, mix_caps, lim_convert, *limiter_chain, aconvert, aresample, aenc, aacparse, atee
        )

        def on_fb_event(_pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
            event = info.get_event()
            if event is not None and event.type == Gst.EventType.EOS:
                # Loop here so the file's EOS never reaches (and ends) the program.
                GLib.timeout_add(100, self.loop_fallback_video)
                return Gst.PadProbeReturn.DROP
            return Gst.PadProbeReturn.OK

        def on_fb_pad_added(_demux: Gst.Element, pad: Gst.Pad, _user_data: object) -> None:
            caps = pad.get_current_caps()
            if not caps:
                return
            media = caps.get_structure(0).get_name()
            if media.startswith("video/"):
                pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, on_fb_event)
                pad.link(fb_h264parse.get_static_pad("sink"))
            elif media.startswith("audio/"):
                # Drop the visualizer's embedded audio; program audio is Ableton + cam mic.
                fb_audio_sink = pipeline.get_by_name("fb_audio_sink")
                if fb_audio_sink is None:
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
        if abl_src:
            abl_src.connect("pad-added", on_abl_pad_added, None)
        self._mix = mix
        self._abl_in_pipeline = abl_src is not None
        self._abl_lost = False
        self._abl_misses = 0
        self._tracks_in_pipeline = self.tracks_ready
        self._tracks_lost = False
        self._tracks_misses = 0
        self._cam_src_dead = False
        if self.tracks_ready:
            self.add_tracks_branch(pipeline, mix, elements)

        self.elements = elements
        self.apply_state_to_elements()
        return pipeline

    def on_bus_message(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        msg_type = message.type
        src = message.src
        if msg_type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"pipeline error: {err} ({debug})", flush=True)
            if self._in_tracks_branch(src):
                self._tracks_lost = True
                if self.pipeline:
                    self.pipeline.set_state(Gst.State.PLAYING)
                return
            if self._is_abl_element(src):
                # Fall back to the next audio source instead of stopping the program.
                self._abl_lost = True
                if self.pipeline:
                    self.pipeline.set_state(Gst.State.PLAYING)
                return
            if self._is_cam_element(src):
                self._cam_src_dead = True
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
        self.align_fallback_clock()
        self.select_video_source(self.cam_using_fallback)
        self.pipeline_running = True
        with self.lock:
            self._update_audio_source()
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
        self._tracks_elems = []
        self._jitterbuffers = {}
        self._tracks_drift = None
        self._mix = None
        self._vselector_cam_pad = None
        self._vselector_fb_pad = None
        self._fb_parse_src = None
        self.pipeline_running = False
        self.cam_using_fallback = False
        self._abl_in_pipeline = False

    def pipeline_thread(self) -> None:
        self._pipeline_thread_id = threading.get_ident()
        while not self._stop:
            print("waiting for audio (tracks, ableton or cam) + cam or fallback video…", flush=True)
            while not self._stop and not self.sources_ready():
                time.sleep(2)
            if self._stop:
                return
            audio = "+".join(
                    n
                    for n, up in (
                        ("tracks", self.tracks_ready),
                        ("ableton", self.ableton_ready),
                        ("gopro", self.cam_ready),
                    )
                    if up
                )
            video = "camera" if self.cam_ready else "fallback video"
            print(f"starting pipeline: audio={audio} video={video}", flush=True)
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
            if key.endswith("_enabled") or key.endswith("_muted"):
                updates[key] = bool(body[key])
            elif key == "video_rotation":
                rotation = int(body[key])
                updates[key] = rotation if rotation in VIDEO_ROTATION_METHODS else 0
            elif key == "solo_track":
                solo = int(body[key])
                updates[key] = solo if 1 <= solo <= NUM_TRACKS else 0
            elif key == "stream_video_bitrate_kbps":
                updates[key] = max(800, min(4500, int(body[key])))
            elif key == "stream_audio_bitrate_kbps":
                updates[key] = max(96, min(320, int(body[key])))
            elif key.endswith("_name"):
                updates[key] = str(body[key]).strip()[:24] or DEFAULT_STATE[key]
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
