# Contributing to redis-wrapper

Thank you for your interest in contributing to redis-wrapper! This document provides guidelines and information for contributors.

## Development Setup

### Prerequisites

- Go 1.21 or later
- Git

### Quick Setup

Run the development setup script:

```bash
./setup-dev.sh
```

This will install all necessary tools and set up your development environment.

### Manual Setup

If you prefer to set up manually:

```bash
# Install required tools
go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest
go install golang.org/x/tools/cmd/goimports@latest
go install github.com/securecodewarrior/gosec/v2/cmd/gosec@latest

# Install pre-commit hooks (optional)
pip install pre-commit
pre-commit install

# Download dependencies
go mod download
```

## Development Workflow

### Code Quality

We use several tools to maintain code quality:

- **golangci-lint**: Comprehensive linting
- **gosec**: Security vulnerability scanning
- **go vet**: Static analysis
- **go fmt**: Code formatting
- **goimports**: Import organization

### Running Checks

Use the provided Makefile for common tasks:

```bash
make help          # Show all available commands
make test          # Run tests
make lint          # Run linter
make fmt           # Format code
make ci            # Run all CI checks locally
make security-check # Run security scan
```

### Pre-commit Hooks

If you have pre-commit installed, it will automatically run quality checks before commits. You can also run it manually:

```bash
pre-commit run --all-files
```

### Testing

- Write tests for new features
- Ensure all tests pass: `make test`
- Run tests with race detection: `make test-race`
- Check test coverage: `make test-cover`

### Code Style

- Follow standard Go conventions
- Use `go fmt` and `goimports` for formatting
- Keep functions focused and well-documented
- Use meaningful variable and function names

## Pull Request Process

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature-name`
3. Make your changes
4. Run all quality checks: `make ci`
5. Commit your changes: `git commit -m "Add your feature"`
6. Push to your fork: `git push origin feature/your-feature-name`
7. Create a Pull Request

### PR Requirements

- All CI checks must pass
- Code is reviewed by maintainers
- Tests are included for new features
- Documentation is updated if needed

## Commit Messages

Use clear, descriptive commit messages:

```
feat: add new miss policy for stale-while-revalidate
fix: resolve race condition in background refresh
docs: update README with new configuration options
```

## Reporting Issues

- Use GitHub Issues for bug reports and feature requests
- Provide clear reproduction steps for bugs
- Include Go version and OS information
- Attach relevant code snippets or test cases

## Code of Conduct

Please be respectful and constructive in all interactions. We follow a code of conduct to ensure a positive community environment.

## License

By contributing to this project, you agree that your contributions will be licensed under the same license as the project.