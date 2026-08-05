<script setup>
import { ref, computed, onMounted, onUnmounted } from "vue";

const emit = defineEmits(["simulationDone"]);

// ── Formulario ────────────────────────────────────────────────────────────────
const availableSymbols = ref([]);
const botConfig = ref(null);
const dropdownSym = ref("");
const dropdownStrat = ref("breakout"); // estrategia a añadir
const selectedSymbols = ref([]); // [{ id, symbol, strategy, filters }]
let _nextId = 1;
const capital = ref(100);
const maxPositions = ref(3);
const years = ref(10);
const leverage = ref(3);
const periodMode = ref("years"); // "years" | "range"
const dateFrom = ref("");
const dateTo = ref("");

// Símbolos únicos para el selector (filtra los ya añadidos con esa estrategia)
const selected = computed(() => [
  ...new Set(selectedSymbols.value.map((s) => s.symbol)),
]);

function _defaultFilters(sp = {}, strategy = "breakout") {
  if (strategy === "retest") {
    return {
      retest_min_move_pct: sp.retest_min_move_pct ?? 0.5,
      retest_tolerance_pct: sp.retest_tolerance_pct ?? 0.35,
      retest_pullback_vol_max: sp.retest_pullback_vol_max ?? 1.5,
    };
  }
  if (strategy === "bounce") {
    return {};
  }
  // breakout
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

function addSymbol() {
  const sym = dropdownSym.value;
  const strat = dropdownStrat.value;
  if (!sym || !strat) return;
  // Prevenir duplicado (mismo símbolo + misma estrategia)
  if (
    selectedSymbols.value.find((s) => s.symbol === sym && s.strategy === strat)
  )
    return;
  if (selectedSymbols.value.length >= 16) return;
  const sp = botConfig.value?.symbol_params?.[sym] ?? {};
  selectedSymbols.value.push({
    id: _nextId++,
    symbol: sym,
    strategy: strat,
    filters: _defaultFilters(sp, strat),
  });
  dropdownSym.value = "";
}

function removeEntry(id) {
  selectedSymbols.value = selectedSymbols.value.filter((s) => s.id !== id);
}

// Construye la lista de entries para la API: [{symbol, strategy, ...params}]
function buildStrategyEntries() {
  return selectedSymbols.value.map(({ symbol, strategy, filters }) => {
    const entry = { symbol, strategy };
    if (strategy === "breakout") {
      if (filters.f1?.enabled)
        entry.momentum_q3_block = [
          Number(filters.f1.lo),
          Number(filters.f1.hi),
        ];
      if (filters.f2b?.enabled)
        entry.session_block_hours = [
          Number(filters.f2b.lo),
          Number(filters.f2b.hi),
        ];
      if (filters.f3?.enabled)
        entry.usdt_norm_block_range = [
          Number(filters.f3.lo),
          Number(filters.f3.hi),
        ];
      if (filters.f4?.enabled)
        entry.rsi_overbought_block = Number(filters.f4.threshold);
      if (!filters.fr?.enabled) entry.failed_retest_filter = false;
      if (filters.volmax?.enabled)
        entry.volume_trigger_ratio_max = Number(filters.volmax.max);
    } else if (strategy === "retest") {
      entry.retest_min_move_pct = Number(filters.retest_min_move_pct);
      entry.retest_tolerance_pct = Number(filters.retest_tolerance_pct);
      entry.retest_pullback_vol_max = Number(filters.retest_pullback_vol_max);
    }
    return entry;
  });
}

// Etiqueta corta para pestañas de resultado
function tabLabel(key) {
  // "ADA/USDT · breakout" → "ADA · Break"  |  "ADA/USDT" → "ADA"
  const parts = key.split(" · ");
  const base = parts[0].split("/")[0];
  const stMap = { breakout: "Break", retest: "Retest", bounce: "Bounce" };
  return parts.length > 1 ? `${base} · ${stMap[parts[1]] ?? parts[1]}` : base;
}

async function loadSymbols() {
  const [symRes, cfgRes] = await Promise.all([
    fetch("/api/lab/symbols"),
    fetch("/api/config"),
  ]);
  const symData = await symRes.json();
  botConfig.value = await cfgRes.json();
  availableSymbols.value = symData.available ?? [];
  // Pre-seleccionar los símbolos activos del bot con sus filtros configurados
  for (const sym of symData.active_symbols ?? []) {
    const sp = botConfig.value?.symbol_params?.[sym] ?? {};
    // Añadir una entrada por cada estrategia configurada en el símbolo
    const strats = sp.strategies ?? ["breakout"];
    for (const strat of strats) {
      if (
        selectedSymbols.value.find(
          (s) => s.symbol === sym && s.strategy === strat,
        )
      )
        continue;
      selectedSymbols.value.push({
        id: _nextId++,
        symbol: sym,
        strategy: strat,
        filters: _defaultFilters(sp, strat),
      });
    }
  }
}
loadSymbols();

// ── Job ───────────────────────────────────────────────────────────────────────
const jobId = ref(null);
const jobStatus = ref(null); // pending | running | done | error | cancelled
const progress = ref([]);
const result = ref(null);
const errorMsg = ref(null);
const actualYears = ref(null);
const capped = ref(false);
let pollTimer = null;

const JOB_STORAGE_KEY = "lab_active_job";

// Recuperar job en curso si el usuario refrescó la página mientras simulaba
onMounted(async () => {
  const saved = localStorage.getItem(JOB_STORAGE_KEY);
  if (!saved) return;
  try {
    const { id } = JSON.parse(saved);
    const res = await fetch(`/api/lab/jobs/${id}`);
    if (!res.ok) {
      localStorage.removeItem(JOB_STORAGE_KEY);
      return;
    }
    const data = await res.json();
    jobId.value = id;
    jobStatus.value = data.status;
    progress.value = data.progress ?? [];
    if (data.status === "done") {
      result.value = data.result;
      localStorage.removeItem(JOB_STORAGE_KEY);
    } else if (data.status === "error" || data.status === "cancelled") {
      errorMsg.value = data.error ?? "Simulación cancelada.";
      localStorage.removeItem(JOB_STORAGE_KEY);
    } else {
      // Todavía corriendo — retomar el polling
      startPolling();
    }
  } catch {
    localStorage.removeItem(JOB_STORAGE_KEY);
  }
});
const activeResultTab = ref("general");
const selectedTrade = ref(null);
const tradeFilter = ref("con"); // 'con' | 'sin'

// ── Modal de cancelación ──────────────────────────────────────────────────────
const showCancelModal = ref(false);

async function simulate() {
  if (!selectedSymbols.value.length) return;
  errorMsg.value = null;
  result.value = null;
  progress.value = [];
  jobStatus.value = "pending";
  const stratEntries = buildStrategyEntries();
  const uniqueSyms = [...new Set(stratEntries.map((e) => e.symbol))];
  const res = await fetch("/api/lab/simulate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      symbols: uniqueSyms,
      capital: capital.value,
      max_positions: maxPositions.value,
      years: periodMode.value === "years" ? years.value : 1,
      leverage: leverage.value,
      strategy_entries: stratEntries,
      date_from: periodMode.value === "range" ? dateFrom.value || null : null,
      date_to: periodMode.value === "range" ? dateTo.value || null : null,
    }),
  });
  const data = await res.json();
  jobId.value = data.job_id;
  actualYears.value = data.actual_years;
  capped.value = data.capped ?? false;
  // Persistir jobId para sobrevivir un refresco de página
  localStorage.setItem(
    JOB_STORAGE_KEY,
    JSON.stringify({ id: data.job_id, syms: selected.value }),
  );
  startPolling();
}

async function confirmCancel() {
  showCancelModal.value = false;
  if (!jobId.value) return;
  await fetch(`/api/lab/jobs/${jobId.value}/cancel`, { method: "POST" });
  stopPolling();
  jobStatus.value = "cancelled";
  progress.value = []; // limpiar log para que no se muestre el spinner
  errorMsg.value = "Simulación cancelada por el usuario.";
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(pollJob, 2000);
}
function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function pollJob() {
  if (!jobId.value) return;
  try {
    const res = await fetch(`/api/lab/jobs/${jobId.value}`);
    if (!res.ok) {
      // 404 = job expirado; otro código = error de servidor
      errorMsg.value =
        res.status === 404
          ? "El job ha expirado (simulación muy larga o reinicio del servidor). Vuelve a simular."
          : `Error del servidor (HTTP ${res.status}).`;
      stopPolling();
      jobStatus.value = "error";
      progress.value = [];
      return;
    }
    const data = await res.json();
    jobStatus.value = data.status;
    progress.value = data.progress ?? [];
    if (data.status === "done") {
      result.value = data.result;
      stopPolling();
      localStorage.removeItem(JOB_STORAGE_KEY);
      emit("simulationDone");
    } else if (data.status === "error" || data.status === "cancelled") {
      errorMsg.value = data.error ?? "Simulación cancelada.";
      progress.value = [];
      stopPolling();
      localStorage.removeItem(JOB_STORAGE_KEY);
    }
  } catch (e) {
    // Error de red o de parseo de JSON (respuesta muy grande, timeout, etc.)
    console.error("Poll error:", e);
    errorMsg.value = `Error de conexión durante la simulación: ${e.message}. Verifica la consola del servidor.`;
    stopPolling();
    jobStatus.value = "error";
    progress.value = [];
  }
}

onUnmounted(stopPolling);

const isRunning = computed(() =>
  ["pending", "running"].includes(jobStatus.value),
);

function fmtPct(v) {
  return v != null ? (v >= 0 ? "+" : "") + Number(v).toFixed(1) + "%" : "—";
}
function fmtUsd(v) {
  return v != null ? "$" + Number(v).toFixed(2) : "—";
}
function pc(v) {
  return Number(v) >= 0 ? "positive" : "negative";
}

// ── Helpers para vista de símbolo ─────────────────────────────────────────────
function tradesForTab(which) {
  if (!result.value || activeResultTab.value === "general") return [];
  const src =
    which === "sin" ? result.value.sin_filtros : result.value.con_filtros;
  return src?.trades_por_simbolo?.[activeResultTab.value] ?? [];
}
function symSin(sym) {
  return result.value?.sin_filtros?.por_simbolo?.find((s) => s.symbol === sym);
}
function symCon(sym) {
  return result.value?.con_filtros?.por_simbolo?.find((s) => s.symbol === sym);
}

const FILTER_NAMES = {
  momentum_q3_block: "F1 Momentum",
  session_block_hours: "F2b Sesión UTC",
  usdt_norm_block_range: "F3 Vol USDT",
  rsi_overbought_block: "F4 RSI14",
  failed_retest_filter: "Failed retest",
  volume_trigger_ratio_max: "Vol máximo",
};
function activeFilters(sym) {
  const sp = result.value?.params?.symbol_params?.[sym];
  if (!sp) return [];
  return Object.keys(sp)
    .filter((k) => sp[k] != null && sp[k] !== false)
    .map((k) => FILTER_NAMES[k] ?? k);
}
function fmtDate(s) {
  if (!s) return "—";
  return String(s).slice(0, 16).replace("T", " ");
}

// Parsea los mensajes estructurados del backend en objetos para renderizar
const parsedProgress = computed(() => {
  let currentPass = 0;
  return progress.value.map((raw) => {
    if (raw.startsWith("PASS:")) {
      currentPass = parseInt(raw.split(":")[1]);
      return { type: "pass", pass: currentPass };
    }
    if (raw.startsWith("SYM:")) {
      const [, idx, total, sym] = raw.split(":");
      return { type: "sym_start", idx: +idx, total: +total, sym };
    }
    if (raw.startsWith("SYMDONE:")) {
      const [, sym, trades, wr, longs, shorts] = raw.split(":");
      return {
        type: "sym_done",
        sym,
        trades: +trades,
        wr: +wr,
        longs: +longs,
        shorts: +shorts,
      };
    }
    if (raw === "DONE") return { type: "done" };
    if (raw === "FILTERPASS") return { type: "filterpass" };
    if (raw.startsWith("FILTERANAL:")) {
      return { type: "filteranal", sym: raw.split(":")[1] };
    }
    return { type: "info", text: raw };
  });
});

const currentSymbol = computed(() => {
  const starts = parsedProgress.value.filter((p) => p.type === "sym_start");
  const dones = parsedProgress.value
    .filter((p) => p.type === "sym_done")
    .map((p) => p.sym);
  const pending = starts.find((p) => !dones.includes(p.sym));
  return pending ?? null;
});

// ── Análisis de filtros ───────────────────────────────────────────────────────
// undefined = resultado antiguo sin filter_analysis; objeto = datos disponibles
function filterAnalysis(sym) {
  if (!result.value) return undefined;
  if (!("filter_analysis" in result.value)) return undefined;
  return result.value.filter_analysis[sym] ?? undefined;
}
function filterAnalysisRows(sym) {
  const fa = filterAnalysis(sym);
  if (!fa) return [];
  return Object.entries(fa)
    .filter(([k]) => k !== "baseline")
    .map(([name, data]) => ({ name, ...data }));
}
</script>

<template>
  <div class="lab">
    <h2>Laboratorio de simulación</h2>
    <p class="subtitle">
      Configura los parámetros y simula la cartera. Los resultados comparan
      <strong>con y sin filtros F1-F4</strong>.
    </p>

    <!-- ── Formulario ──────────────────────────────────────────────────────── -->
    <div class="form-grid">
      <!-- Selector de símbolo -->
      <div class="field field-full">
        <label
          >Añadir entrada
          <span class="hint">(símbolo + estrategia)</span></label
        >
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
                    !selectedSymbols.find(
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
              selectedSymbols.length >= 16 ||
              !!selectedSymbols.find(
                (s) => s.symbol === dropdownSym && s.strategy === dropdownStrat,
              )
            "
            @click="addSymbol"
          >
            + Añadir
          </button>
        </div>
        <div v-if="!selectedSymbols.length" class="field-warn">
          Añade al menos una criptomoneda.
        </div>
      </div>

      <!-- Tarjetas por (símbolo + estrategia) -->
      <div v-if="selectedSymbols.length" class="field field-full sym-cards">
        <div
          v-for="item in selectedSymbols"
          :key="item.id"
          class="sym-filter-card"
        >
          <div class="sfc-header">
            <span class="sfc-name">{{ item.symbol }}</span>
            <span class="sfc-strat-badge" :class="`strat-${item.strategy}`">{{
              item.strategy
            }}</span>
            <button class="sfc-remove" @click="removeEntry(item.id)">✕</button>
          </div>
          <div class="sfc-filters">
            <!-- ── Breakout: filtros F1-F4 ── -->
            <template v-if="item.strategy === 'breakout'">
              <div class="sfc-section-label">Filtros de calidad</div>
              <label class="sfc-row">
                <input type="checkbox" v-model="item.filters.f1.enabled" />
                <span class="sfc-fname">F1 Momentum</span>
                <template v-if="item.filters.f1.enabled">
                  <input
                    class="sfc-num"
                    type="number"
                    v-model.number="item.filters.f1.lo"
                    step="0.1"
                  />
                  <span class="sfc-sep">–</span>
                  <input
                    class="sfc-num"
                    type="number"
                    v-model.number="item.filters.f1.hi"
                    step="0.1"
                  />
                  <span class="sfc-unit">%</span>
                </template>
                <span v-else class="sfc-hint"
                  >bloquea zona trampa de momentum 5 velas</span
                >
              </label>
              <label class="sfc-row">
                <input type="checkbox" v-model="item.filters.f2b.enabled" />
                <span class="sfc-fname">F2b Sesión UTC</span>
                <template v-if="item.filters.f2b.enabled">
                  <input
                    class="sfc-num"
                    type="number"
                    v-model.number="item.filters.f2b.lo"
                    min="0"
                    max="23"
                  />
                  <span class="sfc-sep">–</span>
                  <input
                    class="sfc-num"
                    type="number"
                    v-model.number="item.filters.f2b.hi"
                    min="1"
                    max="24"
                  />
                  <span class="sfc-unit">h UTC</span>
                </template>
                <span v-else class="sfc-hint"
                  >bloquea franja horaria con WR bajo</span
                >
              </label>
              <label class="sfc-row">
                <input type="checkbox" v-model="item.filters.f3.enabled" />
                <span class="sfc-fname">F3 Vol USDT</span>
                <template v-if="item.filters.f3.enabled">
                  <input
                    class="sfc-num"
                    type="number"
                    v-model.number="item.filters.f3.lo"
                    step="0.1"
                  />
                  <span class="sfc-sep">–</span>
                  <input
                    class="sfc-num"
                    type="number"
                    v-model.number="item.filters.f3.hi"
                    step="0.1"
                  />
                  <span class="sfc-unit">×</span>
                </template>
                <span v-else class="sfc-hint"
                  >bloquea zona trampa de vol USDT normalizado</span
                >
              </label>
              <label class="sfc-row">
                <input type="checkbox" v-model="item.filters.f4.enabled" />
                <span class="sfc-fname">F4 RSI14 ≥</span>
                <template v-if="item.filters.f4.enabled">
                  <input
                    class="sfc-num"
                    type="number"
                    v-model.number="item.filters.f4.threshold"
                    min="50"
                    max="100"
                  />
                  <span class="sfc-unit">(sobrecompra)</span>
                </template>
                <span v-else class="sfc-hint"
                  >bloquea entradas con RSI en sobrecompra</span
                >
              </label>
              <label class="sfc-row">
                <input type="checkbox" v-model="item.filters.fr.enabled" />
                <span class="sfc-fname">Failed retest (auto)</span>
                <span class="sfc-hint">anti-fakeout adaptativo</span>
              </label>
              <label class="sfc-row">
                <input type="checkbox" v-model="item.filters.volmax.enabled" />
                <span class="sfc-fname">Vol máximo ≤</span>
                <template v-if="item.filters.volmax.enabled">
                  <input
                    class="sfc-num"
                    type="number"
                    v-model.number="item.filters.volmax.max"
                    step="0.1"
                  />
                  <span class="sfc-unit">×</span>
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
                <span class="sfc-fname retest-label">Mov. mínimo</span>
                <input
                  class="sfc-num"
                  type="number"
                  step="0.1"
                  v-model.number="item.filters.retest_min_move_pct"
                />
                <span class="sfc-unit">% (distancia mínima al nivel)</span>
              </div>
              <div class="retest-param">
                <span class="sfc-fname retest-label">Tolerancia</span>
                <input
                  class="sfc-num"
                  type="number"
                  step="0.05"
                  v-model.number="item.filters.retest_tolerance_pct"
                />
                <span class="sfc-unit">% (proximidad al nivel)</span>
              </div>
              <div class="retest-param">
                <span class="sfc-fname retest-label">Vol. pullback ≤</span>
                <input
                  class="sfc-num"
                  type="number"
                  step="0.1"
                  v-model.number="item.filters.retest_pullback_vol_max"
                />
                <span class="sfc-unit">× (pullback silencioso)</span>
              </div>
            </template>

            <!-- ── Bounce: sin parámetros adicionales ── -->
            <template v-else-if="item.strategy === 'bounce'">
              <div class="sfc-hint" style="padding: 0.4rem 0">
                Sin parámetros configurables — usa el midpoint del rango mensual
                como TP.
              </div>
            </template>
          </div>
        </div>
      </div>

      <!-- Parámetros globales -->
      <div class="field">
        <label>Capital inicial ($)</label>
        <input v-model.number="capital" type="number" min="1" step="10" />
      </div>
      <div class="field">
        <label>Posiciones simultáneas</label>
        <input v-model.number="maxPositions" type="number" min="1" max="10" />
        <span class="hint">{{
          maxPositions > 1
            ? (100 / maxPositions).toFixed(1) + "% por trade"
            : "100% por trade"
        }}</span>
      </div>
      <!-- Período: toggle años / rango de fechas -->
      <div class="field field-full">
        <label>Período a simular</label>
        <div class="period-toggle">
          <button
            class="ptbtn"
            :class="{ active: periodMode === 'years' }"
            @click="periodMode = 'years'"
          >
            Nº de años
          </button>
          <button
            class="ptbtn"
            :class="{ active: periodMode === 'range' }"
            @click="periodMode = 'range'"
          >
            Rango de fechas
          </button>
        </div>
      </div>
      <div v-if="periodMode === 'years'" class="field">
        <label>Años a simular</label>
        <input v-model.number="years" type="number" min="1" max="15" />
      </div>
      <template v-else>
        <div class="field">
          <label>Desde</label>
          <input v-model="dateFrom" type="date" class="date-input" />
        </div>
        <div class="field">
          <label>Hasta</label>
          <input v-model="dateTo" type="date" class="date-input" />
        </div>
      </template>
      <div class="field">
        <label>Apalancamiento (×)</label>
        <input v-model.number="leverage" type="number" min="1" max="10" />
      </div>

      <div class="field field-full btn-row">
        <button
          class="btn-simulate"
          :disabled="isRunning || !selectedSymbols.length"
          @click="simulate"
        >
          {{ isRunning ? "Simulando…" : "▶ Simular" }}
        </button>
        <button
          v-if="isRunning"
          class="btn-cancel"
          @click="showCancelModal = true"
        >
          ✕ Cancelar
        </button>
      </div>
    </div>

    <!-- ── Aviso años recortados ───────────────────────────────────────────── -->
    <div v-if="capped && actualYears" class="info-banner">
      ℹ️ Datos disponibles para los símbolos seleccionados:
      <strong>{{ actualYears }} años</strong> (pediste {{ years }}). Simulando
      con lo disponible.
    </div>

    <!-- ── Progreso visual ────────────────────────────────────────────────── -->
    <div v-if="isRunning || (progress.length && !result)" class="prog-box">
      <!-- Cabecera con estado actual -->
      <div class="prog-header">
        <span class="spinner" />
        <span v-if="currentSymbol">
          Analizando <strong>{{ currentSymbol.sym }}</strong> ({{
            currentSymbol.idx
          }}
          de {{ currentSymbol.total }})
        </span>
        <span v-else>Preparando análisis…</span>
      </div>

      <div v-for="(item, i) in parsedProgress" :key="i">
        <!-- Cabecera de pasada -->
        <div v-if="item.type === 'pass'" class="prog-pass">
          <span class="prog-pass-badge">{{ item.pass }}/2</span>
          {{
            item.pass === 1
              ? "Análisis base sin filtros"
              : "Análisis con filtros F1-F4"
          }}
        </div>

        <!-- Análisis de filtros -->
        <div v-else-if="item.type === 'filterpass'" class="prog-pass">
          <span class="prog-pass-badge" style="background: var(--green)"
            >3/3</span
          >
          Impacto individual de cada filtro por criptomoneda
        </div>
        <div
          v-else-if="item.type === 'filteranal'"
          class="prog-sym prog-sym-active"
        >
          <span class="spinner-sm" /> Analizando filtros de {{ item.sym }}…
        </div>

        <!-- Símbolo en curso (solo el actual) -->
        <div
          v-else-if="
            item.type === 'sym_start' && currentSymbol?.sym === item.sym
          "
          class="prog-sym prog-sym-active"
        >
          <span class="spinner-sm" />
          <span>{{ item.sym }}</span>
          <span class="prog-sym-sub">descargando datos históricos…</span>
        </div>

        <!-- Símbolo completado -->
        <div
          v-else-if="item.type === 'sym_done'"
          class="prog-sym prog-sym-done"
        >
          <span class="prog-check">✓</span>
          <span>{{ item.sym }}</span>
          <span class="prog-sym-stats">
            {{ item.trades }} operaciones
            <span class="prog-wr" :class="item.wr >= 30 ? 'wr-ok' : 'wr-low'">
              · WR {{ item.wr }}%
            </span>
            <span class="prog-dir">· {{ item.longs }}↑ {{ item.shorts }}↓</span>
          </span>
        </div>

        <!-- Mensajes informativos -->
        <div v-else-if="item.type === 'info' && item.text" class="prog-info">
          {{ item.text }}
        </div>
      </div>
    </div>

    <!-- ── Error ─────────────────────────────────────────────────────────── -->
    <div v-if="errorMsg && !isRunning" class="error-box">{{ errorMsg }}</div>

    <!-- ── Resultados ─────────────────────────────────────────────────────── -->
    <template v-if="result">
      <div class="result-params">
        Simulación: <strong>{{ result.params.symbols.join(", ") }}</strong> ·
        ${{ result.params.capital }} ·
        {{ result.params.max_positions }} posiciones ·
        {{ result.params.leverage }}× · {{ result.params.actual_years }} años
      </div>

      <!-- Pestañas General + una por criptomoneda -->
      <div class="result-tabs">
        <button
          class="rtab"
          :class="{ active: activeResultTab === 'general' }"
          @click="activeResultTab = 'general'"
        >
          General
        </button>
        <button
          v-for="sym in result.params.symbols"
          :key="sym"
          class="rtab"
          :class="{ active: activeResultTab === sym }"
          @click="
            activeResultTab = sym;
            tradeFilter = 'con';
          "
        >
          {{ tabLabel(sym) }}
        </button>
      </div>

      <!-- ── Vista General ── -->
      <div v-show="activeResultTab === 'general'" class="compare-grid">
        <div class="sim-block">
          <div class="sim-title">
            Sin filtros F1-F4 <span class="badge-base">baseline</span>
          </div>
          <div v-if="result.sin_filtros?.error" class="sim-error">
            {{ result.sin_filtros.error }}
          </div>
          <template v-else>
            <div class="kpi-row">
              <div class="kpi">
                <div class="kpi-label">Capital final</div>
                <div class="kpi-val" :class="pc(result.sin_filtros.pnl_pct)">
                  {{ fmtUsd(result.sin_filtros.capital_final) }}
                </div>
                <div class="kpi-sub" :class="pc(result.sin_filtros.pnl_pct)">
                  {{ fmtPct(result.sin_filtros.pnl_pct) }}
                </div>
              </div>
              <div class="kpi">
                <div class="kpi-label">Win Rate</div>
                <div class="kpi-val">
                  {{ result.sin_filtros.win_rate?.toFixed(1) }}%
                </div>
                <div class="kpi-sub">
                  {{ result.sin_filtros.wins }}W /
                  {{ result.sin_filtros.losses }}L
                </div>
              </div>
              <div class="kpi">
                <div class="kpi-label">Max Drawdown</div>
                <div class="kpi-val negative">
                  {{ result.sin_filtros.max_drawdown_pct?.toFixed(1) }}%
                </div>
                <div class="kpi-sub">
                  Mín. {{ fmtUsd(result.sin_filtros.min_equity) }}
                </div>
              </div>
              <div class="kpi">
                <div class="kpi-label">Trades / Señales</div>
                <div class="kpi-val">{{ result.sin_filtros.total_trades }}</div>
                <div class="kpi-sub">
                  de {{ result.sin_filtros.total_signals }} señales
                </div>
              </div>
            </div>
            <div class="section-label">Por criptomoneda</div>
            <table class="mini-table">
              <thead>
                <tr>
                  <th>Criptomoneda</th>
                  <th>Operaciones</th>
                  <th>Win Rate</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="s in result.sin_filtros.por_simbolo" :key="s.symbol">
                  <td>{{ s.symbol }}</td>
                  <td>{{ s.trades }}</td>
                  <td :class="s.win_rate >= 30 ? 'positive' : 'negative'">
                    {{ s.win_rate }}%
                  </td>
                </tr>
              </tbody>
            </table>
            <div class="section-label">Año a año</div>
            <table class="mini-table">
              <thead>
                <tr>
                  <th>Año</th>
                  <th>Capital</th>
                  <th>PnL acum.</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in result.sin_filtros.por_anio" :key="row.year">
                  <td>{{ row.year }}</td>
                  <td>{{ fmtUsd(row.capital) }}</td>
                  <td :class="pc(row.pnl_pct)">{{ fmtPct(row.pnl_pct) }}</td>
                </tr>
              </tbody>
            </table>
            <div class="section-label">Volumen</div>
            <table class="mini-table">
              <thead>
                <tr>
                  <th>Bucket</th>
                  <th>Trades</th>
                  <th>WR</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="b in result.sin_filtros.analisis_volumen"
                  :key="b.bucket"
                >
                  <td>{{ b.bucket }}</td>
                  <td>{{ b.trades }}</td>
                  <td :class="b.win_rate >= 30 ? 'positive' : 'negative'">
                    {{ b.win_rate }}%
                  </td>
                </tr>
              </tbody>
            </table>
            <div class="section-label">Long vs Short</div>
            <table class="mini-table">
              <thead>
                <tr>
                  <th>Dirección</th>
                  <th>Trades</th>
                  <th>WR</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="d in result.sin_filtros.analisis_direccion"
                  :key="d.direction"
                >
                  <td>
                    <span
                      :class="d.direction === 'long' ? 'dir-long' : 'dir-short'"
                      >{{ d.direction.toUpperCase() }}</span
                    >
                  </td>
                  <td>{{ d.trades }}</td>
                  <td :class="d.win_rate >= 30 ? 'positive' : 'negative'">
                    {{ d.win_rate }}%
                  </td>
                </tr>
              </tbody>
            </table>
          </template>
        </div>

        <div class="sim-block highlighted">
          <div class="sim-title">
            Con filtros F1-F4 <span class="badge-filters">activos</span>
          </div>
          <div v-if="result.con_filtros?.error" class="sim-error">
            {{ result.con_filtros.error }}
          </div>
          <template v-else>
            <div class="kpi-row">
              <div class="kpi">
                <div class="kpi-label">Capital final</div>
                <div class="kpi-val" :class="pc(result.con_filtros.pnl_pct)">
                  {{ fmtUsd(result.con_filtros.capital_final) }}
                </div>
                <div class="kpi-sub" :class="pc(result.con_filtros.pnl_pct)">
                  {{ fmtPct(result.con_filtros.pnl_pct) }}
                </div>
              </div>
              <div class="kpi">
                <div class="kpi-label">Win Rate</div>
                <div class="kpi-val">
                  {{ result.con_filtros.win_rate?.toFixed(1) }}%
                </div>
                <div class="kpi-sub">
                  {{ result.con_filtros.wins }}W /
                  {{ result.con_filtros.losses }}L
                </div>
              </div>
              <div class="kpi">
                <div class="kpi-label">Max Drawdown</div>
                <div class="kpi-val negative">
                  {{ result.con_filtros.max_drawdown_pct?.toFixed(1) }}%
                </div>
                <div class="kpi-sub">
                  Mín. {{ fmtUsd(result.con_filtros.min_equity) }}
                </div>
              </div>
              <div class="kpi">
                <div class="kpi-label">Trades / Señales</div>
                <div class="kpi-val">{{ result.con_filtros.total_trades }}</div>
                <div class="kpi-sub">
                  de {{ result.con_filtros.total_signals }} señales
                </div>
              </div>
            </div>
            <div class="section-label">Por criptomoneda</div>
            <table class="mini-table">
              <thead>
                <tr>
                  <th>Criptomoneda</th>
                  <th>Operaciones</th>
                  <th>Win Rate</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="s in result.con_filtros.por_simbolo" :key="s.symbol">
                  <td>{{ s.symbol }}</td>
                  <td>{{ s.trades }}</td>
                  <td :class="s.win_rate >= 30 ? 'positive' : 'negative'">
                    {{ s.win_rate }}%
                  </td>
                </tr>
              </tbody>
            </table>
            <div class="section-label">Año a año</div>
            <table class="mini-table">
              <thead>
                <tr>
                  <th>Año</th>
                  <th>Capital</th>
                  <th>PnL acum.</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in result.con_filtros.por_anio" :key="row.year">
                  <td>{{ row.year }}</td>
                  <td>{{ fmtUsd(row.capital) }}</td>
                  <td :class="pc(row.pnl_pct)">{{ fmtPct(row.pnl_pct) }}</td>
                </tr>
              </tbody>
            </table>
            <div class="section-label">Volumen</div>
            <table class="mini-table">
              <thead>
                <tr>
                  <th>Bucket</th>
                  <th>Trades</th>
                  <th>WR</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="b in result.con_filtros.analisis_volumen"
                  :key="b.bucket"
                >
                  <td>{{ b.bucket }}</td>
                  <td>{{ b.trades }}</td>
                  <td :class="b.win_rate >= 30 ? 'positive' : 'negative'">
                    {{ b.win_rate }}%
                  </td>
                </tr>
              </tbody>
            </table>
            <div class="section-label">Long vs Short</div>
            <table class="mini-table">
              <thead>
                <tr>
                  <th>Dirección</th>
                  <th>Trades</th>
                  <th>WR</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="d in result.con_filtros.analisis_direccion"
                  :key="d.direction"
                >
                  <td>
                    <span
                      :class="d.direction === 'long' ? 'dir-long' : 'dir-short'"
                      >{{ d.direction.toUpperCase() }}</span
                    >
                  </td>
                  <td>{{ d.trades }}</td>
                  <td :class="d.win_rate >= 30 ? 'positive' : 'negative'">
                    {{ d.win_rate }}%
                  </td>
                </tr>
              </tbody>
            </table>
          </template>
        </div>
      </div>
      <!-- /compare-grid -->

      <!-- ── Vista por símbolo ── -->
      <template v-if="activeResultTab !== 'general'">
        <!-- Filtros activos -->
        <div class="sym-filters-row">
          <span class="sym-filters-label">Filtros activos:</span>
          <span v-if="!activeFilters(activeResultTab).length" class="tag-none"
            >Ninguno</span
          >
          <span
            v-for="f in activeFilters(activeResultTab)"
            :key="f"
            class="tag-filter"
            >{{ f }}</span
          >
        </div>

        <!-- Comparativa sin vs con para este símbolo -->
        <div class="sym-compare">
          <div class="sym-compare-block">
            <div class="sym-compare-title">Sin filtros</div>
            <template v-if="symSin(activeResultTab)">
              <div class="sym-kpi-row">
                <div class="sym-kpi">
                  <span class="sym-kpi-label">Trades</span>
                  {{ symSin(activeResultTab).trades }}
                </div>
                <div class="sym-kpi">
                  <span class="sym-kpi-label">WR</span>
                  {{ symSin(activeResultTab).win_rate }}%
                </div>
              </div>
            </template>
          </div>
          <div class="sym-compare-arrow">→</div>
          <div class="sym-compare-block highlighted-light">
            <div class="sym-compare-title">Con filtros</div>
            <template v-if="symCon(activeResultTab) && symSin(activeResultTab)">
              <div class="sym-kpi-row">
                <div class="sym-kpi">
                  <span class="sym-kpi-label">Trades</span>
                  {{ symCon(activeResultTab).trades }}
                  <span
                    class="delta"
                    :class="
                      symCon(activeResultTab).trades <=
                      symSin(activeResultTab).trades
                        ? 'negative'
                        : 'positive'
                    "
                  >
                    ({{
                      symCon(activeResultTab).trades -
                        symSin(activeResultTab).trades >=
                      0
                        ? "+"
                        : ""
                    }}{{
                      symCon(activeResultTab).trades -
                      symSin(activeResultTab).trades
                    }})
                  </span>
                </div>
                <div class="sym-kpi">
                  <span class="sym-kpi-label">WR</span>
                  {{ symCon(activeResultTab).win_rate }}%
                  <span
                    class="delta"
                    :class="
                      symCon(activeResultTab).win_rate -
                        symSin(activeResultTab).win_rate >=
                      0
                        ? 'positive'
                        : 'negative'
                    "
                  >
                    ({{
                      symCon(activeResultTab).win_rate -
                        symSin(activeResultTab).win_rate >=
                      0
                        ? "+"
                        : ""
                    }}{{
                      (
                        symCon(activeResultTab).win_rate -
                        symSin(activeResultTab).win_rate
                      ).toFixed(1)
                    }}%)
                  </span>
                </div>
              </div>
            </template>
          </div>
        </div>

        <!-- Tabla de impacto por filtro -->
        <template v-if="filterAnalysis(activeResultTab)">
          <div class="section-label">
            Impacto de cada filtro
            <span class="fa-legend">
              <span class="fa-badge-on">✓ activo</span> = ya configurado en
              config.yaml · <span class="fa-badge-off">◌ test</span> = umbral
              estándar sin configurar
            </span>
          </div>
          <div class="filter-analysis-wrap">
            <table class="mini-table">
              <thead>
                <tr>
                  <th>Filtro</th>
                  <th>Estado</th>
                  <th>Operaciones</th>
                  <th>Win Rate</th>
                  <th>Δ WR vs base</th>
                  <th>Trades filtrados</th>
                </tr>
              </thead>
              <tbody>
                <tr class="row-baseline">
                  <td><strong>Sin filtros (base)</strong></td>
                  <td>—</td>
                  <td>{{ filterAnalysis(activeResultTab).baseline.trades }}</td>
                  <td>
                    {{ filterAnalysis(activeResultTab).baseline.win_rate }}%
                  </td>
                  <td>—</td>
                  <td>—</td>
                </tr>
                <tr
                  v-for="row in filterAnalysisRows(activeResultTab)"
                  :key="row.name"
                  :class="
                    row.wr_delta > 0
                      ? 'row-beneficial'
                      : row.wr_delta < 0
                        ? 'row-harmful'
                        : ''
                  "
                >
                  <td>{{ row.name }}</td>
                  <td>
                    <span v-if="row.configured" class="fa-badge-on"
                      >✓ activo</span
                    >
                    <span v-else class="fa-badge-off">◌ test</span>
                  </td>
                  <td>{{ row.trades }}</td>
                  <td :class="row.wr_delta >= 0 ? 'positive' : 'negative'">
                    {{ row.win_rate }}%
                  </td>
                  <td>
                    <span
                      class="fa-delta"
                      :class="row.wr_delta >= 0 ? 'positive' : 'negative'"
                    >
                      {{ row.wr_delta >= 0 ? "+" : "" }}{{ row.wr_delta }}%
                    </span>
                  </td>
                  <td class="fa-filtered">-{{ row.trades_filtered }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="fa-hint">
            Δ WR &gt; 0 = el filtro mejora el Win Rate eliminando entradas
            malas. Añade los filtros beneficiosos en la pestaña
            <strong>Configuración</strong>.
          </div>
        </template>

        <!-- Resultado antiguo: necesita re-simulación -->
        <div v-else class="fa-notice">
          ↻ Vuelve a simular para ver el análisis de filtros. La pasada de
          análisis se ejecuta automáticamente al finalizar cada simulación.
        </div>

        <!-- Toggle sin/con y tabla de trades -->
        <div class="trade-table-header">
          <div class="section-label" style="margin: 0">Trades ejecutados</div>
          <div class="toggle-row">
            <button
              class="toggle-btn"
              :class="{ active: tradeFilter === 'sin' }"
              @click="tradeFilter = 'sin'"
            >
              Sin filtros
            </button>
            <button
              class="toggle-btn"
              :class="{ active: tradeFilter === 'con' }"
              @click="tradeFilter = 'con'"
            >
              Con filtros
            </button>
          </div>
        </div>

        <div class="trades-wrap">
          <table class="mini-table trades-table">
            <thead>
              <tr>
                <th>Fecha entrada</th>
                <th>Fecha salida</th>
                <th>Dir.</th>
                <th>Entrada</th>
                <th>Salida</th>
                <th>PnL%</th>
                <th>R</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(t, i) in tradesForTab(tradeFilter)"
                :key="i"
                class="trade-row"
                @click="selectedTrade = { ...t, symbol: activeResultTab }"
              >
                <td class="ts-col">{{ fmtDate(t.entry_ts) }}</td>
                <td class="ts-col">{{ fmtDate(t.exit_ts) }}</td>
                <td>
                  <span
                    :class="t.direction === 'long' ? 'dir-long' : 'dir-short'"
                    >{{ t.direction?.toUpperCase() }}</span
                  >
                </td>
                <td>{{ t.entry_price }}</td>
                <td>{{ t.exit_price }}</td>
                <td :class="t.pnl_pct >= 0 ? 'positive' : 'negative'">
                  {{ fmtPct(t.pnl_pct) }}
                </td>
                <td>
                  <span
                    :class="t.result === 'win' ? 'positive' : 'negative'"
                    style="font-weight: 700"
                    >{{ t.result === "win" ? "✓" : "✗" }}</span
                  >
                </td>
              </tr>
              <tr v-if="!tradesForTab(tradeFilter).length">
                <td
                  colspan="7"
                  style="
                    color: var(--text-muted);
                    text-align: center;
                    padding: 1rem;
                  "
                >
                  Sin trades para este símbolo y filtro
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </template>

    <!-- ── Modal detalle trade ────────────────────────────────────────────── -->
    <Teleport to="body">
      <div
        v-if="selectedTrade"
        class="modal-overlay"
        @click.self="selectedTrade = null"
      >
        <div class="modal">
          <div class="modal-title">
            {{ selectedTrade.symbol }} —
            <span
              :class="
                selectedTrade.direction === 'long' ? 'dir-long' : 'dir-short'
              "
            >
              {{ selectedTrade.direction?.toUpperCase() }}
            </span>
          </div>
          <table class="detail-table">
            <tbody>
              <tr>
                <td>Fecha entrada</td>
                <td>{{ fmtDate(selectedTrade.entry_ts) }} UTC</td>
              </tr>
              <tr>
                <td>Fecha salida</td>
                <td>{{ fmtDate(selectedTrade.exit_ts) }} UTC</td>
              </tr>
              <tr>
                <td>Precio entrada</td>
                <td>
                  <strong>{{ selectedTrade.entry_price }}</strong>
                </td>
              </tr>
              <tr>
                <td>Precio salida</td>
                <td>
                  <strong>{{ selectedTrade.exit_price }}</strong>
                </td>
              </tr>
              <tr>
                <td>Resultado</td>
                <td>
                  <span
                    :class="
                      selectedTrade.result === 'win' ? 'positive' : 'negative'
                    "
                    style="font-weight: 700"
                  >
                    {{ selectedTrade.result === "win" ? "✓ WIN" : "✗ LOSS" }}
                  </span>
                </td>
              </tr>
              <tr>
                <td>PnL</td>
                <td
                  :class="selectedTrade.pnl_pct >= 0 ? 'positive' : 'negative'"
                >
                  <strong>{{ fmtPct(selectedTrade.pnl_pct) }}</strong>
                </td>
              </tr>
              <tr>
                <td>Vol. ratio</td>
                <td>{{ selectedTrade.volume_ratio }}×</td>
              </tr>
            </tbody>
          </table>
          <div class="modal-actions">
            <button class="btn-modal-keep" @click="selectedTrade = null">
              Cerrar
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- ── Modal cancelación ──────────────────────────────────────────────── -->
    <Teleport to="body">
      <div
        v-if="showCancelModal"
        class="modal-overlay"
        @click.self="showCancelModal = false"
      >
        <div class="modal">
          <div class="modal-title">¿Detener la simulación?</div>
          <p class="modal-body">
            La simulación está en curso. Si la cancelas entre símbolos se
            detendrá y perderás los resultados parciales.
          </p>
          <div class="modal-actions">
            <button class="btn-modal-keep" @click="showCancelModal = false">
              Continuar simulando
            </button>
            <button class="btn-modal-stop" @click="confirmCancel">
              Sí, cancelar
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.lab {
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
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

.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 1rem;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.field-full {
  grid-column: 1 / -1;
}
.btn-row {
  flex-direction: row;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
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
.hint {
  font-size: 0.75rem;
  color: var(--text-muted);
}
.field-warn {
  font-size: 0.75rem;
  color: var(--red);
}
.selected-list {
  font-size: 0.75rem;
  color: var(--accent);
}

.sym-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}
.sym-chip {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 0.3rem 0.6rem;
  border-radius: 0.5rem;
  border: 1px solid var(--border);
  background: var(--bg);
  cursor: pointer;
  transition: all 0.15s;
}
.sym-chip:hover {
  border-color: var(--accent);
}
.sym-chip.selected {
  border-color: var(--accent);
  background: rgba(99, 102, 241, 0.12);
}
.chip-sym {
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--text);
}
.chip-since {
  font-size: 0.68rem;
  color: var(--text-muted);
}

.btn-simulate {
  padding: 0.6rem 2rem;
  border-radius: 0.6rem;
  border: none;
  background: var(--accent);
  color: #fff;
  font-weight: 700;
  font-size: 0.9rem;
  cursor: pointer;
}
.btn-simulate:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn-simulate:hover:not(:disabled) {
  opacity: 0.88;
}

.btn-cancel {
  padding: 0.55rem 1.25rem;
  border-radius: 0.6rem;
  border: 1px solid var(--red);
  background: transparent;
  color: var(--red);
  font-weight: 600;
  font-size: 0.85rem;
  cursor: pointer;
}
.btn-cancel:hover {
  background: rgba(239, 68, 68, 0.1);
}

.info-banner {
  background: rgba(99, 102, 241, 0.1);
  border: 1px solid rgba(99, 102, 241, 0.3);
  border-radius: 0.5rem;
  padding: 0.6rem 1rem;
  font-size: 0.82rem;
  color: #a5b4fc;
}
.error-box {
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: 0.5rem;
  padding: 0.75rem 1rem;
  font-size: 0.85rem;
  color: var(--red);
}

.progress-box {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.75rem;
  padding: 0.75rem 1rem;
  font-family: monospace;
  font-size: 0.78rem;
  color: var(--text-muted);
  max-height: 220px;
  overflow-y: auto;
}
.progress-title {
  color: var(--text);
  font-weight: 600;
  margin-bottom: 0.4rem;
  display: flex;
  align-items: center;
  gap: 0.4rem;
}
.progress-line {
  line-height: 1.6;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
.spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  border: 2px solid var(--accent);
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

.result-params {
  font-size: 0.8rem;
  color: var(--text-muted);
  padding-bottom: 0.5rem;
  border-bottom: 1px solid var(--border);
}
.compare-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 1rem;
}

.sim-block {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.75rem;
  padding: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}
.sim-block.highlighted {
  border-color: var(--accent);
}
.sim-title {
  font-weight: 700;
  font-size: 0.9rem;
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.sim-error {
  color: var(--red);
  font-size: 0.82rem;
}

.badge-base {
  font-size: 0.65rem;
  background: rgba(100, 116, 139, 0.2);
  color: var(--text-muted);
  padding: 0.1rem 0.45rem;
  border-radius: 0.3rem;
}
.badge-filters {
  font-size: 0.65rem;
  background: rgba(99, 102, 241, 0.2);
  color: #a5b4fc;
  padding: 0.1rem 0.45rem;
  border-radius: 0.3rem;
}

.kpi-row {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 0.5rem;
}
.kpi {
  background: var(--surface);
  border-radius: 0.5rem;
  padding: 0.5rem 0.75rem;
}
.kpi-label {
  font-size: 0.68rem;
  text-transform: uppercase;
  color: var(--text-muted);
  letter-spacing: 0.04em;
}
.kpi-val {
  font-size: 1.1rem;
  font-weight: 700;
  color: var(--text);
}
.kpi-sub {
  font-size: 0.75rem;
  color: var(--text-muted);
}

.section-label {
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  margin-top: 0.25rem;
}

.mini-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.8rem;
}
.mini-table th {
  text-align: left;
  color: var(--text-muted);
  font-size: 0.7rem;
  padding: 0.25rem 0.4rem;
  border-bottom: 1px solid var(--border);
}
.mini-table td {
  padding: 0.3rem 0.4rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  color: var(--text);
}

.positive {
  color: var(--green);
}
.negative {
  color: var(--red);
}
.dir-long {
  color: var(--green);
  font-weight: 600;
}
.dir-short {
  color: var(--red);
  font-weight: 600;
}

/* Modal */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.65);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}
.modal {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 1rem;
  padding: 1.5rem;
  max-width: 380px;
  width: 90%;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.modal-title {
  font-weight: 700;
  font-size: 1rem;
  color: var(--text);
}
.modal-body {
  font-size: 0.85rem;
  color: var(--text-muted);
  line-height: 1.5;
  margin: 0;
}
.modal-actions {
  display: flex;
  gap: 0.75rem;
  justify-content: flex-end;
}
.btn-modal-keep {
  padding: 0.45rem 1rem;
  border-radius: 0.5rem;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 0.85rem;
}
.btn-modal-stop {
  padding: 0.45rem 1rem;
  border-radius: 0.5rem;
  border: none;
  background: var(--red);
  color: #fff;
  cursor: pointer;
  font-weight: 600;
  font-size: 0.85rem;
}

/* Progreso visual */
.prog-box {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.75rem;
  padding: 1rem 1.25rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.prog-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.88rem;
  font-weight: 600;
  color: var(--text);
  padding-bottom: 0.5rem;
  border-bottom: 1px solid var(--border);
}
.prog-pass {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.82rem;
  font-weight: 700;
  color: var(--text);
  margin-top: 0.25rem;
}
.prog-pass-badge {
  background: var(--accent);
  color: #fff;
  font-size: 0.65rem;
  font-weight: 700;
  padding: 0.1rem 0.45rem;
  border-radius: 0.3rem;
}
.prog-sym {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.82rem;
  padding: 0.25rem 0.5rem;
  border-radius: 0.4rem;
}
.prog-sym-active {
  background: rgba(99, 102, 241, 0.08);
  color: var(--text);
}
.prog-sym-done {
  color: var(--text-muted);
}
.prog-check {
  color: var(--green);
  font-weight: 700;
  flex-shrink: 0;
}
.prog-sym-sub {
  font-size: 0.75rem;
  color: var(--text-muted);
}
.prog-sym-stats {
  font-size: 0.75rem;
  color: var(--text-muted);
  margin-left: auto;
}
.wr-ok {
  color: var(--green);
}
.wr-low {
  color: #eab308;
}
.prog-dir {
  color: var(--text-muted);
}
.prog-info {
  font-size: 0.78rem;
  color: var(--text-muted);
  padding-left: 0.5rem;
}

@keyframes spin-sm {
  to {
    transform: rotate(360deg);
  }
}
.spinner-sm {
  display: inline-block;
  width: 10px;
  height: 10px;
  flex-shrink: 0;
  border: 2px solid var(--accent);
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin-sm 0.8s linear infinite;
}

/* Tabla análisis por filtro */
.filter-analysis-wrap {
  overflow-x: auto;
}
.row-baseline td {
  color: var(--text-muted);
  font-size: 0.78rem;
}
.fa-delta {
  font-weight: 700;
}
.fa-filtered {
  color: var(--text-muted);
}

/* Pestañas de resultados */
.result-tabs {
  display: flex;
  gap: 0.35rem;
  flex-wrap: wrap;
  border-bottom: 1px solid var(--border);
  padding-bottom: 0.5rem;
}
.rtab {
  padding: 0.3rem 0.85rem;
  border-radius: 0.4rem;
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 0.82rem;
  transition: all 0.15s;
}
.rtab:hover {
  border-color: var(--border);
  color: var(--text);
}
.rtab.active {
  background: var(--surface);
  border-color: var(--accent);
  color: var(--text);
  font-weight: 600;
}

/* Vista símbolo */
.sym-filters-row {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  flex-wrap: wrap;
}
.sym-filters-label {
  font-size: 0.75rem;
  color: var(--text-muted);
}
.tag-filter {
  font-size: 0.7rem;
  background: rgba(99, 102, 241, 0.15);
  color: #a5b4fc;
  padding: 0.1rem 0.45rem;
  border-radius: 0.3rem;
}
.tag-none {
  font-size: 0.7rem;
  color: var(--text-muted);
}

.sym-compare {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
}
.sym-compare-block {
  flex: 1;
  min-width: 150px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.75rem;
  padding: 0.75rem 1rem;
}
.sym-compare-block.highlighted-light {
  border-color: var(--accent);
}
.sym-compare-title {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-muted);
  margin-bottom: 0.4rem;
}
.sym-compare-arrow {
  font-size: 1.2rem;
  color: var(--text-muted);
  flex-shrink: 0;
}
.sym-kpi-row {
  display: flex;
  gap: 1rem;
}
.sym-kpi {
  font-size: 0.85rem;
  color: var(--text);
}
.sym-kpi-label {
  font-size: 0.7rem;
  color: var(--text-muted);
  margin-right: 0.25rem;
}
.delta {
  font-size: 0.75rem;
  margin-left: 0.2rem;
}

/* Toggle sin/con */
.trade-table-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.toggle-row {
  display: flex;
  gap: 0.25rem;
}
.toggle-btn {
  padding: 0.2rem 0.7rem;
  border-radius: 0.35rem;
  font-size: 0.75rem;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}
.toggle-btn.active {
  background: var(--surface);
  color: var(--text);
  border-color: var(--accent);
}

/* Tabla de trades */
.trades-wrap {
  overflow-x: auto;
  max-height: 400px;
  overflow-y: auto;
}
.trades-table .trade-row {
  cursor: pointer;
}
.trades-table .trade-row:hover td {
  background: rgba(255, 255, 255, 0.03);
}
.ts-col {
  white-space: nowrap;
  font-size: 0.75rem;
  font-family: monospace;
}

/* Modal detalle trade */
.detail-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}
.detail-table td {
  padding: 0.4rem 0.5rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
}
.detail-table td:first-child {
  color: var(--text-muted);
  width: 42%;
}
.detail-table td:last-child {
  color: var(--text);
  font-weight: 500;
}

/* Tabla análisis por filtro */
.filter-analysis-wrap {
  overflow-x: auto;
}
.fa-legend {
  font-size: 0.68rem;
  font-weight: 400;
  color: var(--text-muted);
  margin-left: 0.5rem;
}
.fa-badge-on {
  font-size: 0.68rem;
  background: rgba(34, 197, 94, 0.15);
  color: var(--green);
  padding: 0.1rem 0.4rem;
  border-radius: 0.25rem;
}
.fa-badge-off {
  font-size: 0.68rem;
  background: rgba(100, 116, 139, 0.15);
  color: var(--text-muted);
  padding: 0.1rem 0.4rem;
  border-radius: 0.25rem;
}
.row-baseline td {
  color: var(--text-muted);
  font-size: 0.78rem;
}
.row-beneficial td {
  background: rgba(34, 197, 94, 0.04);
}
.row-harmful td {
  background: rgba(239, 68, 68, 0.04);
}
.fa-delta {
  font-weight: 700;
}
.fa-filtered {
  color: var(--text-muted);
}
.fa-hint {
  font-size: 0.75rem;
  color: var(--text-muted);
  margin-top: 0.25rem;
  padding: 0.4rem 0.6rem;
  background: var(--bg);
  border-radius: 0.4rem;
  border-left: 2px solid var(--accent);
}
.fa-notice {
  font-size: 0.8rem;
  color: var(--text-muted);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.5rem;
  padding: 0.6rem 0.9rem;
}

/* Selector de símbolo + tarjetas de filtros */
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
  padding: 0.5rem 0.9rem;
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
  padding: 0.5rem 0.9rem;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
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
  min-width: 130px;
}
.sfc-num {
  width: 58px;
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

/* Toggle de período */
.period-toggle {
  display: flex;
  border: 1px solid var(--border);
  border-radius: 0.5rem;
  overflow: hidden;
}
.ptbtn {
  flex: 1;
  padding: 0.35rem 0;
  border: none;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 0.82rem;
  transition: all 0.15s;
}
.ptbtn.active {
  background: var(--accent);
  color: #fff;
  font-weight: 600;
}
.date-input {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0.5rem;
  color: var(--text);
  padding: 0.4rem 0.75rem;
  font-size: 0.88rem;
  width: 100%;
}
.date-input:focus {
  outline: none;
  border-color: var(--accent);
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
</style>
