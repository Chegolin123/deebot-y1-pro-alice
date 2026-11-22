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

RUN pip install --no-cache-dir fastapi "uvicorn[standard]"

RUN pip install --no-cache-dir \
    "git+https://github.com/Lalit-Patil-07/client.py@feat/y1-pro-device-support"

RUN pip install --no-cache-dir python-multipart

# The Y1 Pro is reported as class kduunv, which has no profile upstream.
# It shares UILogicId y30_ww_h_y30h5 with cqyi87, so reuse that profile.
RUN python -c "import deebot_client, os; p = os.path.join(os.path.dirname(deebot_client.__file__), 'hardware', 'kduunv.py'); open(p, 'w', encoding='utf-8').write('from .cqyi87 import get_device_info\n')"

WORKDIR /app
COPY app.py /app/app.py

ENV DATA_DIR=/data
RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8099

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8099"]
