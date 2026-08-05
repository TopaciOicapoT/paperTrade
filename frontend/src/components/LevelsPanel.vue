<script setup>
import { ref, onMounted } from "vue";

const symbols = ref([]);
const loading = ref(false);
const lastUpdate = ref(null);

const STATUS_META = {
  inside: { label: "Dentro del rango", cls: "status-inside" },
  near_resistance: { label: "⚡ Cerca de resistencia", cls: "status-near" },
  near_support: { label: "⚡ Cerca de soporte", cls: "status-near" },
  broke_resistance: { label: "▲ Rompió resistencia", cls: "status-long" },
  broke_support: { label: "▼ Rompió soporte", cls: "status-short" },
};

async function load() {
  loading.value = true;
  try {
    const res = await fetch("/api/levels");
    const data = await res.json();
    symbols.value = data.symbols ?? [];
    lastUpdate.value = new Date().toLocaleTimeString();
  } catch (e) {
    console.error(e);
  } finally {
    loading.value = false;
  }
}

onMounted(load);

function distBar(pct) {
  // Normalize distance to a 0-100% bar (cap at ±60%)
  const capped = Math.min(Math.abs(pct), 60);
  return Math.round((capped / 60) * 100);
}
</script>

<template>
  <div class="levels-panel">
    <div class="section-header">
      <h2>Niveles mensuales</h2>
      <div class="header-right">
        <span v-if="lastUpdate" class="last-update"
          >Actualizado {{ lastUpdate }}</span
        >
        <button class="btn btn-refresh" :disabled="loading" @click="load">
          {{ loading ? "…" : "↻ Actualizar" }}
        </button>
      </div>
    </div>

    <div v-if="loading && symbols.length === 0" class="loading-msg">
      Consultando niveles… (puede tardar ~10 segundos)
    </div>

    <div class="grid">
      <div
        v-for="sym in symbols"
        :key="sym.symbol"
        class="sym-card"
        :class="{ 'card-signal': sym.signal }"
      >
        <!-- Cabecera -->
        <div class="sym-header">
          <span class="sym-name">{{ sym.symbol }}</span>
          <span class="sym-badge" :class="STATUS_META[sym.status]?.cls">
            {{ STATUS_META[sym.status]?.label ?? sym.status }}
          </span>
        </div>

        <div v-if="sym.error" class="sym-error">{{ sym.error }}</div>

        <template v-else>
          <!-- Precio y niveles -->
          <div class="price-row">
            <span class="price">${{ sym.price }}</span>
          </div>

          <div class="levels-row">
            <!-- Resistencia -->
            <div class="level-item">
              <span class="level-label">Resistencia</span>
              <span class="level-val">{{ sym.resistance }}</span>
              <span
                class="level-dist"
                :class="sym.dist_res_pct > 0 ? 'positive' : 'negative'"
              >
                {{ sym.dist_res_pct > 0 ? "+" : ""
                }}{{ sym.dist_res_pct.toFixed(1) }}%
              </span>
              <div class="dist-bar-wrap">
                <div
                  class="dist-bar dist-bar-res"
                  :style="{ width: distBar(sym.dist_res_pct) + '%' }"
                />
              </div>
            </div>

            <!-- Soporte -->
            <div class="level-item">
              <span class="level-label">Soporte</span>
              <span class="level-val">{{ sym.support }}</span>
              <span
                class="level-dist"
                :class="sym.dist_sup_pct < 0 ? 'negative' : 'positive'"
              >
                {{ sym.dist_sup_pct > 0 ? "+" : ""
                }}{{ sym.dist_sup_pct.toFixed(1) }}%
              </span>
              <div class="dist-bar-wrap">
                <div
                  class="dist-bar dist-bar-sup"
                  :style="{ width: distBar(sym.dist_sup_pct) + '%' }"
                />
              </div>
            </div>
          </div>

          <!-- Diagnóstico de filtros (solo si hay señal) -->
          <template v-if="sym.signal">
            <div class="signal-header">
              <span
                class="signal-badge"
                :class="sym.signal === 'long' ? 'sig-long' : 'sig-short'"
              >
                SEÑAL {{ sym.signal.toUpperCase() }}
              </span>
              <span v-if="sym.can_enter" class="can-enter">✓ Puede entrar</span>
              <span v-else class="cannot-enter">
                Bloqueado: {{ sym.blocking.join(", ") }}
              </span>
            </div>

            <div class="filters-list">
              <div
                v-for="f in sym.filters"
                :key="f.name"
                class="filter-row"
                :class="f.passed ? 'f-pass' : 'f-block'"
              >
                <span class="f-icon">{{ f.passed ? "✓" : "✗" }}</span>
                <div class="f-content">
                  <span class="f-name">{{ f.name }}</span>
                  <span class="f-reason">{{ f.reason }}</span>
                </div>
              </div>
            </div>
          </template>

          <!-- Sin señal: mostrar distancia al nivel más cercano -->
          <template v-else>
            <div class="no-signal">
              Sin señal activa — precio dentro del rango mensual.
              <span
                v-if="Math.abs(sym.dist_res_pct) < Math.abs(sym.dist_sup_pct)"
              >
                Nivel más cercano: resistencia a
                {{ sym.dist_res_pct.toFixed(1) }}%
              </span>
              <span v-else>
                Nivel más cercano: soporte a {{ sym.dist_sup_pct.toFixed(1) }}%
              </span>
            </div>
          </template>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.levels-panel {
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
}
.header-right {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}
.last-update {
  font-size: 0.75rem;
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

.loading-msg {
  color: var(--text-muted);
  font-size: 0.9rem;
  padding: 1rem 0;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 1rem;
}

.sym-card {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.75rem;
  padding: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}
.sym-card.card-signal {
  border-color: rgba(99, 102, 241, 0.4);
}

.sym-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.sym-name {
  font-weight: 700;
  font-size: 0.95rem;
  color: var(--text);
}

.sym-badge {
  font-size: 0.7rem;
  font-weight: 600;
  padding: 0.15rem 0.5rem;
  border-radius: 0.3rem;
  letter-spacing: 0.03em;
}
.status-inside {
  background: rgba(100, 116, 139, 0.15);
  color: var(--text-muted);
}
.status-near {
  background: rgba(234, 179, 8, 0.15);
  color: #eab308;
}
.status-long {
  background: rgba(34, 197, 94, 0.15);
  color: var(--green);
}
.status-short {
  background: rgba(239, 68, 68, 0.15);
  color: var(--red);
}

.price {
  font-size: 1.2rem;
  font-weight: 700;
  color: var(--text);
}

.levels-row {
  display: flex;
  gap: 1rem;
}
.level-item {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}
.level-label {
  font-size: 0.7rem;
  text-transform: uppercase;
  color: var(--text-muted);
  letter-spacing: 0.04em;
}
.level-val {
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--text);
}
.level-dist {
  font-size: 0.8rem;
}
.positive {
  color: var(--green);
}
.negative {
  color: var(--red);
}

.dist-bar-wrap {
  height: 3px;
  background: rgba(255, 255, 255, 0.06);
  border-radius: 2px;
  margin-top: 2px;
}
.dist-bar {
  height: 100%;
  border-radius: 2px;
  min-width: 2px;
}
.dist-bar-res {
  background: var(--green);
}
.dist-bar-sup {
  background: var(--red);
}

/* Señal activa */
.signal-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
  padding-top: 0.5rem;
  border-top: 1px solid var(--border);
}
.signal-badge {
  font-size: 0.72rem;
  font-weight: 700;
  padding: 0.2rem 0.6rem;
  border-radius: 0.3rem;
}
.sig-long {
  background: rgba(34, 197, 94, 0.2);
  color: var(--green);
}
.sig-short {
  background: rgba(239, 68, 68, 0.2);
  color: var(--red);
}

.can-enter {
  font-size: 0.75rem;
  color: var(--green);
  font-weight: 600;
}
.cannot-enter {
  font-size: 0.75rem;
  color: var(--red);
}

/* Lista de filtros */
.filters-list {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.filter-row {
  display: flex;
  gap: 0.5rem;
  align-items: flex-start;
  padding: 0.35rem 0.5rem;
  border-radius: 0.4rem;
  font-size: 0.78rem;
}
.f-pass {
  background: rgba(34, 197, 94, 0.07);
}
.f-block {
  background: rgba(239, 68, 68, 0.07);
}
.f-icon {
  font-size: 0.75rem;
  margin-top: 1px;
  flex-shrink: 0;
}
.f-pass .f-icon {
  color: var(--green);
}
.f-block .f-icon {
  color: var(--red);
}
.f-content {
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.f-name {
  font-weight: 600;
  color: var(--text);
}
.f-reason {
  color: var(--text-muted);
  line-height: 1.4;
}

/* Sin señal */
.no-signal {
  font-size: 0.8rem;
  color: var(--text-muted);
  padding: 0.5rem 0;
  border-top: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}

.sym-error {
  font-size: 0.8rem;
  color: var(--red);
}
</style>
