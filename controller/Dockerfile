FROM debian:stable-slim

# ติดตั้งเครื่องมือที่ต้องใช้
RUN apt-get update && apt-get install -y --no-install-recommends \
      android-tools-adb android-tools-fastboot \
      usbutils udev iputils-ping curl procps ca-certificates python3 python3-pip \
      python3-pil python-is-python3 dos2unix \
    && pip3 install --no-cache-dir --break-system-packages pyyaml \
    && rm -rf /var/lib/apt/lists/*

# udev rule สำหรับ Samsung (Vendor ID: 04e8) ให้สิทธิ์อุปกรณ์ USB
RUN groupadd -g 1001 plugdev || true && \
    printf 'SUBSYSTEM=="usb", ATTR{idVendor}=="04e8", MODE="0666", GROUP="plugdev"\n' \
      > /etc/udev/rules.d/51-android.rules

ENV ADB_SERVER_PORT=5037
WORKDIR /work

# Utility scripts
COPY touch_event_capture.py /usr/local/bin/touch-event-capture.py
COPY capture_ui_and_screen.py /usr/local/bin/capture-ui-and-screen.py
COPY overlay_touches.py /usr/local/bin/overlay-touches.py
COPY replay_log.py /usr/local/bin/replay-log.py
COPY ui_dump_capture.py /usr/local/bin/ui-dump-capture.py
COPY flow_config.py /usr/local/bin/flow_config.py
COPY element_interactor.py /usr/local/bin/element_interactor.py
COPY automation_engine.py /usr/local/bin/automation-engine.py
COPY flow_recorder.py /usr/local/bin/flow-recorder.py
COPY debug_server.py /usr/local/bin/debug_server.py
COPY universal_format.py /usr/local/bin/universal_format.py
COPY universal_recorder.py /usr/local/bin/universal-recorder.py
COPY universal_player.py /usr/local/bin/universal-player.py
COPY flow_editor.py /usr/local/bin/flow-editor.py
COPY flow_editor_web.py /usr/local/bin/flow-editor-web.py
COPY sequence_runner.py /usr/local/bin/sequence_runner.py
COPY playback_logger.py /usr/local/bin/playback_logger.py

# Copy importable versions (underscore names) for modules that are also executed as scripts
COPY universal_player.py /usr/local/bin/universal_player.py

# แปลง line endings จาก Windows (CRLF) เป็น Unix (LF)
RUN dos2unix /usr/local/bin/*.py

RUN chmod +x /usr/local/bin/touch-event-capture.py \
    && chmod +x /usr/local/bin/capture-ui-and-screen.py \
    && chmod +x /usr/local/bin/overlay-touches.py \
    && chmod +x /usr/local/bin/replay-log.py \
    && chmod +x /usr/local/bin/ui-dump-capture.py \
    && chmod +x /usr/local/bin/automation-engine.py \
    && chmod +x /usr/local/bin/flow-recorder.py \
    && chmod +x /usr/local/bin/debug_server.py \
    && chmod +x /usr/local/bin/universal-recorder.py \
    && chmod +x /usr/local/bin/universal-player.py \
    && chmod +x /usr/local/bin/flow-editor.py \
    && chmod +x /usr/local/bin/flow-editor-web.py \
    && chmod +x /usr/local/bin/sequence_runner.py

# คงอยู่รอคำสั่งจาก docker-compose (ทั้งโหมด server และ client ใช้ภาพเดียวกัน)
CMD ["bash","-lc","echo image ready; tail -f /dev/null"]
