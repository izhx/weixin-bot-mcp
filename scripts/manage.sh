#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
case "${1:-status}" in
  init)
    umask 077
    mkdir -p data
    chmod 700 data
    ;;
  build)
    python3 scripts/fetch-tunnel.py
    docker compose build bridge
    ;;
  start) docker compose up -d bridge ;;
  stop) docker compose stop bridge ;;
  status)
    docker compose ps
    docker compose exec -T bridge weixin-mcp status
    ;;
  tunnel) docker compose exec bridge weixin-mcp setup-tunnel ;;
  password) docker compose exec bridge weixin-mcp setup-password ;;
  transport)
    shift
    docker compose exec -T bridge weixin-mcp transport "$@"
    ;;
  login)
    docker compose stop bridge
    docker compose run --rm bridge login
    printf '%s\n' 'Scan data/state/weixin-login-qr.png, then run: sh scripts/manage.sh login-poll'
    ;;
  login-poll) docker compose run --rm bridge login-poll ;;
  resume)
    docker compose stop bridge
    docker compose run --rm bridge resume --confirm-single-consumer
    docker compose up -d bridge
    ;;
  callback-host)
    shift
    docker compose exec -T bridge weixin-mcp set-callback-host "$@"
    ;;
  test)
    docker compose --profile test build test
    docker compose --profile test run --rm test
    ;;
  *) printf '%s\n' 'Usage: sh scripts/manage.sh {init|build|start|stop|status|tunnel|password|transport direct|transport tunnel|login|login-poll|resume|callback-host HOST|test}' >&2; exit 2 ;;
esac
