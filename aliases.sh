#!/usr/bin/env bash
# Trading bot log aliases — source this from ~/.bashrc:
#   echo "source /volume1/docker/fuzzy-tribble/aliases.sh" >> ~/.bashrc

_PROJ="/volume1/docker/fuzzy-tribble"

# ── Active services ──────────────────────────────────────────────────────────
alias logs-iv="docker logs -f --tail 200 iv-collector"
alias logs-discount="docker logs -f --tail 200 discount-strategy"
alias logs-bb="docker logs -f --tail 200 break-bounce-strategy"
alias logs-sonar="docker logs -f --tail 200 sonar-scanner"
alias logs-composite="docker logs -f --tail 200 composite-scanner"
alias logs-oi="docker logs -f --tail 200 oi-buildup-scanner"
alias logs-gap="docker logs -f --tail 200 gap-scanner"
alias logs-iv-rank="docker logs -f --tail 200 iv-rank-scanner"
alias logs-delivery="docker logs -f --tail 200 delivery-surge-scanner"
alias logs-smart="docker logs -f --tail 200 smart-money-scanner"

# ── Profile-only services (start with --profile flag) ────────────────────────
alias logs-momentum="docker logs -f --tail 200 momentum-strategy"
alias logs-div="docker logs -f --tail 200 directional-iv-strategy"

# ── All services at once (last 50 lines each, then follow) ───────────────────
alias logs-all="docker compose -f $_PROJ/docker-compose.prod.yml logs -f --tail 50"

# ── Convenience: restart a single service after git pull + rebuild ────────────
alias rebuild-iv="docker compose -f $_PROJ/docker-compose.prod.yml up -d --build iv-collector"
alias rebuild-discount="docker compose -f $_PROJ/docker-compose.prod.yml up -d --build discount"
alias rebuild-bb="docker compose -f $_PROJ/docker-compose.prod.yml up -d --build break-bounce"
alias rebuild-sonar="docker compose -f $_PROJ/docker-compose.prod.yml up -d --build sonar"
alias rebuild-all="docker compose -f $_PROJ/docker-compose.prod.yml up -d --build"

# ── Status at a glance ────────────────────────────────────────────────────────
alias ps-trade="docker compose -f $_PROJ/docker-compose.prod.yml ps"
