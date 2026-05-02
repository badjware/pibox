FROM ubuntu:resolute

RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y nodejs npm fd-find ripgrep \
    && npm install -g @mariozechner/pi-coding-agent \
    && userdel -r ubuntu \
    && rm -rf /var/lib/apt/lists/*

# drop root privileges
USER 1000

RUN mkdir -p /home/ubuntu/work /home/ubuntu/.pi

WORKDIR /home/ubuntu/work

VOLUME ["/home/ubuntu/work", "/home/ubuntu/.pi"]

ENTRYPOINT ["pi"]