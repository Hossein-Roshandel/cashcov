#!/bin/bash

# Docker Development Environment Setup Script
# This script helps set up and manage the Docker development environment

set -e

PROJECT_NAME="redis-wrapper"
COMPOSE_FILE=".devcontainer/docker-compose.yml"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_docker() {
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed. Please install Docker first."
        log_info "Visit: https://docs.docker.com/get-docker/"
        exit 1
    fi

    if ! command -v docker compose &> /dev/null && ! command -v docker-compose &> /dev/null; then
        log_error "Docker Compose is not available. Please install Docker Compose."
        exit 1
    fi
}

# Determine docker compose command
get_compose_cmd() {
    if command -v docker compose &> /dev/null; then
        echo "docker compose"
    elif command -v docker-compose &> /dev/null; then
        echo "docker-compose"
    else
        log_error "Docker Compose not found"
        exit 1
    fi
}

show_help() {
    echo "Docker Development Environment Manager for $PROJECT_NAME"
    echo ""
    echo "Usage: $0 [COMMAND]"
    echo ""
    echo "Commands:"
    echo "  up          Start the development environment"
    echo "  down        Stop the development environment"
    echo "  build       Build the Docker images"
    echo "  rebuild     Rebuild the Docker images from scratch"
    echo "  shell       Open a shell in the development container"
    echo "  test        Run tests in the container"
    echo "  lint        Run linting in the container"
    echo "  logs        Show container logs"
    echo "  clean       Remove containers and volumes"
    echo "  help        Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 up          # Start development environment"
    echo "  $0 shell       # Open shell in container"
    echo "  $0 test        # Run tests"
}

main() {
    local cmd="$1"
    local compose_cmd

    case "$cmd" in
        "up")
            check_docker
            compose_cmd=$(get_compose_cmd)
            log_info "Starting development environment..."
            $compose_cmd up -d
            log_success "Development environment started!"
            log_info "Run '$0 shell' to open a shell in the container"
            ;;

        "down")
            check_docker
            compose_cmd=$(get_compose_cmd)
            log_info "Stopping development environment..."
            $compose_cmd down
            log_success "Development environment stopped!"
            ;;

        "build")
            check_docker
            compose_cmd=$(get_compose_cmd)
            log_info "Building Docker images..."
            $compose_cmd build
            log_success "Docker images built!"
            ;;

        "rebuild")
            check_docker
            compose_cmd=$(get_compose_cmd)
            log_info "Rebuilding Docker images from scratch..."
            $compose_cmd build --no-cache
            log_success "Docker images rebuilt!"
            ;;

        "shell")
            check_docker
            compose_cmd=$(get_compose_cmd)
            log_info "Opening shell in development container..."
            $compose_cmd exec dev bash
            ;;

        "test")
            check_docker
            compose_cmd=$(get_compose_cmd)
            log_info "Running tests in container..."
            $compose_cmd exec dev go test -v ./...
            ;;

        "lint")
            check_docker
            compose_cmd=$(get_compose_cmd)
            log_info "Running linting in container..."
            $compose_cmd exec dev make lint
            ;;

        "logs")
            check_docker
            compose_cmd=$(get_compose_cmd)
            log_info "Showing container logs..."
            $compose_cmd logs -f
            ;;

        "clean")
            check_docker
            compose_cmd=$(get_compose_cmd)
            log_warning "This will remove all containers and volumes. Continue? (y/N)"
            read -r response
            if [[ "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
                log_info "Cleaning up Docker resources..."
                $compose_cmd down -v --remove-orphans
                $compose_cmd rm -f
                log_success "Cleanup completed!"
            else
                log_info "Cleanup cancelled."
            fi
            ;;

        "help"|*)
            show_help
            ;;
    esac
}

# Run main function with all arguments
main "$@"
