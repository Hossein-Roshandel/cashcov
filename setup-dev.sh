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

# pre-commit (optional)
make dev-pre-commit

# # Download dependencies
# echo "📥 Downloading Go dependencies..."
# go mod download
# go mod tidy

# # Run initial checks
# echo "🔍 Running initial code quality checks..."
# echo "Running go fmt..."
# go fmt ./...

# echo "Running go vet..."
# go vet ./...

# echo "Running tests..."
# go test -v ./...

# echo "Running linter..."
# golangci-lint run || echo "⚠️  Linting found issues. Run 'make lint-fix' to auto-fix some issues."

# echo ""
# echo "🎉 Development environment setup complete!"
# echo ""
# echo "Available commands:"
# echo "  make help        - Show all available commands"
# echo "  make test        - Run tests"
# echo "  make lint        - Run linter"
# echo "  make fmt         - Format code"
# echo "  make ci          - Run all CI checks locally"
# echo ""
# echo "Happy coding! 🚀"
