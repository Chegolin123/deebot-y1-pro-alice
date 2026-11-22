FROM python:3.14-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl build-essential pkg-config ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV CARGO_HOME=/root/.cargo \
    RUSTUP_HOME=/root/.rustup \
    PATH=/root/.cargo/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

RUN curl -sSf -o /tmp/rustup-init \
      https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init \
 && chmod +x /tmp/rustup-init \
 && /tmp/rustup-init -y --profile minimal --default-toolchain stable --no-modify-path \
 && rm -f /tmp/rustup-init

RUN pip install --no-cache-dir \
    "git+https://github.com/Lalit-Patil-07/client.py@feat/y1-pro-device-support"

# Class alias: the Y1 Pro exists under several device classes sharing the same
# UILogicId (y30_ww_h_y30h5). The community profile covers cqyi87; our unit
# reports kduunv, so reuse the same profile under that name.
RUN python -c "import deebot_client, os; p = os.path.join(os.path.dirname(deebot_client.__file__), 'hardware', 'kduunv.py'); open(p, 'w', encoding='utf-8').write('from .cqyi87 import get_device_info\n')"

WORKDIR /app
COPY probe.py /app/probe.py

CMD ["python", "probe.py"]
