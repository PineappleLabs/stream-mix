FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-gi \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-rtsp \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY mixer.py /app/mixer.py
COPY static/ /app/static/
COPY assets/citadelSkullVisualizer.mp4 /tmp/citadelSkullVisualizer.mp4
# The source MP4 is oddly fragmented and qtdemux can't restart it to loop; a plain
# faststart remux (video only, no re-encode) loops cleanly.
RUN mkdir -p /app/assets && ffmpeg -v error -y -i /tmp/citadelSkullVisualizer.mp4 -map 0:v -c copy -movflags +faststart /app/assets/fallback.mp4 \
    && chmod 644 /app/assets/fallback.mp4 && rm /tmp/citadelSkullVisualizer.mp4

ENV PYTHONUNBUFFERED=1
CMD ["python3", "/app/mixer.py"]
