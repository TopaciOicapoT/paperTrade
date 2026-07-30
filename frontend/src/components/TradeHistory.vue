<script setup>
import { ref, onMounted, computed } from "vue";
import { useBot } from "../composables/useBot.js";

const { history, historyTotal, fetchHistory } = useBot();

const page = ref(0);
const pageSize = 25;
const loading = ref(false);
const stats = ref(null);

const totalPages = computed(() => Math.ceil(historyTotal.value / pageSize));

async function load() {
  loading.value = true;
  try {
    await fetchHistory(pageSize, page.value * pageSize);
  } finally {
    loading.value = false;
  }
}

onMounted(async () => {
  await load();
  // cargar estadísticas
  try {
    const { fetchStats } = useBot();
    stats.value = await fetchStats();
  } catch {
    /* sin trades aún */
  }
});

async function prev() {
  if (page.value > 0) {
    page.value--;
    await load();
  }
}
async function next() {
  if (page.value < totalPages.value - 1) {
    page.value++;
    await load();
  }
}

function exitClass(type) {
  if (type === "TP") return "exit-tp";
  if (type === "SL") return "exit-sl";
  if (type === "MANUAL") return "exit-manual";
  return "";
}

function fmt(n, d = 2) {
  return n != null ? Number(n).toFixed(d) : "—";
}
function fmtSign(n) {
  return n != null ? (n >= 0 ? "+" : "") + Number(n).toFixed(2) : "—";
}
</script>

<template>
  <div class="trade-history">
    <div class="section-header">
      <h2>
        Historial de trades <span class="count">{{ historyTotal }}</span>
      </h2>
      <button class="btn btn-refresh" @click="load" :disabled="loading">
        {{ loading ? "…" : "↻ Actualizar" }}
      </button>
    </div>

    <!-- Resumen estadístico -->
    <div v-if="stats && stats.total_trades > 0" class="stats-row">
      <div class="stat">
        <span class="stat-label">WR</span> {{ fmt(stats.win_rate_pct, 1) }}%
      </div>
      <div class="stat">
        <span class="stat-label">PF</span> {{ stats.profit_factor ?? "—" }}
      </div>
      <div class="stat">
        <span class="stat-label">PnL total</span>
        <span :class="stats.total_pnl_usdt >= 0 ? 'positive' : 'negative'">
          {{ fmtSign(stats.total_pnl_usdt) }} USDT
        </span>
      </div>
      <div class="stat">
        <span class="stat-label">Mejor trade</span>
        <span class="positive">+{{ fmt(stats.best_trade_usdt) }}</span>
      </div>
      <div class="stat">
        <span class="stat-label">Peor trade</span>
        <span class="negative">{{ fmt(stats.worst_trade_usdt) }}</span>
      </div>
    </div>

    <div v-if="history.length === 0 && !loading" class="empty">
      Sin trades registrados aún.
    </div>

    <div v-else class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Símbolo</th>
            <th>Dir.</th>
            <th>Entrada</th>
            <th>Salida</th>
            <th>Tipo</th>
            <th>PnL USDT</th>
            <th>PnL %</th>
            <th>Balance</th>
            <th>IA prob.</th>
            <th>Hora</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in history" :key="t.entry_time + t.symbol">
            <td class="symbol">{{ t.symbol }}</td>
            <td>
              <span class="dir-badge" :class="t.direction">{{
                t.direction?.toUpperCase()
              }}</span>
            </td>
            <td>{{ fmt(t.entry_price, 4) }}</td>
            <td>{{ fmt(t.exit_price, 4) }}</td>
            <td>
              <span class="exit-badge" :class="exitClass(t.exit_type)">{{
                t.exit_type
              }}</span>
            </td>
            <td :class="t.pnl_usdt >= 0 ? 'positive' : 'negative'">
              {{ fmtSign(t.pnl_usdt) }}
            </td>
            <td :class="t.pnl_pct >= 0 ? 'positive' : 'negative'">
              {{ fmtSign(t.pnl_pct) }}%
            </td>
            <td>${{ fmt(t.balance_after) }}</td>
            <td>
              {{
                t.ai_probability != null
                  ? fmt(t.ai_probability * 100, 1) + "%"
                  : "—"
              }}
            </td>
            <td class="time">{{ t.entry_time?.slice(11, 16) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Paginación -->
    <div v-if="totalPages > 1" class="pagination">
      <button class="btn" @click="prev" :disabled="page === 0">
        ← Anterior
      </button>
      <span>{{ page + 1 }} / {{ totalPages }}</span>
      <button class="btn" @click="next" :disabled="page >= totalPages - 1">
        Siguiente →
      </button>
    </div>
  </div>
</template>

<style scoped>
.trade-history {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

h2 {
  font-size: 1rem;
  font-weight: 600;
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.count {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 0.1rem 0.5rem;
  font-size: 0.8rem;
  color: var(--text-muted);
}

.btn {
  padding: 0.35rem 0.9rem;
  border-radius: 0.5rem;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  cursor: pointer;
  font-size: 0.82rem;
}
.btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.btn-refresh {
  color: var(--text-muted);
}

.stats-row {
  display: flex;
  flex-wrap: wrap;
  gap: 1rem;
  padding: 0.75rem 1rem;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 0.75rem;
  font-size: 0.85rem;
}

.stat {
  display: flex;
  gap: 0.4rem;
  align-items: baseline;
}
.stat-label {
  color: var(--text-muted);
  font-size: 0.75rem;
}

.empty {
  color: var(--text-muted);
  font-size: 0.9rem;
  text-align: center;
  padding: 2rem 0;
}

.table-wrap {
  overflow-x: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.83rem;
}

th {
  text-align: left;
  color: var(--text-muted);
  font-weight: 500;
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 0.5rem 0.6rem;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}

td {
  padding: 0.55rem 0.6rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  color: var(--text);
  white-space: nowrap;
}

.symbol {
  font-weight: 600;
}

.dir-badge {
  padding: 0.1rem 0.45rem;
  border-radius: 0.25rem;
  font-size: 0.7rem;
  font-weight: 700;
}
.dir-badge.long {
  background: rgba(34, 197, 94, 0.15);
  color: var(--green);
}
.dir-badge.short {
  background: rgba(239, 68, 68, 0.15);
  color: var(--red);
}

.exit-badge {
  padding: 0.1rem 0.45rem;
  border-radius: 0.25rem;
  font-size: 0.7rem;
  font-weight: 700;
}
.exit-tp {
  background: rgba(34, 197, 94, 0.15);
  color: var(--green);
}
.exit-sl {
  background: rgba(239, 68, 68, 0.15);
  color: var(--red);
}
.exit-manual {
  background: rgba(148, 163, 184, 0.15);
  color: #94a3b8;
}

.positive {
  color: var(--green);
}
.negative {
  color: var(--red);
}
.time {
  color: var(--text-muted);
  font-size: 0.78rem;
}

.pagination {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 1rem;
  font-size: 0.85rem;
  color: var(--text-muted);
  padding-top: 0.5rem;
}
</style>
