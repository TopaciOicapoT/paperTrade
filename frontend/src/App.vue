<script setup>
import { ref } from "vue";
import BotStatus from "./components/BotStatus.vue";
import OpenPositions from "./components/OpenPositions.vue";
import LevelsPanel from "./components/LevelsPanel.vue";
import TradeHistory from "./components/TradeHistory.vue";
import LabView from "./components/LabView.vue";
import ConfigEditor from "./components/ConfigEditor.vue";

const tab = ref("dashboard");
const labNotification = ref(false);

function goLab() {
  tab.value = "lab";
  labNotification.value = false;
}

function onSimulationDone() {
  if (tab.value !== "lab") labNotification.value = true;
}
</script>

<template>
  <div class="layout">
    <header class="topbar">
      <div class="logo">📈 PaperTrade</div>
      <nav class="nav">
        <button
          class="tab-btn"
          :class="{ active: tab === 'dashboard' }"
          @click="tab = 'dashboard'"
        >
          Dashboard
        </button>
        <button
          class="tab-btn"
          :class="{ active: tab === 'lab' }"
          @click="goLab"
        >
          Laboratorio
          <span v-if="labNotification" class="notif-dot" />
        </button>
        <button
          class="tab-btn"
          :class="{ active: tab === 'config' }"
          @click="tab = 'config'"
        >
          Configuración
        </button>
        <a href="/docs" target="_blank" rel="noopener" class="nav-link"
          >API Docs</a
        >
      </nav>
    </header>

    <main class="main">
      <!-- v-show en vez de v-if: los componentes no se destruyen al cambiar pestaña -->
      <div v-show="tab === 'dashboard'">
        <section class="panel"><BotStatus /></section>
        <section class="panel"><OpenPositions /></section>
        <section class="panel"><LevelsPanel /></section>
        <section class="panel"><TradeHistory /></section>
      </div>

      <div v-show="tab === 'lab'">
        <section class="panel panel-wide">
          <LabView @simulation-done="onSimulationDone" />
        </section>
      </div>

      <div v-show="tab === 'config'">
        <section class="panel panel-wide"><ConfigEditor /></section>
      </div>
    </main>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.75rem 1.5rem;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  position: sticky;
  top: 0;
  z-index: 10;
}

.logo {
  font-size: 1rem;
  font-weight: 700;
  color: var(--text);
  letter-spacing: -0.02em;
}

.nav {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.tab-btn {
  background: transparent;
  border: 1px solid transparent;
  color: var(--text-muted);
  padding: 0.35rem 0.9rem;
  border-radius: 0.5rem;
  cursor: pointer;
  font-size: 0.85rem;
  transition: all 0.15s;
}
.tab-btn:hover {
  color: var(--text);
  border-color: var(--border);
}
.tab-btn.active {
  color: var(--text);
  background: var(--bg);
  border-color: var(--border);
  font-weight: 600;
}

.notif-dot {
  display: inline-block;
  width: 7px;
  height: 7px;
  background: var(--red);
  border-radius: 50%;
  margin-left: 4px;
  vertical-align: middle;
  box-shadow: 0 0 5px var(--red);
}

.panel-wide {
  max-width: 100%;
}

.nav-link {
  font-size: 0.8rem;
  color: var(--text-muted);
  text-decoration: none;
  margin-left: 0.5rem;
}
.nav-link:hover {
  color: var(--text);
}

.main {
  padding: 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
  max-width: 1400px;
  width: 100%;
  margin: 0 auto;
}

.panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 1rem;
  padding: 1.25rem 1.5rem;
}

@media (max-width: 640px) {
  .main {
    padding: 1rem;
  }
  .panel {
    padding: 1rem;
  }
}
</style>
