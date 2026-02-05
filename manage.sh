#!/bin/bash

# ==============================================================================
#
#         Unified Environment Management Script for the MESH Project
#
# ==============================================================================
#
# This script is the single entry point for managing all Docker environments.
# It ensures that the correct .env file and docker-compose files are used
# for the specified environment (dev or prod).
#
#
# Usage:
#   ./manage.sh up -e [dev|prod]   - Start the services for the specified environment.
#   ./manage.sh down -e [dev|prod] - Stop the services for the specified environment.
#   ./manage.sh logs -e [dev|prod] - View the logs for the specified environment.
#   ./manage.sh build -e [dev|prod] - Build or rebuild the services.
#
# Examples:
#   ./manage.sh up -e dev          - Starts the development stack.
#   ./manage.sh logs -e prod       - Tails the logs of the production stack.
#
# ==============================================================================

# --- Configuration ---
DEFAULT_ENV="dev"
PROG_NAME=$(basename "$0")

# --- Helper Functions ---
function display_help() {
    echo "Usage: $PROG_NAME -e [dev|prod] [COMMAND] [ARGS...]"
    echo
    echo "This script is the official entry point for managing the project's Docker environments."
    echo
    echo "Options:"
    echo "  -e      Specify the environment (dev or prod). Required."
    echo "  -h      Display this help message."
    echo
    echo "Commands:"
    echo "  up      Create and start containers (e.g., 'up -d' to detach)."
    echo "  down    Stop and remove containers, networks, and volumes."
    echo "  logs    Follow log output (e.g., 'logs -f backend')."
    echo "  build   Build or rebuild services."
    echo
    echo "Examples:"
    echo "  $PROG_NAME -e dev up -d"
    echo "  $PROG_NAME -e prod down"
    echo "  $PROG_NAME -e dev logs -f backend"
}

function error_exit() {
    echo "Error: $1" >&2
    echo "See '$PROG_NAME -h' for usage."
    exit 1
}

# --- Main Script Logic ---

# New argument parsing logic
ENV=""
while getopts ":e:h" opt; do
  case ${opt} in
    e)
      ENV=$OPTARG
      ;;
    h)
      display_help
      exit 0
      ;;
    \?)
      error_exit "Invalid option: -$OPTARG"
      ;;
    :)
      error_exit "Option -$OPTARG requires an argument."
      ;;
  esac
done
shift "$((OPTIND-1))" # Shift off the options and optional --.

# After `getopts`, the first argument is now the command (up, down, etc.)
COMMAND=$1
if [[ -z "$COMMAND" ]]; then
    error_exit "No command specified."
fi
shift # Remove command from arguments list

# --- Environment Setup ---
# Set environment to default if not provided
# Make environment selection mandatory
if [ -z "$ENV" ]; then
    error_exit "No environment specified. You must use '-e dev' or '-e prod'."
fi

# Validate environment
if [ "$ENV" != "dev" ] && [ "$ENV" != "prod" ]; then
    error_exit "Invalid environment '$ENV'. Please use 'dev' or 'prod'."
fi

# Define the environment file and compose files based on the chosen environment
ENV_FILE=".env.$ENV"
COMPOSE_FILES=(
    -f docker-compose.$ENV.yaml
)

# Check if the required .env file exists
if [ ! -f "$ENV_FILE" ]; then
    error_exit "$ENV_FILE not found. Please create it by copying from $ENV_FILE.example and filling in the values."
fi

# --- Execution ---
echo "=========================================================="
echo "          Running command '$COMMAND' in '$ENV' environment"
echo "=========================================================="
echo "Using env file: $ENV_FILE"
echo "Using compose files: ${COMPOSE_FILES[*]}"
echo "----------------------------------------------------------"

# Execute the docker-compose command with all specified files and arguments
docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" "$COMMAND" "$@"