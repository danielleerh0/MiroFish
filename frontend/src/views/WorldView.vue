<template>
  <div class="world">
    <nav class="nav">
      <div class="brand" @click="router.push('/')">ScenarioIQ</div>
      <div class="nav-title">World model <span class="tag">v2 · P0</span></div>
    </nav>

    <div class="layout">
      <!-- Scenarios -->
      <aside class="side">
        <div class="side-head">
          <h2>Scenarios</h2>
        </div>
        <form class="new-form" @submit.prevent="onCreate">
          <input v-model="newName" placeholder="New scenario name" />
          <button class="btn" :disabled="!newName.trim() || busy">Create</button>
        </form>
        <div class="fixture-row">
          <button class="btn ghost" :disabled="busy" @click="onFixture(false)">Load RT-05 (approved)</button>
          <button class="btn ghost" :disabled="busy" @click="onFixture(true)">Load RT-05 (to review)</button>
        </div>
        <p v-if="!scenarios.length" class="muted small">No scenarios yet.</p>
        <ul class="scen-list">
          <li v-for="s in scenarios" :key="s.id">
            <div class="scen-name">{{ s.name }}</div>
            <div class="ver-list">
              <button
                v-for="v in s.versions" :key="v.id"
                class="ver" :class="{ active: v.id === versionId }"
                @click="openVersion(v.id)"
              >
                v{{ v.number }} <span class="chip" :class="v.status">{{ v.status }}</span>
              </button>
            </div>
          </li>
        </ul>
      </aside>

      <!-- Version -->
      <main class="main">
        <div v-if="error" class="alert" role="alert">
          {{ error }} <button class="link" @click="error = ''">Dismiss</button>
        </div>

        <div v-if="!world" class="empty">
          <h1>Build a reviewed world model</h1>
          <p>Create a scenario or load the synthetic RT-05 case. The LLM drafts entities, facts and rules from a
            redacted briefing. Nothing becomes a seed fact (F0) until you approve it.</p>
        </div>

        <template v-else>
          <header class="ver-head">
            <div>
              <h1>{{ world.scenario.name }} <span class="muted">v{{ world.version.number }}</span></h1>
              <p class="muted small">
                <span class="chip" :class="world.version.status">{{ world.version.status }}</span>
                {{ pendingCount }} proposed · {{ approvedCount }} approved · {{ rejectedCount }} rejected
                <template v-if="world.version.parent_id"> · derived from an earlier version</template>
              </p>
            </div>
            <div class="head-actions">
              <button v-if="isDraft" class="btn" :disabled="busy || pendingCount > 0" @click="onApproveVersion"
                      :title="pendingCount ? 'Review every proposed row first' : 'Freeze this version'">
                Approve version
              </button>
              <button class="btn ghost" :disabled="busy" @click="onNewVersion">New version from this</button>
            </div>
          </header>

          <div class="tabs" role="tablist">
            <button v-for="t in visibleTabs" :key="t.id" role="tab" class="tab"
                    :class="{ active: tab === t.id }" :aria-selected="tab === t.id" @click="tab = t.id">
              {{ t.label }}<span v-if="t.count !== undefined" class="count">{{ t.count }}</span>
            </button>
          </div>

          <!-- Source -->
          <section v-if="tab === 'source'" class="panel">
            <div v-if="world.documents.length" class="docs">
              <h3>Documents</h3>
              <div v-for="d in world.documents" :key="d.id" class="doc">
                <div>
                  <strong>{{ d.filename }}</strong>
                  <span class="muted small"> · {{ d.redacted_terms }} redacted terms</span>
                </div>
                <div v-if="isDraft" class="extract-row">
                  <input v-model="requirement" placeholder="Planning question (optional, not extracted)" />
                  <button class="btn" :disabled="busy" @click="onExtract(d.id)">Draft world with LLM</button>
                </div>
              </div>
            </div>

            <div v-if="extractResult" class="note">
              Drafted {{ summarise(extractResult.counts) }} with {{ extractResult.call.model }}
              ({{ extractResult.call.prompt_version }}).
              <details v-if="extractResult.issues.length">
                <summary>{{ extractResult.issues.length }} items dropped</summary>
                <ul><li v-for="(i, n) in extractResult.issues" :key="n" class="small">{{ i }}</li></ul>
              </details>
            </div>

            <template v-if="isDraft">
              <h3>Add a briefing</h3>
              <p class="muted small">Redaction runs before any model call. Preview what leaves this machine, then save.</p>
              <div class="upload">
                <input type="file" accept=".pdf,.md,.txt,.markdown" @change="onFile" />
                <span class="muted small">or paste text below</span>
              </div>
              <textarea v-if="!file" v-model="pasteText" rows="8" placeholder="Paste briefing text"></textarea>

              <h4>Terms to redact</h4>
              <div v-for="(t, i) in terms" :key="i" class="term">
                <input v-model="t.text" placeholder="Exact text, e.g. an organisation name" />
                <select v-model="t.label">
                  <option>ORG</option><option>PERSON</option><option>SITE</option>
                  <option>ASSET</option><option>FIGURE</option><option>TERM</option>
                </select>
                <button class="link" @click="terms.splice(i, 1)" aria-label="Remove term">Remove</button>
              </div>
              <button class="link" @click="terms.push({ text: '', label: 'ORG' })">+ Add term</button>
              <p class="muted small">Emails, Singapore phone numbers and NRIC/FIN numbers are redacted automatically.
                Terms you do not list are not redacted.</p>

              <div class="row-actions">
                <button class="btn ghost" :disabled="busy || !hasInput" @click="onPreview">Preview redaction</button>
                <button class="btn" :disabled="busy || !hasInput" @click="onSaveDoc">Save redacted document</button>
              </div>
              <pre v-if="preview" class="preview">{{ preview }}</pre>
            </template>
          </section>

          <!-- Review tables -->
          <section v-if="reviewTables.includes(tab)" class="panel">
            <div class="table-tools">
              <label class="small"><input type="checkbox" v-model="onlyPending" /> Only proposed</label>
              <button v-if="isDraft && verifiedPending.length" class="btn ghost small-btn" :disabled="busy"
                      @click="onApproveVerified">
                Approve {{ verifiedPending.length }} with verified quotes
              </button>
            </div>
            <table class="grid">
              <thead>
                <tr>
                  <th v-for="c in columns[tab]" :key="c.key">{{ c.label }}</th>
                  <th>Source</th><th>Status</th><th v-if="isDraft"></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="r in shownRows" :key="r.id" :class="r.review_status">
                  <td v-for="c in columns[tab]" :key="c.key" :class="c.cls">
                    <template v-if="editing === r.id && c.editable">
                      <textarea v-model="editBuffer[c.key]" rows="2"></textarea>
                    </template>
                    <template v-else>{{ fmt(r[c.key]) }}</template>
                  </td>
                  <td class="quote">
                    <span v-if="r.source_quote">
                      <span class="qv" :class="qClass(r)" :title="qTitle(r)">{{ qIcon(r) }}</span>
                      "{{ r.source_quote }}"
                    </span>
                    <span v-else class="muted small">{{ r.origin }}</span>
                  </td>
                  <td>
                    <span class="chip" :class="r.review_status">{{ r.review_status }}</span>
                    <span class="prov" :class="r.provenance" :title="provLabel[r.provenance]">{{ r.provenance }}</span>
                  </td>
                  <td v-if="isDraft" class="acts">
                    <template v-if="editing === r.id">
                      <button class="link" @click="saveEdit(r)">Save</button>
                      <button class="link" @click="editing = null">Cancel</button>
                    </template>
                    <template v-else>
                      <button v-if="r.review_status !== 'approved'" class="link ok" :disabled="busy"
                              @click="decide(r, 'approve')">Approve</button>
                      <button v-if="r.review_status !== 'rejected'" class="link bad" :disabled="busy"
                              @click="decide(r, 'reject')">Reject</button>
                      <button class="link" @click="startEdit(r)">Edit</button>
                    </template>
                  </td>
                </tr>
                <tr v-if="!shownRows.length"><td :colspan="columns[tab].length + 3" class="muted">Nothing here.</td></tr>
              </tbody>
            </table>
          </section>

          <!-- Injections -->
          <section v-if="tab === 'injections'" class="panel">
            <p class="muted small">An injection is new information (escalation or de-escalation) applied to a running
              world. It never edits the frozen seed. Review it, finalize it, then apply it when you continue a run.</p>
            <div class="row-actions">
              <button v-if="isRt05" class="btn ghost small-btn" :disabled="busy" @click="onLoadRt05Updates">
                Load RT-05 updates (escalation + de-escalation)</button>
            </div>

            <div class="inj-list">
              <button v-for="i in world.injections" :key="i.id" class="ver"
                      :class="{ active: inj && inj.id === i.id }" @click="openInjection(i.id)">
                {{ i.label }} <span class="chip" :class="i.status === 'ready' ? 'approved' : 'draft'">{{ i.status }}</span>
                <span v-if="i.pending" class="muted small">{{ i.pending }} to review</span>
              </button>
            </div>

            <div v-if="inj" class="inj-detail">
              <h3>{{ inj.label }}</h3>
              <details>
                <summary class="small">Redacted briefing ({{ inj.redacted_terms }} placeholders, shared with the seed)</summary>
                <pre class="preview">{{ inj.redacted_text }}</pre>
              </details>
              <div v-if="inj.status === 'draft'" class="row-actions">
                <label class="small"><input type="checkbox" v-model="autoAccept" /> Accept items the source proves</label>
                <button class="btn" :disabled="busy" @click="onDraftInjection">Draft changes with LLM</button>
                <button class="btn ghost" :disabled="busy || injPending > 0 || !inj.items.length" @click="onFinalize"
                        :title="injPending ? 'Review every proposed item first' : 'Freeze this injection'">Finalize</button>
              </div>
              <div v-if="injResult" class="note small">
                Drafted {{ summarise(injResult.counts) }}.
                <span v-if="injResult.call">Model {{ injResult.call.model }} ({{ injResult.call.prompt_version }}).</span>
                <details v-if="injResult.issues.length"><summary>{{ injResult.issues.length }} items dropped</summary>
                  <ul><li v-for="(x, n) in injResult.issues" :key="n">{{ x }}</li></ul></details>
              </div>
              <table class="grid">
                <thead><tr><th>#</th><th>Change</th><th>Source</th><th>Status</th><th v-if="inj.status === 'draft'"></th></tr></thead>
                <tbody>
                  <tr v-for="it in inj.items" :key="it.id" :class="it.review_status">
                    <td>{{ it.seq }}</td>
                    <td class="mono detail">{{ describeItem(it) }}</td>
                    <td class="quote">
                      <span v-if="it.source_quote"><span class="qv" :class="qClass(it)" :title="qTitle(it)">{{ qIcon(it) }}</span>
                        "{{ it.source_quote }}"</span>
                      <span v-else class="muted small">{{ it.origin }}</span>
                    </td>
                    <td><span class="chip" :class="it.review_status">{{ it.review_status }}</span>
                      <span v-if="it.auto_accepted" class="small muted"> auto</span></td>
                    <td v-if="inj.status === 'draft'" class="acts">
                      <button v-if="it.review_status !== 'approved'" class="link ok" :disabled="busy"
                              @click="decideItem(it, 'approve')">Approve</button>
                      <button v-if="it.review_status !== 'rejected'" class="link bad" :disabled="busy"
                              @click="decideItem(it, 'reject')">Reject</button>
                    </td>
                  </tr>
                  <tr v-if="!inj.items.length"><td colspan="5" class="muted">No changes yet. Draft them with the LLM.</td></tr>
                </tbody>
              </table>
            </div>

            <h3>New injection</h3>
            <input v-model="injLabel" class="wide" placeholder="Label, e.g. Escalation: second power fault" />
            <div class="upload">
              <input type="file" accept=".pdf,.md,.txt,.markdown" @change="onInjFile" />
              <span class="muted small">or paste the update below</span>
            </div>
            <textarea v-if="!injFile" v-model="injText" rows="6" placeholder="Paste the update briefing"></textarea>
            <p class="muted small">Terms already redacted in this scenario are redacted again with the same placeholders.
              Add only new names.</p>
            <div v-for="(t, i) in injTerms" :key="'it' + i" class="term">
              <input v-model="t.text" placeholder="New name to redact" />
              <select v-model="t.label"><option>ORG</option><option>PERSON</option><option>SITE</option>
                <option>ASSET</option><option>FIGURE</option><option>TERM</option></select>
              <button class="link" @click="injTerms.splice(i, 1)">Remove</button>
            </div>
            <button class="link" @click="injTerms.push({ text: '', label: 'ORG' })">+ Add term</button>
            <div class="row-actions">
              <button class="btn" :disabled="busy || !injLabel.trim() || (!injFile && !injText.trim())"
                      @click="onCreateInjection">Create injection</button>
            </div>
          </section>

          <!-- Simulations -->
          <section v-if="tab === 'sims'" class="panel">
            <template v-if="!isDraft">
              <h3>Run a scripted slice</h3>
              <p class="muted small">Agents propose actions. The engine validates each one against world truth and logs
                every step. In P1, OASIS agents will produce these proposals.</p>
              <textarea v-model="scriptText" rows="10" class="mono" spellcheck="false"></textarea>
              <div class="row-actions">
                <input v-model="runName" placeholder="Run name" />
                <button class="btn" :disabled="busy" @click="onRun">Run</button>
              </div>
            </template>
            <p v-else class="muted">Approve this version before running simulations.</p>

            <h3 v-if="world.simulations.length">Runs</h3>
            <div class="runs tree">
              <button v-for="s in runTree" :key="s.id" class="ver run"
                      :style="{ marginLeft: s.depth * 18 + 'px' }"
                      :class="{ active: sim && sim.simulation.id === s.id }" @click="openSim(s.id)">
                <span v-if="s.depth">↳ </span>{{ s.name }}
                <span v-if="s.parent_id" class="muted small">after round {{ s.fork_round }}</span>
                <span v-if="s.injection_label" class="chip x0">{{ s.injection_label }}</span>
              </button>
            </div>

            <div v-if="sim" class="events">
              <div class="events-head">
                <h3>Event log · {{ sim.simulation.name }}</h3>
                <button class="btn ghost small-btn" :disabled="busy" @click="onReplay">Replay</button>
              </div>
              <div class="continue">
                <h4>Continue this run</h4>
                <p class="muted small">Branch after a round. The original run stays as it is. Apply an injection to model
                  escalation or de-escalation, then add the next actions.</p>
                <div class="row-actions">
                  <label class="small">After round
                    <select v-model.number="forkRound">
                      <option v-for="r in simRounds" :key="r" :value="r">{{ r }}</option>
                    </select></label>
                  <label class="small">Injection
                    <select v-model="forkInjection">
                      <option value="">None (branch only)</option>
                      <option v-for="i in readyInjections" :key="i.id" :value="i.id">{{ i.label }}</option>
                    </select></label>
                  <input v-model="forkName" placeholder="Branch name" />
                </div>
                <textarea v-model="forkScript" rows="6" class="mono" spellcheck="false"
                          :placeholder="`Actions from round ${forkRound + 1}, as a JSON list`"></textarea>
                <div class="row-actions">
                  <button class="btn" :disabled="busy" @click="onContinue">Continue</button>
                  <button v-if="isRt05 && forkInjection" class="link" @click="fillFixtureBranchScript">Use RT-05 sample actions</button>
                </div>
              </div>
              <div class="legend small">
                <span v-for="(label, code) in provLabel" :key="code"><span class="prov" :class="code">{{ code }}</span> {{ label }}</span>
              </div>
              <table class="grid events-table">
                <thead><tr><th>#</th><th>Round</th><th>Type</th><th>Actor</th><th>Detail</th><th></th></tr></thead>
                <tbody>
                  <tr v-for="e in sim.events" :key="e.id" :class="['ev', e.kind, e.valid === false ? 'invalid' : '']">
                    <td>{{ e.seq }}</td>
                    <td>{{ e.round }}</td>
                    <td>{{ e.kind.replace('_', ' ') }}</td>
                    <td>{{ e.actor_key || '—' }}</td>
                    <td class="detail">{{ describe(e) }}</td>
                    <td><span class="prov" :class="e.provenance" :title="provLabel[e.provenance]">{{ e.provenance }}</span></td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>
        </template>
      </main>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as api from '../api/world'

const route = useRoute()
const router = useRouter()

const scenarios = ref([])
const world = ref(null)
const versionId = ref(route.params.versionId || null)
const tab = ref('facts')
const busy = ref(false)
const error = ref('')

const newName = ref('')
const file = ref(null)
const pasteText = ref('')
const terms = ref([{ text: '', label: 'ORG' }])
const preview = ref('')
const requirement = ref('')
const extractResult = ref(null)

const onlyPending = ref(false)
const editing = ref(null)
const editBuffer = ref({})

const inj = ref(null)
const injResult = ref(null)
const autoAccept = ref(true)
const injLabel = ref('')
const injText = ref('')
const injFile = ref(null)
const injTerms = ref([])
const forkRound = ref(0)
const forkInjection = ref('')
const forkName = ref('Branch')
const forkScript = ref('[]')
const fixtureBranchScripts = ref(null)

const scriptText = ref('[]')
const runName = ref('Baseline')
const sim = ref(null)

const provLabel = {
  F0: 'Seed fact (approved)',
  F1: 'Derived by rules',
  A0: 'Agent action',
  A1: 'Agent belief / assertion',
  S0: 'Simulation outcome',
  U0: 'Unverified / uncertain',
  X0: 'Injected from outside'
}

const reviewTables = ['entities', 'facts', 'relationships', 'rules', 'profiles', 'knowledge']
const columns = {
  entities: [
    { key: 'key', label: 'Key', cls: 'mono' },
    { key: 'kind', label: 'Kind', editable: true },
    { key: 'name', label: 'Name', editable: true }
  ],
  facts: [
    { key: 'entity_key', label: 'Entity', cls: 'mono' },
    { key: 'attribute', label: 'Attribute', cls: 'mono', editable: true },
    { key: 'value', label: 'Value', cls: 'mono', editable: true },
    { key: 'unit', label: 'Unit', editable: true }
  ],
  relationships: [
    { key: 'source_key', label: 'Source', cls: 'mono' },
    { key: 'type', label: 'Type', cls: 'mono', editable: true },
    { key: 'target_key', label: 'Target', cls: 'mono' }
  ],
  rules: [
    { key: 'key', label: 'Rule', cls: 'mono' },
    { key: 'description', label: 'Description', editable: true },
    { key: 'definition', label: 'Definition (JSON)', cls: 'mono json', editable: true }
  ],
  profiles: [
    { key: 'entity_key', label: 'Actor', cls: 'mono' },
    { key: 'role', label: 'Role', editable: true },
    { key: 'objectives', label: 'Objectives', editable: true },
    { key: 'authority', label: 'Authority', cls: 'mono', editable: true }
  ],
  knowledge: [
    { key: 'agent_key', label: 'Agent', cls: 'mono' },
    { key: 'kind', label: 'Knows / believes', editable: true },
    { key: 'subject_key', label: 'About', cls: 'mono' },
    { key: 'attribute', label: 'Attribute', cls: 'mono', editable: true },
    { key: 'value', label: 'Value', cls: 'mono', editable: true }
  ]
}

const isDraft = computed(() => world.value?.version.status === 'draft')
const allRows = computed(() => (world.value ? reviewTables.flatMap(t => world.value[t].map(r => ({ ...r, _t: t }))) : []))
const pendingCount = computed(() => allRows.value.filter(r => r.review_status === 'proposed').length)
const approvedCount = computed(() => allRows.value.filter(r => r.review_status === 'approved').length)
const rejectedCount = computed(() => allRows.value.filter(r => r.review_status === 'rejected').length)

const visibleTabs = computed(() => {
  if (!world.value) return []
  return [
    { id: 'source', label: 'Source', count: world.value.documents.length },
    ...reviewTables.map(t => ({ id: t, label: t[0].toUpperCase() + t.slice(1), count: world.value[t].length })),
    ...(isDraft.value ? [] : [{ id: 'injections', label: 'Injections', count: world.value.injections.length }]),
    { id: 'sims', label: 'Simulations', count: world.value.simulations.length }
  ]
})

const shownRows = computed(() => {
  const rows = world.value?.[tab.value] || []
  return onlyPending.value ? rows.filter(r => r.review_status === 'proposed') : rows
})
const verifiedPending = computed(() =>
  (world.value?.[tab.value] || []).filter(r => r.review_status === 'proposed' && r.quote_verified === true))

const isRt05 = computed(() => world.value?.scenario.name.startsWith('RT-05'))
const readyInjections = computed(() => (world.value?.injections || []).filter(i => i.status === 'ready'))
const injPending = computed(() => (inj.value?.items || []).filter(i => i.review_status === 'proposed').length)
const simRounds = computed(() => {
  const max = Math.max(0, ...(sim.value?.events || []).map(e => e.round))
  return Array.from({ length: max + 1 }, (_, i) => i)
})
const runTree = computed(() => {
  const sims = world.value?.simulations || []
  const kids = {}
  sims.forEach(s => { (kids[s.parent_id || ''] ||= []).push(s) })
  const out = []
  const walk = (pid, depth) => (kids[pid] || []).forEach(s => { out.push({ ...s, depth }); walk(s.id, depth + 1) })
  walk('', 0)
  return out
})

const hasInput = computed(() => !!file.value || !!pasteText.value.trim())
const cleanTerms = () => terms.value.filter(t => t.text.trim())

async function guard(fn) {
  busy.value = true
  error.value = ''
  try { return await fn() } catch (e) { error.value = e.message || String(e) } finally { busy.value = false }
}

async function refreshScenarios() {
  const res = await api.listScenarios()
  scenarios.value = res.data
}

async function openVersion(id) {
  if (!id) return
  versionId.value = id
  if (route.params.versionId !== id) router.replace({ name: 'WorldVersion', params: { versionId: id } })
  await guard(async () => {
    const res = await api.getVersion(id)
    world.value = res.data
    sim.value = null
    extractResult.value = null
    if (!world.value.documents.length && isDraft.value && !allRows.value.length) tab.value = 'source'
  })
}

async function reload() { await openVersion(versionId.value); await refreshScenarios() }

async function onCreate() {
  await guard(async () => {
    const res = await api.createScenario({ name: newName.value.trim() })
    newName.value = ''
    await refreshScenarios()
    await openVersion(res.data.version_id)
    tab.value = 'source'
  })
}

async function onFixture(review) {
  await guard(async () => {
    const res = await api.loadRt05Fixture(review)
    await refreshScenarios()
    await openVersion(res.data.version_id)
    scriptText.value = JSON.stringify(res.data.scripts.baseline, null, 2)
    tab.value = review ? 'facts' : 'sims'
  })
}

function onFile(e) { file.value = e.target.files[0] || null; preview.value = '' }

async function onPreview() {
  await guard(async () => {
    const res = await api.redactPreview({ file: file.value, text: pasteText.value, terms: cleanTerms() })
    preview.value = res.data.redacted_text
  })
}

async function onSaveDoc() {
  await guard(async () => {
    await api.addDocument(versionId.value, { file: file.value, text: pasteText.value, filename: 'pasted.txt', terms: cleanTerms() })
    file.value = null; pasteText.value = ''; preview.value = ''
    await reload()
    tab.value = 'source'
  })
}

async function onExtract(docId) {
  await guard(async () => {
    const res = await api.extractWorld(versionId.value, docId, requirement.value || null)
    extractResult.value = res.data
    const keep = extractResult.value
    await openVersion(versionId.value)
    extractResult.value = keep
  })
}

async function decide(r, decision) {
  await guard(async () => { await api.reviewRow(tab.value, r.id, decision); await openVersion(versionId.value) })
}

async function onApproveVerified() {
  await guard(async () => {
    for (const r of verifiedPending.value) await api.reviewRow(tab.value, r.id, 'approve')
    await openVersion(versionId.value)
  })
}

function startEdit(r) {
  editing.value = r.id
  editBuffer.value = {}
  for (const c of columns[tab.value]) {
    if (c.editable) editBuffer.value[c.key] = typeof r[c.key] === 'object' || typeof r[c.key] === 'boolean' || typeof r[c.key] === 'number'
      ? JSON.stringify(r[c.key]) : (r[c.key] ?? '')
  }
}

function parseMaybeJson(v) {
  if (typeof v !== 'string') return v
  const s = v.trim()
  if (s === '') return null
  try { return JSON.parse(s) } catch { return v }
}

async function saveEdit(r) {
  const edits = {}
  for (const [k, v] of Object.entries(editBuffer.value)) {
    const parsed = ['value', 'definition', 'objectives', 'authority'].includes(k) ? parseMaybeJson(v) : v
    if (JSON.stringify(parsed) !== JSON.stringify(r[k])) edits[k] = parsed
  }
  await guard(async () => {
    if (Object.keys(edits).length) await api.reviewRow(tab.value, r.id, 'edit', edits)
    editing.value = null
    await openVersion(versionId.value)
  })
}

async function onApproveVersion() {
  await guard(async () => { await api.approveVersion(versionId.value); await reload(); tab.value = 'sims' })
}

async function onNewVersion() {
  const note = window.prompt('What will change in the new version?') // eslint-disable-line no-alert
  if (note === null) return
  await guard(async () => {
    const res = await api.newVersion(versionId.value, note)
    await refreshScenarios()
    await openVersion(res.data.version_id)
  })
}

async function onRun() {
  await guard(async () => {
    let actions
    try { actions = JSON.parse(scriptText.value) } catch { throw new Error('Script is not valid JSON') }
    const res = await api.runSimulation(versionId.value, actions, runName.value)
    sim.value = res.data
    const keep = sim.value
    await openVersion(versionId.value)
    sim.value = keep
    tab.value = 'sims'
  })
}

async function openInjection(id) {
  await guard(async () => { inj.value = (await api.getInjection(id)).data; injResult.value = null })
}

function onInjFile(e) { injFile.value = e.target.files[0] || null }

async function refreshInjection() {
  if (inj.value) inj.value = (await api.getInjection(inj.value.id)).data
  const keep = inj.value
  const keepRes = injResult.value
  await openVersion(versionId.value)
  inj.value = keep
  injResult.value = keepRes
  tab.value = 'injections'
}

async function onCreateInjection() {
  await guard(async () => {
    const res = await api.createInjection(versionId.value, {
      file: injFile.value, text: injText.value, filename: 'update.txt', label: injLabel.value,
      terms: injTerms.value.filter(t => t.text.trim())
    })
    inj.value = res.data
    injLabel.value = ''; injText.value = ''; injFile.value = null; injTerms.value = []
    await refreshInjection()
  })
}

async function onDraftInjection() {
  await guard(async () => {
    const res = await api.draftInjection(inj.value.id, autoAccept.value)
    injResult.value = res.data
    inj.value = res.data.injection
    await refreshInjection()
  })
}

async function decideItem(it, decision) {
  await guard(async () => { await api.reviewInjectionItem(it.id, decision); await refreshInjection() })
}

async function onFinalize() {
  await guard(async () => { await api.finalizeInjection(inj.value.id); await refreshInjection() })
}

async function onLoadRt05Updates() {
  await guard(async () => {
    const res = await api.loadRt05Injections(versionId.value)
    fixtureBranchScripts.value = res.data
    await openVersion(versionId.value)
    tab.value = 'injections'
  })
}

function fillFixtureBranchScript() {
  const fx = fixtureBranchScripts.value
  const chosen = readyInjections.value.find(i => i.id === forkInjection.value)
  if (!chosen) return
  const key = chosen.label.toLowerCase().startsWith('de-escalation') ? 'deescalation' : 'escalation'
  const script = fx?.scripts?.[key]
  if (!script) { error.value = 'Load the RT-05 updates first to get sample actions'; return }
  const shift = forkRound.value + 1 - Math.min(...script.map(a => a.round))
  forkScript.value = JSON.stringify(script.map(a => ({ ...a, round: a.round + shift })), null, 2)
  forkName.value = chosen.label.split(':')[0]
}

async function onContinue() {
  await guard(async () => {
    let actions
    try { actions = JSON.parse(forkScript.value || '[]') } catch { throw new Error('Actions are not valid JSON') }
    const res = await api.continueRun(sim.value.simulation.id, {
      fork_round: forkRound.value, injection_id: forkInjection.value || null, actions, name: forkName.value
    })
    const keep = res.data
    await openVersion(versionId.value)
    sim.value = keep
    tab.value = 'sims'
  })
}

function describeItem(it) {
  const p = it.payload
  switch (it.op) {
    case 'add_entity': return `+ ${p.kind} ${p.key} (${p.name})`
    case 'set_fact': return `${p.entity}.${p.attribute} → ${fmt(p.value)}${p.unit ? ' ' + p.unit : ''}` +
      (p.informed?.length ? `  · told: ${p.informed.join(', ')}` : '  · told: nobody')
    case 'add_rule': return `rule ${p.key}: ${p.description}`
    case 'set_profile': return `profile ${p.entity}: ${p.role}; authority ${fmt(p.authority)}`
    default: return fmt(p)
  }
}

async function openSim(id) {
  await guard(async () => {
    sim.value = (await api.getSimulation(id)).data
    forkRound.value = Math.max(0, ...sim.value.events.map(e => e.round))
  })
}

async function onReplay() {
  await guard(async () => {
    const res = await api.replaySimulation(sim.value.simulation.id)
    const keep = res.data
    await openVersion(versionId.value)
    sim.value = keep
    tab.value = 'sims'
  })
}

function fmt(v) {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
const summarise = (c) => Object.entries(c).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(', ') || 'nothing'
const qIcon = (r) => (r.quote_verified === true ? '✓' : r.quote_verified === false ? '✗' : '·')
const qClass = (r) => (r.quote_verified === true ? 'yes' : r.quote_verified === false ? 'no' : '')
const qTitle = (r) => (r.quote_verified === true ? 'Quote found in source'
  : r.quote_verified === false ? 'Quote NOT found in source: check before approving' : 'No source to check')

function describe(e) {
  const p = e.payload || {}
  switch (e.kind) {
    case 'derivation':
      return `${p.entity}.${p.attribute} = ${fmt(p.value)} (rule ${p.rule}; inputs ${fmt(p.inputs)})`
    case 'derivation_skipped':
      return `Rule ${p.rule} skipped${p.binding ? ` for ${p.binding}` : ''}: ${e.reason}`
    case 'observation': {
      const b = Object.entries(p.beliefs || {}).map(([k, v]) =>
        `${k}=${fmt(v.value)} (${v.kind}${v.matches_truth ? '' : ', differs from truth'})`)
      return `About ${p.subject}: ${b.join('; ') || 'no prior knowledge'}${p.rationale ? ` · "${p.rationale}"` : ''}`
    }
    case 'action':
      return `${e.action_type} ${fmt(p.params)}`
    case 'validation':
      return `${e.valid ? 'VALID' : 'REJECTED'}: ${e.reason}`
    case 'state_change':
      return `${p.entity}.${p.attribute}: ${fmt(p.old)} → ${fmt(p.new)}`
    case 'fork':
      return `Continues "${p.parent_name}" after round ${p.fork_round}`
    case 'injection':
      return `${p.label}: ${p.fact_changes} fact changes` +
        (p.new_entities.length ? `; new ${p.new_entities.join(', ')}` : '') +
        (p.rules.length ? `; rules ${p.rules.join(', ')}` : '') +
        (p.profiles.length ? `; profiles ${p.profiles.join(', ')}` : '')
    case 'entity_added':
      return `New ${p.kind} ${p.entity} (${p.name})`
    case 'rule_set':
      return `Rule ${p.rule}${p.replaces_seed_rule ? ' replaces the seed rule in this branch' : ' added'}`
    case 'profile_set':
      return `Profile for ${p.entity}; authority ${fmt(p.authority)}`
    case 'knowledge_update':
      return `Now ${p.kind} ${p.subject}.${p.attribute} = ${fmt(p.new)} (was ${fmt(p.old_belief)})`
    default:
      return fmt(p)
  }
}

watch(() => route.params.versionId, (id) => { if (id && id !== versionId.value) openVersion(id) })

onMounted(async () => {
  await guard(refreshScenarios)
  if (versionId.value) await openVersion(versionId.value)
  try {
    const res = await api.getFixtureScripts()
    if (scriptText.value === '[]') scriptText.value = JSON.stringify(res.data.baseline || [], null, 2)
  } catch { /* fixture scripts are optional */ }
})
</script>

<style scoped>
.world {
  --paper: #EEF1F4;
  --card: #FFFFFF;
  --ink: #1B2A3A;
  --muted: #56667A;
  --line: #CDD5DE;
  --teal: #2E8B7A;
  --amber: #C9822B;
  --red: #C4553C;
  --blue: #3A6EA5;
  --violet: #6B5AA6;
  min-height: 100vh;
  background: var(--paper);
  color: var(--ink);
  font-family: 'Inter', 'Segoe UI', Roboto, Arial, sans-serif;
  font-size: 0.95rem;
  line-height: 1.5;
}
.nav { display: flex; align-items: center; gap: 24px; padding: 14px 24px; background: var(--card); border-bottom: 1px solid var(--line); }
.brand { font-weight: 700; letter-spacing: 0.02em; cursor: pointer; }
.nav-title { color: var(--muted); }
.tag { font-size: 0.75rem; border: 1px solid var(--line); border-radius: 4px; padding: 1px 6px; margin-left: 6px; }

.layout { display: grid; grid-template-columns: 280px 1fr; min-height: calc(100vh - 52px); }
@media (max-width: 860px) { .layout { grid-template-columns: 1fr; } }
.side { background: var(--card); border-right: 1px solid var(--line); padding: 16px; overflow-y: auto; }
.side h2 { font-size: 1rem; margin-bottom: 10px; }
.new-form { display: flex; gap: 6px; margin-bottom: 8px; }
.new-form input { flex: 1; min-width: 0; }
.fixture-row { display: flex; flex-direction: column; gap: 6px; margin-bottom: 16px; }
.scen-list { list-style: none; }
.scen-list li { padding: 10px 0; border-top: 1px solid var(--line); }
.scen-name { font-weight: 600; margin-bottom: 6px; }
.ver-list, .runs { display: flex; flex-wrap: wrap; gap: 6px; }
.ver { background: transparent; border: 1px solid var(--line); border-radius: 6px; padding: 4px 8px; cursor: pointer; font-size: 0.85rem; color: var(--ink); }
.ver.active { border-color: var(--ink); background: var(--paper); }

.main { padding: 20px 24px; min-width: 0; }
.empty { max-width: 640px; margin-top: 40px; }
.empty h1 { font-size: 1.5rem; margin-bottom: 8px; }
.ver-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap; margin-bottom: 14px; }
.ver-head h1 { font-size: 1.35rem; }
.head-actions { display: flex; gap: 8px; }

.tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--line); overflow-x: auto; }
.tab { background: transparent; border: none; border-bottom: 2px solid transparent; padding: 8px 12px; cursor: pointer; color: var(--muted); white-space: nowrap; }
.tab.active { color: var(--ink); border-bottom-color: var(--ink); font-weight: 600; }
.count { margin-left: 6px; font-size: 0.75rem; color: var(--muted); }
.panel { background: var(--card); border: 1px solid var(--line); border-top: none; padding: 16px; overflow-x: auto; }
.panel h3 { font-size: 1rem; margin: 12px 0 6px; }
.panel h4 { font-size: 0.9rem; margin: 12px 0 6px; }

input, select, textarea { font: inherit; border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; background: #fff; color: var(--ink); }
textarea { width: 100%; }
.mono, .mono textarea { font-family: 'JetBrains Mono', monospace; font-size: 0.82rem; }
.btn { background: var(--ink); color: #fff; border: 1px solid var(--ink); border-radius: 6px; padding: 6px 12px; cursor: pointer; }
.btn.ghost { background: transparent; color: var(--ink); }
.btn:disabled { opacity: 0.45; cursor: not-allowed; }
.small-btn { padding: 3px 8px; font-size: 0.82rem; }
.link { background: none; border: none; color: var(--blue); cursor: pointer; padding: 0 4px; font-size: 0.85rem; }
.link.ok { color: var(--teal); }
.link.bad { color: var(--red); }
.link:disabled { opacity: 0.4; }
.muted { color: var(--muted); }
.small { font-size: 0.82rem; }
.alert { background: #FBEDEA; border: 1px solid var(--red); color: var(--ink); padding: 8px 12px; border-radius: 6px; margin-bottom: 12px; }
.note { background: var(--paper); border-radius: 6px; padding: 8px 12px; margin: 10px 0; }

.doc { border: 1px solid var(--line); border-radius: 6px; padding: 10px; margin-bottom: 8px; }
.extract-row, .row-actions { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
.extract-row input { flex: 1; min-width: 200px; }
.upload { display: flex; align-items: center; gap: 10px; margin: 8px 0; }
.term { display: flex; gap: 6px; margin-bottom: 6px; }
.term input { flex: 1; }
.preview { white-space: pre-wrap; background: var(--paper); padding: 10px; border-radius: 6px; margin-top: 10px; max-height: 320px; overflow: auto; font-size: 0.82rem; }

.table-tools { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; gap: 8px; }
.grid { width: 100%; border-collapse: collapse; font-size: 0.86rem; }
.grid th { text-align: left; font-weight: 600; color: var(--muted); border-bottom: 1px solid var(--line); padding: 6px 8px; white-space: nowrap; }
.grid td { border-bottom: 1px solid var(--line); padding: 6px 8px; vertical-align: top; }
.grid tr.rejected td { opacity: 0.5; text-decoration: line-through; }
.grid tr.rejected td:last-child, .grid tr.rejected td:nth-last-child(2) { text-decoration: none; }
.json { max-width: 360px; word-break: break-word; }
.quote { max-width: 340px; color: var(--muted); font-size: 0.82rem; }
.qv { font-weight: 700; margin-right: 2px; }
.qv.yes { color: var(--teal); }
.qv.no { color: var(--red); }
.acts { white-space: nowrap; }

.chip { display: inline-block; font-size: 0.72rem; border-radius: 10px; padding: 1px 7px; border: 1px solid var(--line); text-transform: uppercase; letter-spacing: 0.03em; }
.chip.approved { border-color: var(--teal); color: var(--teal); }
.chip.proposed, .chip.draft { border-color: var(--amber); color: var(--amber); }
.chip.rejected { border-color: var(--red); color: var(--red); }
.prov { display: inline-block; font-family: 'JetBrains Mono', monospace; font-size: 0.72rem; border-radius: 4px; padding: 0 5px; margin-left: 4px; color: #fff; background: var(--muted); }
.prov.F0 { background: var(--ink); }
.prov.F1 { background: var(--blue); }
.prov.A0 { background: var(--violet); }
.prov.A1 { background: #8C7FC0; }
.prov.S0 { background: var(--teal); }
.prov.U0 { background: var(--amber); }
.prov.X0 { background: #A0522D; }
.chip.x0 { border-color: #A0522D; color: #A0522D; text-transform: none; letter-spacing: 0; margin-left: 4px; }
.runs.tree { flex-direction: column; align-items: flex-start; }
.run { text-align: left; }
.inj-list { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0; }
.inj-detail { border-top: 1px solid var(--line); margin-top: 10px; padding-top: 6px; }
.continue { background: var(--paper); border-radius: 6px; padding: 10px 12px; margin: 10px 0; }
.continue h4 { margin: 0 0 4px; }
.continue label { display: flex; align-items: center; gap: 6px; }
.wide { width: 100%; margin-bottom: 6px; }
.ev.injection td, .ev.entity_added td, .ev.rule_set td, .ev.profile_set td { background: #F6EEE8; }
.ev.fork td { background: #F1F1F4; font-style: italic; }
.legend { display: flex; flex-wrap: wrap; gap: 12px; margin: 6px 0 10px; color: var(--muted); }

.events-head { display: flex; justify-content: space-between; align-items: center; margin-top: 16px; }
.events-table .detail { font-family: 'JetBrains Mono', monospace; font-size: 0.8rem; word-break: break-word; }
.ev.invalid td { background: #FBEDEA; }
.ev.state_change td { background: #EAF5F2; }
.ev.derivation td { background: #EDF2F9; }
</style>
