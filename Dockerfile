FROM docker.io/library/python:3.12-slim-bookworm

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN apt-get update && apt-get install -y curl unzip &&  \
    curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp &&  \
    chmod a+rx /usr/local/bin/yt-dlp &&  \
    curl -L https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip -o /tmp/deno.zip && \
    unzip /tmp/deno.zip -d /usr/local/bin && \
    chmod a+rx /usr/local/bin/deno && \
    rm /tmp/deno.zip && \
    apt install ffmpeg -y &&  \
    apt-get autoremove -y  &&  \
    apt-get clean  &&  \
    rm -rf /var/lib/apt/lists/* &&  \
    pip install -r requirements.txt &&  \
    groupadd -g 10000 nonroot && \
    useradd -u 10000 -g 10000 -s /bin/bash -m nonroot

# yt-dlp drives deno (its JS runtime) to solve YouTube's player JS challenge.
# deno needs a writable cache dir; /tmp is writable for the nonroot user.
ENV DENO_DIR=/tmp/deno-cache

COPY ./app/main.py /app/main.py

USER nonroot:nonroot
CMD ["python3", "/app/main.py"]
