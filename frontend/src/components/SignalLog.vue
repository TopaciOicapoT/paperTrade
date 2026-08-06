<script setup>
import { ref, onMounted, onUnmounted, computed } from "vue";

const events = ref([]);
const loading = ref(false);
const filterSym = ref("");
const filterType = ref("");
let pollTimer = null;

const EVENT_LABELS = {
  TRADE_OPENED: { label: "Trade abierto", cls: "ev-trade" },
  REJECTED_CAPACITY: { label: "Sin slot", cls: "ev-capacity" },
  REJECTED_NEWS: { label: "Pausa news", cls: "ev-news" },
  REJECTED_SESSION: { label: "Sesión bloqueada", cls: "ev-filter" },
  REJECTED_VOLUME: { label: "Vol. bloqueado", cls: "ev-filter" },
  REJECTED_MOMENTUM: { label: "Momentum bloqueado", cls: "ev-filter" },
  REJECTED_RSI: { label: "RSI sobrecompra", cls: "ev-filter" },
  REJECTED_AI: { label: "IA rechazó", cls: "ev-ai" },
  REJECTED_RISK: { label: "Riesgo inválido", cls: "ev-risk" },
};

const STRAT_COLORS = {
  breakout: "#a5b4fc",
  retest: "#4ade80",
  bounce: "#fb923c",
};

const uniqueSymbols = computed(() =>
  [...new Set(events.value.map((e) => e.symbol))].sort(),
);
const uniqueTypes = computed(() =>
  [...new Set(events.value.map((e) => e.event_type))].sort(),
);

const filtered = computed(() => {
  let ev = events.value;
  if (filterSym.value) ev = ev.filter((e) => e.symbol === filterSym.value);
  if (filterType.value)
    ev = ev.filter((e) => e.event_type === filterType.value);
  return ev;
});

async function fetchEvents() {
  loading.value = true;
  try {
    const params = new URLSearchParams({ limit: 200 });
    if (filterSym.value) params.set("symbol", filterSym.value);
    if (filterType.value) params.set("event_type", filterType.value);
    const res = await fetch(`/api/events?${params}`);
    events.value = await res.json();
  } catch {
    /* silencioso */
  } finally {
    loading.value = false;
  }
}

function fmtTs(iso) {
  if (!iso) return "—";
  const d = new Date(iso + "Z");
  return d
    .toLocaleString("es-ES", { timeZone: "UTC", hour12: false })
    .replace(",", "");
}
function fmtPrice(p) {
  if (p == null) return "—";
  return p < 10 ? p.toFixed(5) : p.toFixed(2);
}

onMounted(() => {
  fetchEvents();
  pollTimer = setInterval(fetchEvents, 15000);
});
onUnmounted(() => clearInterval(pollTimer));
</script>

<template>
  <div class="signal-log">
    <div class="sl-header">
      <div class="sl-title">
        📋 Registro de señales
        <span class="sl-count"
          >{{ filtered.length }} evento{{
            filtered.length !== 1 ? "s" : ""
          }}</span
        >
      </div>
      <div class="sl-filters">
        <select v-model="filterSym" @change="fetchEvents" class="sl-select">
          <option value="">Todos los símbolos</option>
          <option v-for="s in uniqueSymbols" :key="s" :value="s">
            {{ s }}
          </option>
        </select>
        <select v-model="filterType" @change="fetchEvents" class="sl-select">
          <option value="">Todos los tipos</option>
          <option v-for="t in uniqueTypes" :key="t" :value="t">
            {{ EVENT_LABELS[t]?.label ?? t }}
          </option>
        </select>
        <button class="sl-refresh" @click="fetchEvents" :class="{ loading }">
          ↻
        </button>
      </div>
    </div>

    <div v-if="!filtered.length && !loading" class="sl-empty">
      Sin señales registradas aún. El bot escribe aquí cada vez que detecta un
      trigger (trade tomado o rechazado).
    </div>

    <div class="sl-table-wrap">
      <table v-if="filtered.length" class="sl-table">
        <thead>
          <tr>
            <th>Timestamp UTC</th>
            <th>Símbolo</th>
            <th>Estrategia</th>
            <th>Dir.</th>
            <th>Precio</th>
            <th>Vol×</th>
            <th>Evento</th>
            <th>Razón / Detalle</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="ev in filtered"
            :key="ev.id"
            :class="EVENT_LABELS[ev.event_type]?.cls"
          >
            <td class="ts">{{ fmtTs(ev.timestamp) }}</td>
            <td class="sym">{{ ev.symbol }}</td>
            <td>
              <span
                class="strat-badge"
                :style="`color:${STRAT_COLORS[ev.strategy] ?? '#ccc'}`"
              >
                {{ ev.strategy }}
              </span>
            </td>
            <td>
              <span v-if="ev.direction" class="dir-badge" :class="ev.direction">
                {{ ev.direction === "long" ? "▲" : "▼" }} {{ ev.direction }}
              </span>
            </td>
            <td class="price">{{ fmtPrice(ev.price) }}</td>
            <td class="vol">
              {{
                ev.volume_ratio != null ? ev.volume_ratio.toFixed(2) + "×" : "—"
              }}
            </td>
            <td>
              <span class="ev-badge" :class="EVENT_LABELS[ev.event_type]?.cls">
                {{ EVENT_LABELS[ev.event_type]?.label ?? ev.event_type }}
              </span>
            </td>
            <td class="reason">
              {{
                ev.rejection_reason ??
                (ev.event_type === "TRADE_OPENED"
                  ? "✅ Orden paper abierta"
                  : "")
              }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.signal-log {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  margin-top: 1.5rem;
}
.sl-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.5rem;
}
.sl-title {
  font-size: 0.95rem;
  font-weight: 600;
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.sl-count {
  font-size: 0.75rem;
  color: var(--text-muted);
  font-weight: 400;
}
.sl-filters {
  display: flex;
  gap: 0.4rem;
  align-items: center;
}
.sl-select {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.4rem;
  color: var(--text);
  padding: 0.25rem 0.5rem;
  font-size: 0.78rem;
}
.sl-refresh {
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 0.4rem;
  color: var(--text-muted);
  cursor: pointer;
  padding: 0.25rem 0.5rem;
  font-size: 0.85rem;
}
.sl-refresh.loading {
  opacity: 0.5;
}
.sl-refresh:hover {
  color: var(--text);
}
.sl-empty {
  font-size: 0.78rem;
  color: var(--text-muted);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 0.5rem;
  padding: 0.75rem 1rem;
}
.sl-table-wrap {
  overflow-x: auto;
  max-height: 400px;
  overflow-y: auto;
  border-radius: 0.5rem;
  border: 1px solid var(--border);
}
.sl-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.76rem;
}
.sl-table th {
  position: sticky;
  top: 0;
  background: var(--surface);
  padding: 0.4rem 0.6rem;
  text-align: left;
  color: var(--text-muted);
  font-weight: 500;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
.sl-table td {
  padding: 0.35rem 0.6rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  vertical-align: middle;
}
.sl-table tr:hover td {
  background: rgba(255, 255, 255, 0.03);
}

/* Row background by event type */
tr.ev-trade td {
  background: rgba(34, 197, 94, 0.06);
}
tr.ev-filter td {
  background: rgba(251, 191, 36, 0.04);
}
tr.ev-ai td {
  background: rgba(168, 85, 247, 0.06);
}
tr.ev-risk td {
  background: rgba(239, 68, 68, 0.06);
}
tr.ev-capacity td,
tr.ev-news td {
  background: rgba(148, 163, 184, 0.05);
}

.ts {
  color: var(--text-muted);
  white-space: nowrap;
  font-size: 0.72rem;
}
.sym {
  font-weight: 600;
  white-space: nowrap;
}
.price {
  font-family: monospace;
}
.vol {
  font-family: monospace;
}
.reason {
  color: var(--text-muted);
  max-width: 320px;
}

.strat-badge {
  font-size: 0.7rem;
  font-weight: 600;
}

.dir-badge {
  font-size: 0.72rem;
  font-weight: 600;
}
.dir-badge.long {
  color: var(--green);
}
.dir-badge.short {
  color: var(--red);
}

.ev-badge {
  font-size: 0.68rem;
  font-weight: 700;
  padding: 0.1rem 0.4rem;
  border-radius: 0.8rem;
  white-space: nowrap;
}
.ev-badge.ev-trade {
  background: rgba(34, 197, 94, 0.18);
  color: #4ade80;
}
.ev-badge.ev-filter {
  background: rgba(251, 191, 36, 0.18);
  color: #fbbf24;
}
.ev-badge.ev-ai {
  background: rgba(168, 85, 247, 0.18);
  color: #c084fc;
}
.ev-badge.ev-risk {
  background: rgba(239, 68, 68, 0.18);
  color: #f87171;
}
.ev-badge.ev-capacity,
.ev-badge.ev-news {
  background: rgba(148, 163, 184, 0.15);
  color: #94a3b8;
}
</style>
