<script setup>
import { ref, computed, onMounted } from "vue";

const cfg = ref(null);
const available = ref([]);
const saving = ref(false);
const saved = ref(false);
const saveError = ref(null);

// ── Estado editable ───────────────────────────────────────────────────────────
const maxPositions = ref(3);
const leverage = ref(3);
const selectedEntries = ref([]); // [{ id, symbol, strategy, filters }]
let _nextId = 1;
const dropdownSym = ref("");
const dropdownStrat = ref("breakout");

const activeSymbols = computed(() => [
  ...new Set(selectedEntries.value.map((e) => e.symbol)),
]);

// ── Helpers (igual que LabView) ───────────────────────────────────────────────
function _defaultFilters(sp = {}, strategy = "breakout") {
  if (strategy === "retest") {
    return {
      retest_min_move_pct: sp.retest_min_move_pct ?? 0.5,
      retest_tolerance_pct: sp.retest_tolerance_pct ?? 0.35,
      retest_pullback_vol_max: sp.retest_pullback_vol_max ?? 1.5,
    };
  }
  if (strategy === "bounce") return {};
  return {
    f1: {
      enabled: "momentum_q3_block" in sp,
      lo: sp.momentum_q3_block?.[0] ?? 0.3,
      hi: sp.momentum_q3_block?.[1] ?? 1.6,
    },
    f2b: {
      enabled: "session_block_hours" in sp,
      lo: sp.session_block_hours?.[0] ?? 8,
      hi: sp.session_block_hours?.[1] ?? 14,
    },
    f3: {
      enabled: "usdt_norm_block_range" in sp,
      lo: sp.usdt_norm_block_range?.[0] ?? 2.1,
      hi: sp.usdt_norm_block_range?.[1] ?? 2.7,
    },
    f4: {
      enabled: "rsi_overbought_block" in sp,
      threshold: sp.rsi_overbought_block ?? 70,
    },
    fr: { enabled: sp.failed_retest_filter !== false },
    volmax: {
      enabled: "volume_trigger_ratio_max" in sp,
      max: sp.volume_trigger_ratio_max ?? 2.8,
    },
  };
}

// Convierte las entries a symbol_params flat para config.yaml (por símbolo)
function buildSymbolParams() {
  const result = {};
  for (const { symbol, strategy, filters } of selectedEntries.value) {
    if (!result[symbol]) result[symbol] = { strategies: [] };
    result[symbol].strategies.push(strategy);
    if (strategy === "breakout") {
      if (filters.f1?.enabled)
        result[symbol].momentum_q3_block = [
          Number(filters.f1.lo),
          Number(filters.f1.hi),
        ];
      if (filters.f2b?.enabled)
        result[symbol].session_block_hours = [
          Number(filters.f2b.lo),
          Number(filters.f2b.hi),
        ];
      if (filters.f3?.enabled)
        result[symbol].usdt_norm_block_range = [
          Number(filters.f3.lo),
          Number(filters.f3.hi),
        ];
      if (filters.f4?.enabled)
        result[symbol].rsi_overbought_block = Number(filters.f4.threshold);
      if (!filters.fr?.enabled) result[symbol].failed_retest_filter = false;
      if (filters.volmax?.enabled)
        result[symbol].volume_trigger_ratio_max = Number(filters.volmax.max);
    } else if (strategy === "retest") {
      result[symbol].retest_min_move_pct = Number(filters.retest_min_move_pct);
      result[symbol].retest_tolerance_pct = Number(
        filters.retest_tolerance_pct,
      );
      result[symbol].retest_pullback_vol_max = Number(
        filters.retest_pullback_vol_max,
      );
    }
  }
  return result;
}

// ── Carga ─────────────────────────────────────────────────────────────────────
async function load() {
  const [cfgRes, symRes] = await Promise.all([
    fetch("/api/config"),
    fetch("/api/lab/symbols"),
  ]);
  cfg.value = await cfgRes.json();
  const symData = await symRes.json();
  available.value = symData.available ?? [];

  maxPositions.value = cfg.value.max_open_positions ?? 3;
  leverage.value = cfg.value.leverage ?? 3;

  selectedEntries.value = [];
  const sp = cfg.value.symbol_params ?? {};
  for (const sym of cfg.value.symbols ?? []) {
    const symSp = sp[sym] ?? {};
    const strats = symSp.strategies ?? ["breakout"];
    for (const strat of strats) {
      selectedEntries.value.push({
        id: _nextId++,
        symbol: sym,
        strategy: strat,
        filters: _defaultFilters(symSp, strat),
      });
    }
  }
}
onMounted(load);

// ── Add / remove ──────────────────────────────────────────────────────────────
function addSymbol() {
  const sym = dropdownSym.value;
  const strat = dropdownStrat.value;
  if (!sym || !strat) return;
  if (
    selectedEntries.value.find((e) => e.symbol === sym && e.strategy === strat)
  )
    return;
  const sp = cfg.value?.symbol_params?.[sym] ?? {};
  selectedEntries.value.push({
    id: _nextId++,
    symbol: sym,
    strategy: strat,
    filters: _defaultFilters(sp, strat),
  });
  dropdownSym.value = "";
}

function removeEntry(id) {
  selectedEntries.value = selectedEntries.value.filter((e) => e.id !== id);
}

// ── Guardar ───────────────────────────────────────────────────────────────────
async function save() {
  if (!activeSymbols.value.length) return;
  saving.value = true;
  saved.value = false;
  saveError.value = null;
  try {
    const res = await fetch("/api/config", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbols: activeSymbols.value,
        max_open_positions: maxPositions.value,
        leverage: leverage.value,
        symbol_params: buildSymbolParams(),
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail ?? "Error al guardar");
    saved.value = true;
    await load();
    setTimeout(() => {
      saved.value = false;
    }, 4000);
  } catch (e) {
    saveError.value = e.message;
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div class="config-editor">
    <h2>Configuración del bot</h2>
    <p class="subtitle">
      Los cambios se guardan en <code>config/config.yaml</code> y se aplican al
      bot <strong>en el siguiente ciclo</strong> (10 segundos) sin reinicio.
    </p>

    <div v-if="!cfg" class="loading">Cargando configuración…</div>

    <template v-else>
      <!-- ── Parámetros globales ─────────────────────────────────────────── -->
      <div class="section">
        <div class="section-title">Parámetros globales</div>
        <div class="params-grid">
          <div class="param">
            <label>Posiciones simultáneas</label>
            <input
              v-model.number="maxPositions"
              type="number"
              min="1"
              max="10"
            />
            <span class="param-hint">
              {{
                maxPositions > 1
                  ? (100 / maxPositions).toFixed(1) + "% del capital por trade"
                  : "100% por trade"
              }}
            </span>
          </div>
          <div class="param">
            <label>Apalancamiento (×)</label>
            <input v-model.number="leverage" type="number" min="1" max="20" />
            <span class="param-hint">{{
              leverage > 1
                ? `Futuros ${leverage}×`
                : "Spot (sin apalancamiento)"
            }}</span>
          </div>
        </div>
      </div>

      <!-- ── Símbolos activos + filtros ─────────────────────────────────── -->
      <div class="section">
        <div class="section-title">Criptomonedas activas y filtros</div>
        <p class="section-hint">
          Configura qué criptomonedas monitoriza el bot y qué filtros aplica a
          cada una. Los filtros reducen el número de entradas pero mejoran el
          Win Rate.
        </p>

        <!-- Selector para añadir símbolo + estrategia -->
        <div class="sym-add-row">
          <select v-model="dropdownSym" class="sym-select">
            <option value="" disabled>— Selecciona una criptomoneda —</option>
            <optgroup
              v-for="group in [
                {
                  label: 'Majors',
                  syms: [
                    'BTC/USDT',
                    'ETH/USDT',
                    'BNB/USDT',
                    'XRP/USDT',
                    'LTC/USDT',
                    'TRX/USDT',
                  ],
                },
                {
                  label: 'Smart contracts',
                  syms: [
                    'ADA/USDT',
                    'SOL/USDT',
                    'DOT/USDT',
                    'AVAX/USDT',
                    'ATOM/USDT',
                    'EGLD/USDT',
                    'NEAR/USDT',
                    'ALGO/USDT',
                    'ICP/USDT',
                    'THETA/USDT',
                    'VET/USDT',
                  ],
                },
                {
                  label: 'L2 / Infra',
                  syms: [
                    'MATIC/USDT',
                    'ARB/USDT',
                    'OP/USDT',
                    'APT/USDT',
                    'STX/USDT',
                  ],
                },
                {
                  label: 'DeFi',
                  syms: ['LINK/USDT', 'AAVE/USDT', 'UNI/USDT', 'INJ/USDT'],
                },
                {
                  label: 'Otros',
                  syms: [
                    'DOGE/USDT',
                    'XLM/USDT',
                    'HBAR/USDT',
                    'FIL/USDT',
                    'TON/USDT',
                    'AXS/USDT',
                    'SAND/USDT',
                  ],
                },
              ]"
              :key="group.label"
              :label="group.label"
            >
              <option
                v-for="sym in group.syms.filter(
                  (s) =>
                    !selectedEntries.find(
                      (e) => e.symbol === s && e.strategy === dropdownStrat,
                    ),
                )"
                :key="sym"
                :value="sym"
              >
                {{ sym }}
              </option>
            </optgroup>
          </select>
          <select v-model="dropdownStrat" class="strat-select">
            <option value="breakout">Breakout</option>
            <option value="retest">Retest</option>
            <option value="bounce">Bounce</option>
          </select>
          <button
            class="btn-add-sym"
            :disabled="
              !dropdownSym ||
              !!selectedEntries.find(
                (e) => e.symbol === dropdownSym && e.strategy === dropdownStrat,
              )
            "
            @click="addSymbol"
          >
            + Añadir
          </button>
        </div>

        <div v-if="!selectedEntries.length" class="field-warn">
          El bot necesita al menos un símbolo activo.
        </div>

        <!-- Tarjetas por (símbolo + estrategia) -->
        <div v-else class="sym-cards">
          <div
            v-for="item in selectedEntries"
            :key="item.id"
            class="sym-filter-card"
          >
            <div class="sfc-header">
              <span class="sfc-name">{{ item.symbol }}</span>
              <span class="sfc-strat-badge" :class="`strat-${item.strategy}`">{{
                item.strategy
              }}</span>
              <button class="sfc-remove" @click="removeEntry(item.id)">
                ✕
              </button>
            </div>
            <div class="sfc-filters">
              <!-- ── Breakout: filtros F1-F4 ── -->
              <template v-if="item.strategy === 'breakout'">
                <div class="sfc-section-label">Filtros de calidad</div>
                <label class="sfc-row">
                  <input type="checkbox" v-model="item.filters.f1.enabled" />
                  <span class="sfc-fname">Trampa de momentum</span>
                  <template v-if="item.filters.f1.enabled">
                    <input
                      class="sfc-num"
                      type="number"
                      v-model.number="item.filters.f1.lo"
                      step="0.1"
                    /><span class="sfc-sep">–</span>
                    <input
                      class="sfc-num"
                      type="number"
                      v-model.number="item.filters.f1.hi"
                      step="0.1"
                    /><span class="sfc-unit">%</span>
                  </template>
                  <span v-else class="sfc-hint"
                    >evita la zona donde el precio acelera antes de
                    revertir</span
                  >
                </label>
                <label class="sfc-row">
                  <input type="checkbox" v-model="item.filters.f2b.enabled" />
                  <span class="sfc-fname">Horario restringido</span>
                  <template v-if="item.filters.f2b.enabled">
                    <input
                      class="sfc-num"
                      type="number"
                      v-model.number="item.filters.f2b.lo"
                      min="0"
                      max="23"
                    /><span class="sfc-sep">–</span>
                    <input
                      class="sfc-num"
                      type="number"
                      v-model.number="item.filters.f2b.hi"
                      min="1"
                      max="24"
                    /><span class="sfc-unit">h UTC</span>
                  </template>
                  <span v-else class="sfc-hint"
                    >apertura Londres/NY — WR 23-26% en esas horas</span
                  >
                </label>
                <label class="sfc-row">
                  <input type="checkbox" v-model="item.filters.f3.enabled" />
                  <span class="sfc-fname">Trampa de volumen</span>
                  <template v-if="item.filters.f3.enabled">
                    <input
                      class="sfc-num"
                      type="number"
                      v-model.number="item.filters.f3.lo"
                      step="0.1"
                    /><span class="sfc-sep">–</span>
                    <input
                      class="sfc-num"
                      type="number"
                      v-model.number="item.filters.f3.hi"
                      step="0.1"
                    /><span class="sfc-unit">×</span>
                  </template>
                  <span v-else class="sfc-hint"
                    >volumen intermedio 2.1-2.7× — zona de fakeouts</span
                  >
                </label>
                <label class="sfc-row">
                  <input type="checkbox" v-model="item.filters.f4.enabled" />
                  <span class="sfc-fname">Sobrecompra RSI ≥</span>
                  <template v-if="item.filters.f4.enabled">
                    <input
                      class="sfc-num"
                      type="number"
                      v-model.number="item.filters.f4.threshold"
                      min="50"
                      max="100"
                    /><span class="sfc-unit">(sobrecompra)</span>
                  </template>
                  <span v-else class="sfc-hint"
                    >RSI ≥ 70 → WR 25.9% vs 34.2% normal</span
                  >
                </label>
                <label class="sfc-row">
                  <input type="checkbox" v-model="item.filters.fr.enabled" />
                  <span class="sfc-fname">Anti-fakeout</span>
                  <span class="sfc-hint">auto-calibrado por nivel</span>
                </label>
                <label class="sfc-row">
                  <input
                    type="checkbox"
                    v-model="item.filters.volmax.enabled"
                  />
                  <span class="sfc-fname">Spike extremo ≤</span>
                  <template v-if="item.filters.volmax.enabled">
                    <input
                      class="sfc-num"
                      type="number"
                      v-model.number="item.filters.volmax.max"
                      step="0.1"
                    /><span class="sfc-unit">×</span>
                  </template>
                  <span v-else class="sfc-hint"
                    >bloquea spikes de volumen extremos</span
                  >
                </label>
              </template>

              <!-- ── Retest: parámetros específicos ── -->
              <template v-else-if="item.strategy === 'retest'">
                <div class="sfc-section-label">Parámetros de retest</div>
                <div class="retest-param">
                  <span class="sfc-fname retest-label">Distancia mínima</span>
                  <input
                    class="sfc-num"
                    type="number"
                    step="0.1"
                    v-model.number="item.filters.retest_min_move_pct"
                  />
                  <span class="sfc-unit">% (precio alejado del nivel)</span>
                </div>
                <div class="retest-param">
                  <span class="sfc-fname retest-label">Proximidad máxima</span>
                  <input
                    class="sfc-num"
                    type="number"
                    step="0.05"
                    v-model.number="item.filters.retest_tolerance_pct"
                  />
                  <span class="sfc-unit">% (proximidad al nivel)</span>
                </div>
                <div class="retest-param">
                  <span class="sfc-fname retest-label">Vol. pullback máx</span>
                  <input
                    class="sfc-num"
                    type="number"
                    step="0.1"
                    v-model.number="item.filters.retest_pullback_vol_max"
                  />
                  <span class="sfc-unit">× (pullback silencioso)</span>
                </div>
              </template>

              <!-- ── Bounce: sin parámetros configurables ── -->
              <template v-else-if="item.strategy === 'bounce'">
                <div class="sfc-hint" style="padding: 0.4rem 0">
                  Sin parámetros configurables — usa el midpoint del rango
                  mensual como TP.
                </div>
              </template>
            </div>
          </div>
        </div>
      </div>

      <!-- ── Guardar ────────────────────────────────────────────────────── -->
      <div class="save-row">
        <button
          class="btn-save"
          :disabled="saving || !activeSymbols.length"
          @click="save"
        >
          {{ saving ? "Guardando…" : "💾 Guardar y aplicar" }}
        </button>
        <span v-if="saved" class="save-ok"
          >✓ Guardado y aplicado. El bot usa la nueva configuración en el
          próximo ciclo (10s).</span
        >
        <span v-if="saveError" class="save-err">{{ saveError }}</span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.config-editor {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
}
h2 {
  font-size: 1.1rem;
  font-weight: 700;
  color: var(--text);
}
.subtitle {
  font-size: 0.82rem;
  color: var(--text-muted);
}
code {
  background: var(--bg);
  padding: 0.1rem 0.35rem;
  border-radius: 0.3rem;
  font-size: 0.78rem;
}
.loading {
  color: var(--text-muted);
  font-size: 0.9rem;
}

.section {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}
.section-title {
  font-weight: 600;
  font-size: 0.95rem;
  color: var(--text);
}
.section-hint {
  font-size: 0.78rem;
  color: var(--text-muted);
}

/* Parámetros globales */
.params-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 1rem;
}
.param {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
label {
  font-size: 0.8rem;
  color: var(--text-muted);
  font-weight: 500;
}
input[type="number"] {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.5rem;
  color: var(--text);
  padding: 0.4rem 0.75rem;
  font-size: 0.9rem;
  width: 100%;
}
input[type="number"]:focus {
  outline: none;
  border-color: var(--accent);
}
.param-hint {
  font-size: 0.75rem;
  color: var(--text-muted);
}

/* Selector de símbolo */
.sym-add-row {
  display: flex;
  gap: 0.5rem;
}
.sym-select {
  flex: 1;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.5rem;
  color: var(--text);
  padding: 0.4rem 0.75rem;
  font-size: 0.88rem;
}
.sym-select:focus,
.strat-select:focus {
  outline: none;
  border-color: var(--accent);
}
.strat-select {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.5rem;
  color: var(--text);
  padding: 0.4rem 0.55rem;
  font-size: 0.85rem;
  font-weight: 600;
}
.btn-add-sym {
  padding: 0.4rem 1rem;
  border-radius: 0.5rem;
  border: 1px solid var(--accent);
  background: rgba(99, 102, 241, 0.12);
  color: #a5b4fc;
  font-weight: 600;
  font-size: 0.85rem;
  cursor: pointer;
  white-space: nowrap;
}
.btn-add-sym:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.btn-add-sym:hover:not(:disabled) {
  background: rgba(99, 102, 241, 0.2);
}

.field-warn {
  font-size: 0.75rem;
  color: var(--red);
}

/* Tarjetas de símbolo + filtros */
.sym-cards {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}
.sym-filter-card {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.75rem;
  overflow: hidden;
}
.sfc-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
  padding: 0.55rem 0.9rem;
  background: rgba(255, 255, 255, 0.03);
  border-bottom: 1px solid var(--border);
}
.sfc-strat-badge {
  font-size: 0.7rem;
  font-weight: 700;
  padding: 0.15rem 0.45rem;
  border-radius: 0.9rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  flex-shrink: 0;
}
.strat-breakout {
  background: rgba(99, 102, 241, 0.18);
  color: #a5b4fc;
}
.strat-retest {
  background: rgba(34, 197, 94, 0.15);
  color: #4ade80;
}
.strat-bounce {
  background: rgba(251, 146, 60, 0.15);
  color: #fb923c;
}
.sfc-name {
  font-weight: 700;
  font-size: 0.9rem;
  color: var(--text);
}
.sfc-remove {
  background: transparent;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 0.85rem;
  padding: 0.1rem 0.3rem;
  border-radius: 0.3rem;
}
.sfc-remove:hover {
  color: var(--red);
  background: rgba(239, 68, 68, 0.1);
}
.sfc-filters {
  padding: 0.55rem 0.9rem;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}
.sfc-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  cursor: pointer;
  font-size: 0.82rem;
  color: var(--text);
}
.sfc-row input[type="checkbox"] {
  flex-shrink: 0;
  accent-color: var(--accent);
}
.sfc-fname {
  font-weight: 600;
  min-width: 140px;
}
.sfc-num {
  width: 60px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 0.35rem;
  color: var(--text);
  padding: 0.15rem 0.4rem;
  font-size: 0.8rem;
  text-align: center;
}
.sfc-num:focus {
  outline: none;
  border-color: var(--accent);
}
.sfc-sep {
  color: var(--text-muted);
}
.sfc-unit {
  font-size: 0.75rem;
  color: var(--text-muted);
}
.sfc-hint {
  font-size: 0.72rem;
  color: var(--text-muted);
}
.sfc-section-label {
  font-size: 0.68rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  font-weight: 600;
  padding-top: 0.15rem;
}

/* Parámetros de retest expandibles */
.retest-params {
  margin-left: 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  padding: 0.3rem 0.6rem;
  background: rgba(99, 102, 241, 0.06);
  border-left: 2px solid rgba(99, 102, 241, 0.3);
  border-radius: 0 0.35rem 0.35rem 0;
}
.retest-param {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.78rem;
}
.retest-label {
  min-width: 110px;
  color: var(--text-muted);
  font-weight: 500;
}

/* Guardar */
.save-row {
  display: flex;
  align-items: center;
  gap: 1rem;
  flex-wrap: wrap;
}
.btn-save {
  padding: 0.55rem 1.5rem;
  border-radius: 0.6rem;
  border: none;
  background: var(--accent);
  color: #fff;
  font-weight: 700;
  font-size: 0.9rem;
  cursor: pointer;
}
.btn-save:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn-save:hover:not(:disabled) {
  opacity: 0.88;
}
.save-ok {
  font-size: 0.82rem;
  color: var(--green);
}
.save-err {
  font-size: 0.82rem;
  color: var(--red);
}
</style>
