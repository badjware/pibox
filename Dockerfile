FROM ubuntu:resolute

RUN sed -i 's:^path-exclude=/usr/share/man:#path-exclude=/usr/share/man:' /etc/dpkg/dpkg.cfg.d/excludes \
    && apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends tini python-is-python3 nodejs npm golang fd-find ripgrep jq yq bc zip unzip git vim \
        docker.io docker-compose-v2 uidmap fuse-overlayfs rootlesskit slirp4netns iproute2 \
        man-db manpages \
    && apt-get remove -y sudo openssh-client curl wget \
    && apt-get autoremove -y \
    && rm /usr/bin/man && dpkg-divert --rename --remove /usr/bin/man \
    && ln -s $(which fdfind) /usr/local/bin/fd \
    && userdel -r ubuntu \
    && rm -rf /var/lib/apt/lists/*

ARG PI_CODING_AGENT_VERSION=0.73.1
RUN npm install -g @mariozechner/pi-coding-agent@${PI_CODING_AGENT_VERSION}

ENV EDITOR=vim

COPY content/image_AGENTS.md /AGENTS.md
COPY content/entrypoint.sh /entrypoint.sh

WORKDIR /

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]