<script setup>
import { ref, computed } from "vue";
import { useBot } from "../composables/useBot.js";

const { state, closePosition, emergencyStop, resumeTrading } = useBot();

const positions = computed(() => Object.values(state.open_orders));
const closing = ref({}); // { symbol: true } mientras se procesa el cierre
const emergencyLoading = ref(false);
const feedback = ref(null);

function pnlEstimate(pos) {
  // No tenemos precio actual en tiempo real aquí; mostramos distancia al SL/TP
  return pos;
}

async function handleClose(symbol) {
  if (closing.value[symbol]) return;
  closing.value[symbol] = true;
  feedback.value = null;
  try {
    const result = await closePosition(symbol);
    feedback.value = {
      type: "ok",
      msg: `${symbol} cerrado | PnL: ${result.pnl_usdt >= 0 ? "+" : ""}${result.pnl_usdt.toFixed(2)} USDT`,
    };
  } catch (e) {
    feedback.value = {
      type: "err",
      msg: `Error cerrando ${symbol}: ${e.message}`,
    };
  } finally {
    delete closing.value[symbol];
  }
}

async function handleEmergency() {
  if (!confirm("⚠️  ¿Cerrar TODAS las posiciones y detener el bot?")) return;
  emergencyLoading.value = true;
  feedback.value = null;
  try {
    const res = await emergencyStop();
    feedback.value = { type: "ok", msg: res.message };
  } catch (e) {
    feedback.value = { type: "err", msg: e.message };
  } finally {
    emergencyLoading.value = false;
  }
}

async function handleResume() {
  try {
    await resumeTrading();
    feedback.value = { type: "ok", msg: "Trading reanudado." };
  } catch (e) {
    feedback.value = { type: "err", msg: e.message };
  }
}

function fmt(n, d = 4) {
  return n != null ? Number(n).toFixed(d) : "—";
}
</script>

<template>
  <div class="open-positions">
    <div class="section-header">
      <h2>
        Posiciones abiertas <span class="count">{{ positions.length }}</span>
      </h2>

      <div class="actions">
        <button
          v-if="state.trading_halted"
          class="btn btn-resume"
          @click="handleResume"
        >
          Reanudar trading
        </button>

        <button
          class="btn btn-emergency"
          :disabled="emergencyLoading"
          @click="handleEmergency"
        >
          {{ emergencyLoading ? "Cerrando…" : "🔴 EMERGENCY STOP" }}
        </button>
      </div>
    </div>

    <div v-if="feedback" class="feedback" :class="feedback.type">
      {{ feedback.msg }}
    </div>

    <div v-if="positions.length === 0" class="empty">
      Sin posiciones abiertas actualmente.
    </div>

    <div v-else class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Símbolo</th>
            <th>Dir.</th>
            <th>Entrada</th>
            <th>SL</th>
            <th>TP</th>
            <th>Riesgo USDT</th>
            <th>IA prob.</th>
            <th>Vol ×</th>
            <th>Abierto</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="pos in positions" :key="pos.symbol">
            <td class="symbol">{{ pos.symbol }}</td>
            <td>
              <span class="dir-badge" :class="pos.direction">
                {{ pos.direction.toUpperCase() }}
              </span>
            </td>
            <td>{{ fmt(pos.entry_price) }}</td>
            <td class="negative">{{ fmt(pos.stop_loss) }}</td>
            <td class="positive">{{ fmt(pos.take_profit) }}</td>
            <td>{{ fmt(pos.risk_usdt, 2) }}</td>
            <td>{{ fmt(pos.ai_probability * 100, 1) }}%</td>
            <td>{{ fmt(pos.volume_ratio, 2) }}×</td>
            <td class="time">{{ pos.entry_time?.slice(11, 16) }} UTC</td>
            <td>
              <button
                class="btn btn-close"
                :disabled="closing[pos.symbol]"
                @click="handleClose(pos.symbol)"
              >
                {{ closing[pos.symbol] ? "…" : "Cerrar" }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.open-positions {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.5rem;
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

.actions {
  display: flex;
  gap: 0.5rem;
}

.btn {
  padding: 0.4rem 1rem;
  border-radius: 0.5rem;
  border: none;
  cursor: pointer;
  font-size: 0.85rem;
  font-weight: 600;
  transition: opacity 0.15s;
}
.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-emergency {
  background: var(--red);
  color: #fff;
  padding: 0.5rem 1.25rem;
  font-size: 0.9rem;
}
.btn-emergency:hover:not(:disabled) {
  opacity: 0.85;
}

.btn-resume {
  background: rgba(34, 197, 94, 0.15);
  color: var(--green);
  border: 1px solid var(--green);
}

.btn-close {
  background: rgba(239, 68, 68, 0.12);
  color: var(--red);
  border: 1px solid rgba(239, 68, 68, 0.3);
  padding: 0.3rem 0.75rem;
  font-size: 0.8rem;
}
.btn-close:hover:not(:disabled) {
  background: rgba(239, 68, 68, 0.22);
}

.feedback {
  padding: 0.6rem 1rem;
  border-radius: 0.5rem;
  font-size: 0.85rem;
}
.feedback.ok {
  background: rgba(34, 197, 94, 0.1);
  color: var(--green);
  border: 1px solid rgba(34, 197, 94, 0.3);
}
.feedback.err {
  background: rgba(239, 68, 68, 0.1);
  color: var(--red);
  border: 1px solid rgba(239, 68, 68, 0.3);
}

.empty {
  color: var(--text-muted);
  font-size: 0.9rem;
  padding: 2rem 0;
  text-align: center;
}

.table-wrap {
  overflow-x: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}

th {
  text-align: left;
  color: var(--text-muted);
  font-weight: 500;
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 0.5rem 0.75rem;
  border-bottom: 1px solid var(--border);
}

td {
  padding: 0.65rem 0.75rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  color: var(--text);
}

.symbol {
  font-weight: 600;
}

.dir-badge {
  padding: 0.15rem 0.5rem;
  border-radius: 0.3rem;
  font-size: 0.75rem;
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

.time {
  color: var(--text-muted);
  font-size: 0.8rem;
}
.positive {
  color: var(--green);
}
.negative {
  color: var(--red);
}
</style>
