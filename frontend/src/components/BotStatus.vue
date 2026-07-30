<script setup>
import { computed } from "vue";
import { useBot } from "../composables/useBot.js";

const { state, connected } = useBot();

const pnlClass = computed(() =>
  state.pnl_usdt >= 0 ? "positive" : "negative",
);
const statusLabel = computed(() => {
  if (state.trading_halted) return { text: "DETENIDO", cls: "status-halted" };
  if (state.news_paused) return { text: "PAUSA NEWS", cls: "status-paused" };
  return { text: "ACTIVO", cls: "status-active" };
});

function fmt(n, decimals = 2) {
  if (n == null) return "—";
  return Number(n).toFixed(decimals);
}
function fmtSign(n) {
  if (n == null) return "—";
  return (n >= 0 ? "+" : "") + Number(n).toFixed(2);
}
</script>

<template>
  <div class="bot-status">
    <div class="status-header">
      <div class="bot-title">
        <span class="dot" :class="connected ? 'dot-green' : 'dot-red'" />
        <span>{{ connected ? "Conectado" : "Reconectando…" }}</span>
      </div>
      <span class="badge" :class="statusLabel.cls">{{ statusLabel.text }}</span>
    </div>

    <div v-if="state.mode" class="mode-line">
      {{ state.mode }} · {{ state.symbols?.join(", ") }}
    </div>

    <div class="cards">
      <div class="card">
        <div class="card-label">Balance</div>
        <div class="card-value">${{ fmt(state.balance_usdt) }}</div>
        <div class="card-sub">Pico: ${{ fmt(state.balance_peak) }}</div>
      </div>

      <div class="card">
        <div class="card-label">PnL Total</div>
        <div class="card-value" :class="pnlClass">
          {{ fmtSign(state.pnl_usdt) }} USDT
        </div>
        <div class="card-sub" :class="pnlClass">
          {{ fmtSign(state.pnl_pct) }}%
        </div>
      </div>

      <div class="card">
        <div class="card-label">Drawdown</div>
        <div
          class="card-value"
          :class="state.drawdown_pct > 10 ? 'negative' : ''"
        >
          {{ fmt(state.drawdown_pct) }}%
        </div>
        <div class="card-sub">Desde el pico</div>
      </div>

      <div class="card">
        <div class="card-label">Win Rate</div>
        <div class="card-value">{{ fmt(state.win_rate, 1) }}%</div>
        <div class="card-sub">
          {{ state.wins }}W / {{ state.losses }}L / {{ state.manual_closes }}M
        </div>
      </div>
    </div>

    <div v-if="state.news_paused" class="news-banner">
      ⚠️ News circuit breaker activo — nuevas entradas pausadas
    </div>
  </div>
</template>

<style scoped>
.bot-status {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.status-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.bot-title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.85rem;
  color: var(--text-muted);
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}
.dot-green {
  background: var(--green);
  box-shadow: 0 0 6px var(--green);
}
.dot-red {
  background: var(--red);
}

.badge {
  font-size: 0.75rem;
  font-weight: 700;
  letter-spacing: 0.05em;
  padding: 0.25rem 0.75rem;
  border-radius: 999px;
}
.status-active {
  background: rgba(34, 197, 94, 0.15);
  color: var(--green);
}
.status-halted {
  background: rgba(239, 68, 68, 0.15);
  color: var(--red);
}
.status-paused {
  background: rgba(234, 179, 8, 0.15);
  color: #eab308;
}

.mode-line {
  font-size: 0.8rem;
  color: var(--text-muted);
}

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 1rem;
}

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 0.75rem;
  padding: 1rem;
}

.card-label {
  font-size: 0.75rem;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 0.25rem;
}

.card-value {
  font-size: 1.4rem;
  font-weight: 700;
  color: var(--text);
}

.card-sub {
  font-size: 0.8rem;
  color: var(--text-muted);
  margin-top: 0.2rem;
}

.positive {
  color: var(--green) !important;
}
.negative {
  color: var(--red) !important;
}

.news-banner {
  background: rgba(234, 179, 8, 0.1);
  border: 1px solid rgba(234, 179, 8, 0.3);
  border-radius: 0.5rem;
  padding: 0.6rem 1rem;
  font-size: 0.85rem;
  color: #eab308;
}
</style>
