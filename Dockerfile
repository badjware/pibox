FROM ubuntu:resolute

ARG PI_CODING_AGENT_VERSION=0.73.1

RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y python-is-python3 nodejs npm golang fd-find ripgrep jq yq bc zip unzip git vim \
    && apt-get remove -y sudo openssh-client curl wget \
    && ln -s $(which fdfind) /usr/local/bin/fd \
    && npm install -g @mariozechner/pi-coding-agent@${PI_CODING_AGENT_VERSION} \
    && userdel -r ubuntu \
    && rm -rf /var/lib/apt/lists/*

ENV EDITOR=vim

COPY content/image_AGENTS.md /AGENTS.md
COPY content/entrypoint.sh /entrypoint.sh

WORKDIR /

ENTRYPOINT ["/entrypoint.sh"]