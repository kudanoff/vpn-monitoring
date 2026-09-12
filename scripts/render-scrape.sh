#!/usr/bin/env bash
# Собирает hub/vmagent/scrape.yml из шаблонов по списку проектов в .env.
#
# Четыре проекта по пять заданий — это двадцать почти одинаковых блоков.
# Руками они неизбежно разъедутся, поэтому собираются из одного шаблона.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TPL="$ROOT/hub/vmagent/tpl"
OUT="$ROOT/hub/vmagent/scrape.yml"

PROJECTS=$(grep -E '^PROJECTS=' "$ROOT/hub/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"')
[[ -n "$PROJECTS" ]] || { echo "задайте PROJECTS в hub/.env, например: PROJECTS=\"main most\""; exit 1; }

# vmagent падает, если file_sd указывает на несуществующий файл.
# Создаём пустые заготовки для новых проектов, чтобы старт не ломался.
for p in $PROJECTS; do
  for f in "nodes-${p}.yml" "agents-${p}.yml"; do
    [[ -f "$ROOT/targets/$f" ]] || printf '# Заготовка для проекта %s\n[]\n' "$p" > "$ROOT/targets/$f"
  done
done

{
  cat "$TPL/head.yml"
  for p in $PROJECTS; do
    pu=$(echo "$p" | tr '[:lower:]-' '[:upper:]_')
    sed -e "s/__PU__/${pu}/g" -e "s/__P__/${p}/g" "$TPL/project.yml"
  done
  cat "$TPL/tail.yml"
} > "$OUT.tmp"

mv "$OUT.tmp" "$OUT"
echo "Собран scrape.yml для проектов: $PROJECTS"
