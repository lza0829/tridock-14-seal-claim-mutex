<template>
  <div>
    <div class="hero">
      <div>
        <h1>三联对账</h1>
        <p>按供应商+货号+到货日对齐三数量；不一致进差额。架构 B（舱内处置）：quarantine 只能在舱内改命运或丢弃，不支持出舱回灌。未决差额与暂存舱双双清零后才可结案。</p>
      </div>
    </div>
    <div class="panel">
      <div class="row">
        <input v-model="day" placeholder="到货日" />
        <button @click="run">跑三联对账</button>
        <button class="warn" @click="seal">结案封印</button>
        <button class="secondary" @click="load">刷新</button>
        <button class="secondary" @click="tryExport">quarantine 导出（架构 B 不可用）</button>
      </div>
      <p class="msg">{{ err }}</p>
      <p class="okmsg">{{ ok }}</p>
      <p v-if="batch">
        批次 {{ batch.batch_no }} · 对齐 {{ batch.matched_count }} · 差额 {{ batch.variance_count }}
        · 暂存舱 {{ batch.quarantine_count }} · {{ sealedText }}
      </p>
    </div>
    <div class="panel">
      <h2>对齐行（匹配闭包：已排除暂存 held 键）</h2>
      <table>
        <thead><tr><th>供应商</th><th>货号</th><th>预报</th><th>实收</th><th>回执</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="r in matches" :key="r.id">
            <td>{{ r.supplier_code }}</td><td>{{ r.sku }}</td><td>{{ r.expect_qty }}</td><td>{{ r.receive_qty }}</td><td>{{ r.confirm_qty }}</td>
            <td><span class="tag ok">对齐</span></td>
          </tr>
          <tr v-if="!matches.length"><td colspan="6">暂无对齐键</td></tr>
        </tbody>
      </table>
    </div>
    <div class="panel">
      <h2>差额处置</h2>
      <table>
        <thead><tr><th>供应商</th><th>货号</th><th>三数量</th><th>命运</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="r in variances" :key="r.id">
            <td>{{ r.supplier_code }}</td><td>{{ r.sku }}</td>
            <td>{{ r.expect_qty }} / {{ r.receive_qty }} / {{ r.confirm_qty }}</td>
            <td><span class="tag" :class="fateClass(r.fate)">{{ fateText(r.fate) }}</span></td>
            <td>
              <button class="secondary" :disabled="isSealed" @click="fate(r.id, 'accept_short')">认短</button>
              <button class="secondary" :disabled="isSealed" @click="fate(r.id, 'accept_over')">认溢</button>
              <button class="secondary" :disabled="isSealed || r.fate === 'quarantine'" @click="fate(r.id, 'quarantine')">入暂存舱</button>
              <button class="secondary" :disabled="isSealed" @click="fate(r.id, 'discarded')">丢弃</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel">
      <h2>暂存舱（held {{ staging.length }} 条；只读清单，无回灌旁路）</h2>
      <table>
        <thead><tr><th>供应商</th><th>货号</th><th>入舱时间</th><th>原因</th><th>舱内处置</th></tr></thead>
        <tbody>
          <tr v-for="s in staging" :key="s.id">
            <td>{{ s.supplier_code }}</td><td>{{ s.sku }}</td><td>{{ s.created_at }}</td><td>{{ s.reason || '—' }}</td>
            <td>
              <button class="secondary" :disabled="isSealed" @click="fate(s.variance_id, 'accept_short')">舱内认短</button>
              <button class="secondary" :disabled="isSealed" @click="fate(s.variance_id, 'accept_over')">舱内认溢</button>
              <button class="secondary" :disabled="isSealed" @click="fate(s.variance_id, 'pending')">退回待处置</button>
              <button class="warn" :disabled="isSealed" @click="fate(s.variance_id, 'discarded')">舱内丢弃</button>
            </td>
          </tr>
          <tr v-if="!staging.length"><td colspan="5">暂存舱为空</td></tr>
        </tbody>
      </table>
    </div>
    <div class="panel">
      <h2>命运审计</h2>
      <table>
        <thead><tr><th>时间</th><th>供应商</th><th>货号</th><th>原命运</th><th>新命运</th><th>备注</th></tr></thead>
        <tbody>
          <tr v-for="a in auditRows" :key="a.id">
            <td>{{ a.changed_at }}</td><td>{{ a.supplier_code }}</td><td>{{ a.sku }}</td>
            <td>{{ a.from_fate ? fateText(a.from_fate) : '—' }}</td><td>{{ fateText(a.to_fate) }}</td><td>{{ a.note || '—' }}</td>
          </tr>
          <tr v-if="!auditRows.length"><td colspan="6">暂无命运变更</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
const day = ref('2026-11-18')
const batch = ref(null)
const matches = ref([])
const variances = ref([])
const staging = ref([])
const auditRows = ref([])
const err = ref('')
const ok = ref('')

const FATE_LABELS = {
  pending: '待处置',
  accept_short: '认短',
  accept_over: '认溢',
  quarantine: '暂存隔离',
  discarded: '已丢弃',
}
const fateText = (f) => FATE_LABELS[f] || f
const fateClass = (f) => (f === 'quarantine' ? 'warn' : f === 'pending' ? 'bad' : 'ok')
const isSealed = computed(() => batch.value?.status === 'sealed')
const sealedText = computed(() => (isSealed.value ? '已结案' : '未结案'))

async function load() {
  err.value = ''
  const batches = await api(`/reconcile/batches?dock_day=${encodeURIComponent(day.value)}`)
  batch.value = batches[0] || null
  if (!batch.value) {
    matches.value = []; variances.value = []; staging.value = []; auditRows.value = []
    return
  }
  const [m, v, s, a] = await Promise.all([
    api(`/reconcile/matches?batch_id=${batch.value.id}`),
    api(`/reconcile/variances?batch_id=${batch.value.id}`),
    api(`/reconcile/staging?batch_id=${batch.value.id}&status=held`),
    api(`/reconcile/audit?batch_id=${batch.value.id}`),
  ])
  matches.value = m
  variances.value = v
  staging.value = s
  auditRows.value = a
}
async function run() {
  err.value = ''; ok.value = ''
  try {
    batch.value = await api('/reconcile/run', { method: 'POST', body: JSON.stringify({ dock_day: day.value }) })
    ok.value = '对账完成'
    await load()
  } catch (e) { err.value = e.message }
}
async function seal() {
  err.value = ''; ok.value = ''
  try {
    batch.value = await api('/reconcile/seal', { method: 'POST', body: JSON.stringify({ dock_day: day.value }) })
    ok.value = '已结案'
    await load()
  } catch (e) { err.value = e.message }
}
async function fate(id, f) {
  err.value = ''; ok.value = ''
  try {
    await api(`/reconcile/variances/${id}/fate`, {
      method: 'POST', body: JSON.stringify({ fate: f }),
    })
    ok.value = '已更新命运'
    await load()
  } catch (e) { err.value = e.message }
}
async function tryExport() {
  err.value = ''; ok.value = ''
  try {
    await api('/reconcile/quarantine/export')
  } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
