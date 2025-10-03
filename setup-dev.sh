#!/bin/bash

# Development setup script for redis-wrapper
# This script sets up the development environment with all necessary tools

set -e

echo "🚀 Setting up development environment for redis-wrapper..."

# Check if Go is installed
if ! command -v go &> /dev/null; then
    echo "❌ Go is not installed. Please install Go 1.21 or later."
    exit 1
fi

# Check Go version
GO_VERSION=$(go version | awk '{print $3}' | sed 's/go//')
REQUIRED_VERSION="1.21"

if [ "$(printf '%s\n' "$REQUIRED_VERSION" "$GO_VERSION" | sort -V | head -n1)" != "$REQUIRED_VERSION" ]; then
    echo "❌ Go version $GO_VERSION is too old. Please upgrade to Go $REQUIRED_VERSION or later."
    exit 1
fi

echo "✅ Go $GO_VERSION is installed"

# Install development tools
echo "📦 Installing development tools..."

# golangci-lint
if ! command -v golangci-lint &> /dev/null; then
    echo "Installing golangci-lint..."
    go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest
else
    echo "✅ golangci-lint is already installed"
fi

# goimports
if ! command -v goimports &> /dev/null; then
    echo "Installing goimports..."
    go install golang.org/x/tools/cmd/goimports@latest
else
    echo "✅ goimports is already installed"
fi

# gosec
if ! command -v gosec &> /dev/null; then
    echo "Installing gosec..."
    go install github.com/securecodewarrior/gosec/v2/cmd/gosec@latest
else
    echo "✅ gosec is already installed"
fi

# pre-commit (optional)
if command -v pip &> /dev/null || command -v pip3 &> /dev/null; then
    if ! command -v pre-commit &> /dev/null; then
        echo "Installing pre-commit..."
        pip install pre-commit || pip3 install pre-commit || true
    fi

    if command -v pre-commit &> /dev/null; then
        echo "Setting up pre-commit hooks..."
        pre-commit install
        echo "✅ pre-commit hooks installed"
    fi
else
    echo "⚠️  pip not found. Install pre-commit manually if desired: pip install pre-commit"
fi

# Download dependencies
echo "📥 Downloading Go dependencies..."
go mod download
go mod tidy

# Run initial checks
echo "🔍 Running initial code quality checks..."
echo "Running go fmt..."
go fmt ./...

echo "Running go vet..."
go vet ./...

echo "Running tests..."
go test -v ./...

echo "Running linter..."
golangci-lint run || echo "⚠️  Linting found issues. Run 'make lint-fix' to auto-fix some issues."

echo ""
echo "🎉 Development environment setup complete!"
echo ""
echo "Available commands:"
echo "  make help        - Show all available commands"
echo "  make test        - Run tests"
echo "  make lint        - Run linter"
echo "  make fmt         - Format code"
echo "  make ci          - Run all CI checks locally"
echo ""
echo "Happy coding! 🚀"