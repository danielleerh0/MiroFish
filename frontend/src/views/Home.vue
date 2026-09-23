<template>
  <div class="home">
    <nav class="nav">
      <div class="brand">MiroFish</div>
      <div class="nav-right">
        <LanguageSwitcher />
        <a href="https://github.com/666ghj/MiroFish" target="_blank" rel="noopener" class="nav-link">Source code</a>
      </div>
    </nav>

    <main class="wrap">
      <!-- Hero -->
      <section class="hero">
        <div class="hero-text">
          <h1>Rehearse a decision before you make it</h1>
          <p class="lede">
            MiroFish turns your document into a simulated world of the people and
            organisations it describes. You pose a question, run the scenario, and see how
            each party is likely to respond, and what follows from that.
          </p>
          <p class="lede lede-small">Use the results to test your judgement, not to replace it.</p>
          <a href="#start" class="btn btn-main">Run a simulation</a>
        </div>

        <figure class="futures" aria-hidden="true">
          <svg viewBox="0 0 440 320" class="futures-svg">
            <!-- paths -->
            <path
              v-for="(p, i) in paths"
              :key="'p' + i"
              :d="p.d"
              class="branch"
              :class="{ risk: p.risk }"
              :style="{ animationDelay: p.delay + 's' }"
            />
            <!-- moving pulses (loop) -->
            <template v-if="motionOK">
              <circle v-for="(p, i) in pulsePaths" :key="'m' + i" r="3.5" class="pulse" :class="{ risk: p.risk }">
                <animateMotion :path="p.d" :dur="p.dur + 's'" :begin="(2.6 + p.offset) + 's'" repeatCount="indefinite" />
              </circle>
            </template>
            <!-- nodes -->
            <circle
              v-for="(n, i) in nodes"
              :key="'n' + i"
              :cx="n.x" :cy="n.y" :r="n.r"
              class="node"
              :class="n.kind"
              :style="{ animationDelay: n.delay + 's' }"
            />
          </svg>
          <div class="futures-cols">
            <span>Your decision</span>
            <span>How each party responds</span>
            <span>What follows</span>
          </div>
          <figcaption class="futures-caption">
            <span class="key key-risk"></span> Knock-on effects you did not plan for
          </figcaption>
        </figure>
      </section>

      <!-- Start form + steps -->
      <section class="block start" id="start">
        <div class="steps-col">
          <h2>How a simulation works</h2>
          <ol class="steps">
            <li v-for="(s, i) in steps" :key="i" :ref="el => stepEls[i] = el" :class="{ seen: seen[i] }">
              <div>
                <h3>{{ s.title }}</h3>
                <p>{{ s.body }}</p>
              </div>
            </li>
          </ol>
        </div>

        <div class="form-col">
          <h2>Run a simulation</h2>
          <div class="form-card">
            <label class="field-label">Your document</label>
            <div
              class="upload-zone"
              :class="{ 'drag-over': isDragOver, 'has-files': files.length > 0 }"
              @dragover.prevent="handleDragOver"
              @dragleave.prevent="handleDragLeave"
              @drop.prevent="handleDrop"
              @click="triggerFileInput"
            >
              <input
                ref="fileInput"
                type="file"
                multiple
                accept=".pdf,.md,.txt"
                @change="handleFileSelect"
                style="display: none"
                :disabled="loading"
              />
              <div v-if="files.length === 0" class="upload-empty">
                <strong>Drop files here or click to choose</strong>
                <span>A policy paper, briefing, plan or news report. PDF, MD or TXT.</span>
              </div>
              <ul v-else class="file-list">
                <li v-for="(file, index) in files" :key="index" class="file-item">
                  <span class="file-name">{{ file.name }}</span>
                  <button type="button" class="remove-btn" @click.stop="removeFile(index)" :aria-label="'Remove ' + file.name">Remove</button>
                </li>
              </ul>
            </div>

            <label class="field-label" for="question">The decision you want to test</label>
            <textarea
              id="question"
              v-model="formData.simulationRequirement"
              class="question"
              placeholder="Example: If we end this grant next year, how will small logistics firms, their customers and industry associations respond over six months?"
              rows="5"
              :disabled="loading"
            ></textarea>

            <button
              type="button"
              class="btn btn-main btn-full"
              @click="startSimulation"
              :disabled="!canSubmit || loading"
            >
              {{ loading ? 'Starting…' : 'Run simulation' }}
            </button>
            <p class="hint" v-if="!canSubmit">Add a document and describe the decision to start.</p>
          </div>
        </div>
      </section>

      <!-- What you get -->
      <section class="block">
        <h2>What you get</h2>
        <div class="gets">
          <div class="get">
            <h3>A report on likely responses</h3>
            <p>How each party reacts, and the second-order effects that follow.</p>
          </div>
          <div class="get">
            <h3>Where the parties disagree</h3>
            <p>The points of friction, and the reasons each side gives.</p>
          </div>
          <div class="get">
            <h3>A way to question the results</h3>
            <p>Ask the analyst, or any simulated party, why it acted as it did.</p>
          </div>
        </div>
      </section>

      <!-- Fit -->
      <section class="block">
        <h2>Use it to inform judgement</h2>
        <div class="fit">
          <div class="fit-card yes">
            <h3>Good for</h3>
            <ul>
              <li>Testing a policy, plan or message before release</li>
              <li>Finding reactions and knock-on effects you did not expect</li>
              <li>Preparing better questions for real consultation</li>
            </ul>
          </div>
          <div class="fit-card no">
            <h3>Not for</h3>
            <ul>
              <li>Exact forecasts or numbers</li>
              <li>Replacing expert judgement or real consultation</li>
              <li>Sensitive or classified material. Your files go to an external AI service.</li>
            </ul>
          </div>
        </div>
        <p class="note">Each run uses paid AI services. A standard run costs about US$5. Leave the tab open while the simulation runs.</p>
      </section>

      <!-- History -->
      <section class="block history">
        <HistoryDatabase />
      </section>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import HistoryDatabase from '../components/HistoryDatabase.vue'
import LanguageSwitcher from '../components/LanguageSwitcher.vue'

const router = useRouter()

const formData = ref({ simulationRequirement: '' })
const files = ref([])
const loading = ref(false)
const error = ref('')
const isDragOver = ref(false)
const fileInput = ref(null)

const canSubmit = computed(() => {
  return formData.value.simulationRequirement.trim() !== '' && files.value.length > 0
})

const triggerFileInput = () => {
  if (!loading.value) fileInput.value?.click()
}
const handleFileSelect = (event) => {
  addFiles(Array.from(event.target.files))
}
const handleDragOver = () => {
  if (!loading.value) isDragOver.value = true
}
const handleDragLeave = () => {
  isDragOver.value = false
}
const handleDrop = (e) => {
  isDragOver.value = false
  if (loading.value) return
  addFiles(Array.from(e.dataTransfer.files))
}
const addFiles = (newFiles) => {
  const validFiles = newFiles.filter(file => {
    const ext = file.name.split('.').pop().toLowerCase()
    return ['pdf', 'md', 'txt'].includes(ext)
  })
  files.value.push(...validFiles)
}
const removeFile = (index) => {
  files.value.splice(index, 1)
}
const startSimulation = () => {
  if (!canSubmit.value || loading.value) return
  import('../store/pendingUpload.js').then(({ setPendingUpload }) => {
    setPendingUpload(files.value, formData.value.simulationRequirement)
    router.push({ name: 'Process', params: { projectId: 'new' } })
  })
}

// ---- Hero graphic: one decision branching into responses and outcomes ----
const motionOK = ref(false)
const root = { x: 40, y: 160 }
const mids = [{ x: 200, y: 70 }, { x: 200, y: 160 }, { x: 200, y: 250 }]
const ends = [
  { x: 390, y: 30 }, { x: 390, y: 100 },
  { x: 390, y: 135 }, { x: 390, y: 185, risk: true },
  { x: 390, y: 220 }, { x: 390, y: 290 }
]
const curve = (a, b) => {
  const mx = (a.x + b.x) / 2
  return `M${a.x},${a.y} C${mx},${a.y} ${mx},${b.y} ${b.x},${b.y}`
}
const paths = [
  ...mids.map((m, i) => ({ d: curve(root, m), delay: 0.3 + i * 0.15, risk: false })),
  ...ends.map((e, i) => ({ d: curve(mids[Math.floor(i / 2)], e), delay: 1.0 + i * 0.12, risk: !!e.risk }))
]
// full journeys root -> mid -> end, for the looping pulses
const pulsePaths = ends.map((e, i) => {
  const m = mids[Math.floor(i / 2)]
  const first = curve(root, m)
  const second = curve(m, e).replace(/^M[^C]+/, '')
  return { d: first + ' ' + second, dur: 3.2 + (i % 3) * 0.5, offset: i * 0.55, risk: !!e.risk }
})
const nodes = [
  { ...root, r: 9, kind: 'root', delay: 0.1 },
  ...mids.map((m, i) => ({ ...m, r: 7, kind: 'mid', delay: 0.8 + i * 0.15 })),
  ...ends.map((e, i) => ({ ...e, r: e.risk ? 7 : 5.5, kind: e.risk ? 'end risk' : 'end', delay: 1.6 + i * 0.12 }))
]

// ---- Steps light up in sequence as they scroll into view ----
const steps = [
  { title: 'Share a document', body: 'A policy paper, briefing, plan or news report that describes the situation.' },
  { title: 'Describe the decision', body: 'Say what you plan to do, and what you want to know about the response.' },
  { title: 'MiroFish maps the parties', body: 'It finds the people, firms and groups in your document and builds a simulated version of each, with its own interests and memory.' },
  { title: 'The scenario plays out', body: 'The parties react to your decision and to each other over many rounds. You can watch this happen.' },
  { title: 'Review, then question', body: 'An AI analyst writes up what happened. You can ask the analyst, or any single party, to explain its actions.' }
]
const stepEls = ref([])
const seen = ref(steps.map(() => false))
let observer = null

onMounted(() => {
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  motionOK.value = !reduce
  if (reduce || !('IntersectionObserver' in window)) {
    seen.value = steps.map(() => true)
    return
  }
  observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        const i = stepEls.value.indexOf(entry.target)
        if (i > -1) {
          setTimeout(() => { seen.value[i] = true }, i * 120)
          observer.unobserve(entry.target)
        }
      }
    })
  }, { threshold: 0.6 })
  stepEls.value.forEach(el => el && observer.observe(el))
})
onBeforeUnmount(() => observer && observer.disconnect())
</script>

<style scoped>
.home {
  --paper: #EEF1F4;
  --card: #FFFFFF;
  --ink: #1B2A3A;
  --muted: #56667A;
  --line: #CDD5DE;
  --teal: #2E8B7A;
  --amber: #C9822B;
  --red: #C4553C;
  min-height: 100vh;
  background: var(--paper);
  color: var(--ink);
  font-family: 'Inter', 'Segoe UI', Roboto, Arial, sans-serif;
  font-size: 1.0625rem;
  line-height: 1.6;
}
.home :focus-visible { outline: 3px solid var(--teal); outline-offset: 3px; }

.nav { display: flex; justify-content: space-between; align-items: center; max-width: 1040px; margin: 0 auto; padding: 20px 24px; }
.brand { font-weight: 800; font-size: 1.25rem; letter-spacing: -0.01em; }
.nav-right { display: flex; align-items: center; gap: 20px; }
.nav-link { color: var(--muted); text-decoration: none; font-size: 0.95rem; }
.nav-link:hover { color: var(--ink); }

.wrap { max-width: 1040px; margin: 0 auto; padding: 0 24px 64px; }

/* Hero */
.hero { display: grid; grid-template-columns: 1fr 1.05fr; gap: 48px; align-items: center; padding: 48px 0 56px; }
.hero h1 { font-size: clamp(2.2rem, 5vw, 3.4rem); line-height: 1.08; font-weight: 800; letter-spacing: -0.02em; margin: 0 0 20px; max-width: 13ch; }
.lede { color: var(--muted); max-width: 46ch; margin: 0 0 16px; font-size: 1.15rem; }
.lede-small { font-size: 1rem; color: var(--ink); margin-bottom: 28px; }
.hero-text > * { animation: rise 0.7s ease-out both; }
.hero-text > *:nth-child(2) { animation-delay: 0.08s; }
.hero-text > *:nth-child(3) { animation-delay: 0.16s; }
.hero-text > *:nth-child(4) { animation-delay: 0.24s; }
@keyframes rise { from { opacity: 0; transform: translateY(12px); } to { opacity: 1; transform: none; } }

.futures { margin: 0; }
.futures-svg { width: 100%; height: auto; display: block; overflow: visible; }
.branch {
  fill: none; stroke: var(--teal); stroke-width: 2; opacity: 0.55;
  stroke-dasharray: 400; stroke-dashoffset: 400;
  animation: draw 1s ease-out forwards;
}
.branch.risk { stroke: var(--amber); opacity: 0.9; stroke-width: 2.5; }
@keyframes draw { to { stroke-dashoffset: 0; } }
.node { transform-box: fill-box; transform-origin: center; transform: scale(0); animation: pop 0.45s cubic-bezier(.3,1.6,.5,1) forwards; }
.node.root { fill: var(--ink); }
.node.mid { fill: var(--card); stroke: var(--teal); stroke-width: 2.5; }
.node.end { fill: var(--teal); }
.node.end.risk { fill: var(--amber); animation: pop 0.45s cubic-bezier(.3,1.6,.5,1) forwards, glow 2.4s ease-in-out 2.4s infinite; }
@keyframes pop { to { transform: scale(1); } }
@keyframes glow { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.35); } }
.pulse { fill: var(--teal); }
.pulse.risk { fill: var(--amber); }
.futures-cols { display: flex; justify-content: space-between; font-size: 0.8rem; color: var(--muted); margin-top: 10px; }
.futures-cols span:nth-child(2) { text-align: center; }
.futures-cols span:last-child { text-align: right; }
.futures-caption { display: flex; align-items: center; gap: 8px; justify-content: center; font-size: 0.875rem; color: var(--muted); margin-top: 14px; }
.key { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.key-risk { background: var(--amber); }

/* Buttons */
.btn { display: inline-block; padding: 14px 24px; border-radius: 6px; font-weight: 700; font-size: 1rem; text-decoration: none; border: 0; cursor: pointer; font-family: inherit; transition: background 0.2s; }
.btn-main { background: var(--ink); color: #fff; }
.btn-main:hover:not(:disabled) { background: var(--teal); }
.btn-main:disabled { background: #C9D1DA; color: #6B7A8A; cursor: not-allowed; }
.btn-full { width: 100%; margin-top: 20px; }

/* Blocks */
.block { padding: 48px 0; border-top: 1px solid var(--line); }
.block h2 { font-size: 1.6rem; line-height: 1.2; margin: 0 0 24px; font-weight: 800; letter-spacing: -0.01em; }

/* Steps */
.start { display: grid; grid-template-columns: 1fr 1fr; gap: 48px; align-items: start; }
.steps { list-style: none; margin: 0; padding: 0; counter-reset: step; position: relative; }
.steps li { counter-increment: step; display: grid; grid-template-columns: 44px 1fr; gap: 14px; padding: 14px 0; position: relative; }
.steps li::before {
  content: counter(step);
  width: 32px; height: 32px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-weight: 800; font-size: 0.95rem;
  border: 2px solid var(--line); color: var(--muted); background: var(--paper);
  transition: background 0.4s, border-color 0.4s, color 0.4s;
  position: relative; z-index: 1;
}
.steps li:not(:last-child)::after {
  content: ''; position: absolute; left: 15px; top: 46px; bottom: -14px; width: 2px;
  background: var(--line);
}
.steps li.seen::before { background: var(--teal); border-color: var(--teal); color: #fff; }
.steps li.seen:not(:last-child)::after { background: linear-gradient(var(--teal), var(--line)); }
.steps h3 { margin: 4px 0 4px; font-size: 1.05rem; }
.steps p { margin: 0; color: var(--muted); font-size: 0.975rem; }

/* Form */
.form-card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 24px; }
.field-label { display: block; font-weight: 700; font-size: 0.95rem; margin: 0 0 8px; }
.field-label + .upload-zone { margin-bottom: 20px; }
.upload-zone { border: 2px dashed var(--line); border-radius: 6px; min-height: 140px; display: flex; align-items: center; justify-content: center; cursor: pointer; background: var(--paper); padding: 16px; transition: border-color 0.2s; }
.upload-zone:hover, .upload-zone.drag-over { border-color: var(--teal); }
.upload-zone.has-files { align-items: flex-start; }
.upload-empty { text-align: center; display: flex; flex-direction: column; gap: 4px; }
.upload-empty span { color: var(--muted); font-size: 0.9rem; }
.file-list { list-style: none; margin: 0; padding: 0; width: 100%; display: flex; flex-direction: column; gap: 8px; }
.file-item { display: flex; align-items: center; gap: 12px; background: var(--card); border: 1px solid var(--line); border-radius: 4px; padding: 8px 12px; font-size: 0.925rem; }
.file-name { flex: 1; overflow-wrap: anywhere; }
.remove-btn { background: none; border: 0; color: var(--red); cursor: pointer; font-family: inherit; font-size: 0.875rem; }
.question { width: 100%; border: 1px solid var(--line); border-radius: 6px; background: var(--paper); padding: 14px; font-family: inherit; font-size: 1rem; line-height: 1.5; resize: vertical; color: var(--ink); }
.question:focus { outline: 2px solid var(--teal); outline-offset: 1px; }
.hint { color: var(--muted); font-size: 0.875rem; margin: 10px 0 0; text-align: center; }

/* What you get */
.gets { display: grid; grid-template-columns: repeat(3, 1fr); gap: 32px; }
.get { border-top: 3px solid var(--teal); padding-top: 16px; }
.get h3 { margin: 0 0 6px; font-size: 1.05rem; }
.get p { margin: 0; color: var(--muted); font-size: 0.975rem; }

/* Fit */
.fit { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
.fit-card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 24px; }
.fit-card h3 { margin: 0 0 12px; font-size: 1.1rem; }
.fit-card.yes h3 { color: var(--teal); }
.fit-card.no h3 { color: var(--red); }
.fit-card ul { margin: 0; padding-left: 20px; }
.fit-card li { margin-bottom: 8px; }
.note { color: var(--muted); margin: 24px 0 0; max-width: 62ch; }

@media (max-width: 820px) {
  .hero, .start, .fit, .gets { grid-template-columns: 1fr; }
  .hero { padding: 24px 0 40px; gap: 32px; }
  .nav-right { gap: 12px; }
}

@media (prefers-reduced-motion: reduce) {
  .hero-text > *, .branch, .node, .node.end.risk { animation: none; }
  .branch { stroke-dashoffset: 0; }
  .node { transform: none; }
  .steps li::before { transition: none; }
}
</style>
