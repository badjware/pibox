FROM ubuntu:resolute

RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y nodejs npm fd-find ripgrep \
    && npm install -g @mariozechner/pi-coding-agent \
    && userdel -r ubuntu \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh

WORKDIR /

ENTRYPOINT ["/entrypoint.sh"]