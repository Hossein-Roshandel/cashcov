# Development Dockerfile for Redis Wrapper
FROM golang:1.25.1-bookworm


# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    wget \
    make \
    build-essential \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python and pip for pre-commit
RUN apt-get update && apt-get install -y python3-pip && rm -rf /var/lib/apt/lists/*

# Create vscode user for dev container
RUN groupadd --gid 1000 vscode \
    && useradd --uid 1000 --gid vscode --shell /bin/bash --create-home vscode \
    && apt-get update \
    && apt-get install -y sudo \
    && echo vscode ALL=\(root\) NOPASSWD:ALL > /etc/sudoers.d/vscode \
    && chmod 0440 /etc/sudoers.d/vscode \
    && rm -rf /var/lib/apt/lists/*

USER vscode

# Set up workspace
WORKDIR /workspace

# Copy go mod files first for better caching
COPY go.mod go.sum ./
RUN go mod download

# Copy source code
COPY . .

# Install development tools using Makefile
RUN make dev-setup

# Default command
CMD ["bash"]