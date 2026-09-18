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
COPY assets/citadelSkullVisualizer.mp4 /app/assets/citadelSkullVisualizer.mp4
RUN chmod 644 /app/assets/citadelSkullVisualizer.mp4

ENV PYTHONUNBUFFERED=1
CMD ["python3", "/app/mixer.py"]
