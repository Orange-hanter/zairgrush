#!/bin/bash
# E13-trial stand factory: worktree на red-state + swarm.toml плеча + задача.
# Использование: mkstand.sh <task> <source-stand-repo> <red-sha> <arm a|b> <family wordstat|zeus>
set -euo pipefail
TASK=$1; SRC=$2; SHA=$3; ARM=$4; FAMILY=$5
MAIN=/Users/dakh/Git/_my/ZAIrgRush
DST=$MAIN/experiments/stand-e13t-$TASK-$ARM

git -C "$SRC" worktree add "$DST" -b "e13t-$TASK-$ARM" "$SHA"

if [ "$FAMILY" = wordstat ]; then
cat > "$DST/swarm.toml" <<'EOF'
# E13-trial (WAV-004): валидация датасета расходящихся плеч
# (experiments/goldset/e13). Плечи отличаются РОВНО строками executor_*;
# всё остальное одинаково побайтово — один фактор на прогон.
#
# Ревьюер — КОНСТАНТА замера opus/xhigh, а НЕ sonnet/medium как в E13:
# §21 и E15 показали, что дешёвый судья даёт 0 находок на этом классе
# диффов — с ним прогон мерял бы судью, а не исполнителей.
gate_command = ["python3", "-m", "unittest", "discover", "-s", "tests", "-t", "."]
protected_paths = ["tests/*", "tests/**"]

review_model = "claude-opus-5"
review_effort = "xhigh"
review_budget_usd = 1.5

confirmations = 1
max_iterations = 3
verification = "never"
total_budget_usd = 4.0
map_budget = 25
silence_timeout = 600
wall_clock_cap = 1800
live_board = false
board_open = false
EOF
else
cat > "$DST/swarm.toml" <<'EOF'
# E13-trial (WAV-004): валидация датасета расходящихся плеч
# (experiments/goldset/e13). Плечи отличаются РОВНО строками executor_*;
# всё остальное одинаково побайтово — один фактор на прогон.
#
# Ревьюер — КОНСТАНТА замера opus/xhigh, а НЕ sonnet/medium как в E13:
# §21 и E15 показали, что дешёвый судья даёт 0 находок на этом классе
# диффов — с ним прогон мерял бы судью, а не исполнителей.
#
# Память ВЫКЛЮЧЕНА на обоих плечах (никаких [experiments] memory):
# инъекция была бы вторым фактором.
gate_command = ["zeus/scripts/check.sh"]
protected_paths = ["zeus/tests/**", "**/tests/**", "*.toml", "zeus/Cargo.lock"]

review_model = "claude-opus-5"
review_effort = "xhigh"
review_budget_usd = 3.0

confirmations = 1
max_iterations = 3
verification = "never"
map_budget = 25
gate_timeout = 1800
silence_timeout = 900
wall_clock_cap = 5400
live_board = false
board_open = false
EOF
fi

if [ "$ARM" = a ]; then
cat >> "$DST/swarm.toml" <<'EOF'

# ПЛЕЧО A — kimi. Модель пришпилена явно: непришпиленное плечо наследует
# машинный default_model и перестаёт быть плечом (урок E13).
executor_engine = "kimi"
executor_model = "kimi-code/k3-256k"
EOF
else
cat >> "$DST/swarm.toml" <<'EOF'

# ПЛЕЧО B — claude. Потолок вызова задан явно: --max-budget-usd уходит в
# argv исполнителя (kill-switch), цена плеча измерима.
executor_engine = "claude"
executor_model = "claude-sonnet-5"
EOF
if [ "$FAMILY" = wordstat ]; then
cat >> "$DST/swarm.toml" <<'EOF'
executor_budget_usd = 1.5
EOF
else
cat >> "$DST/swarm.toml" <<'EOF'
executor_budget_usd = 4.0
EOF
fi
fi

# Потолок прогона для zeus-плеч различен по метрости: плечо A платит
# только ревью (исполнитель квотой), плечо B — ревью + исполнителя.
if [ "$FAMILY" = zeus ]; then
if [ "$ARM" = a ]; then
cat >> "$DST/swarm.toml" <<'EOF'
total_budget_usd = 8.0
EOF
else
cat >> "$DST/swarm.toml" <<'EOF'
total_budget_usd = 12.0
EOF
fi
fi

git -C "$DST" add swarm.toml
git -C "$DST" commit -q -m "E13-trial: конфиг плеча $ARM ($TASK, ревьюер opus/xhigh константой)"
echo "stand ready: $DST"
