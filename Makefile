.PHONY: help build test test-race test-cover lint lint-fix fmt vet clean deps tidy security-check benchmark profile help

# Default target
help: ## Show this help message
	@echo 'Usage: make [target]'
	@echo ''
	@echo 'Targets:'
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-15s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# Build the project
build: ## Build the project
	go build ./...

# Run tests
test: ## Run tests
	go test -v ./...

# Run tests with race detector
test-race: ## Run tests with race detector
	go test -v -race ./...

# Run tests with coverage
test-cover: ## Run tests with coverage report
	go test -v -coverprofile=coverage.out ./...
	go tool cover -html=coverage.out -o coverage.html
	@echo "Coverage report generated: coverage.html"

# Run linter
lint: ## Run golangci-lint
	golangci-lint run

# Run linter and fix issues
lint-fix: ## Run golangci-lint and fix issues
	golangci-lint run --fix

# Format code
fmt: ## Format Go code
	go fmt ./...
	goimports -w .

# Run go vet
vet: ## Run go vet
	go vet ./...

# Clean build artifacts
clean: ## Clean build artifacts
	go clean ./...
	rm -f coverage.out coverage.html

# Download dependencies
deps: ## Download dependencies
	go mod download

# Tidy dependencies
tidy: ## Tidy go.mod and go.sum
	go mod tidy

# Security check
security-check: ## Run security checks
	gosec ./...

# Run benchmarks
benchmark: ## Run benchmarks
	go test -bench=. -benchmem ./...

# Profile CPU
profile: ## Run CPU profiling
	go test -cpuprofile=cpu.prof -bench=. ./...
	go tool pprof cpu.prof

# Development setup
dev-setup: ## Set up development environment
	go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest
	go install golang.org/x/tools/cmd/goimports@latest
	go install github.com/securego/gosec/v2/cmd/gosec@latest
	go install golang.org/x/tools/gopls@latest
	@echo "Development tools installed. You may also want to install pre-commit:"
	@echo "pip install pre-commit"
	@echo "pre-commit install"

# CI simulation
ci: ## Run all CI checks locally
	$(MAKE) tidy
	$(MAKE) fmt
	$(MAKE) vet
	$(MAKE) lint
	$(MAKE) test-race
	$(MAKE) test-cover
	$(MAKE) security-check
	$(MAKE) build