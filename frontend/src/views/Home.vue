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
        <div>
          <h1>Test a decision on a crowd before you make it</h1>
          <p class="lede">
            Give MiroFish a document and a question. It builds a crowd of AI characters from
            the people and groups in that document, lets them react to each other, and writes
            up what happened.
          </p>
          <a href="#start" class="btn btn-main">Start a simulation</a>
        </div>
        <div aria-hidden="true">
          <svg ref="crowdSvg" class="crowd" viewBox="0 0 400 300"></svg>
          <p class="crowd-caption">One crowd, two camps. MiroFish shows you where people split.</p>
        </div>
      </section>

      <!-- Start form + steps -->
      <section class="block start" id="start">
        <div class="steps-col">
          <h2>How it works</h2>
          <ol class="steps">
            <li><div><h3>Share a document</h3><p>A report, news article or policy paper. PDF, Markdown or plain text.</p></div></li>
            <li><div><h3>Ask a question</h3><p>Write it in plain words, like you would ask a colleague.</p></div></li>
            <li><div><h3>MiroFish builds the crowd</h3><p>It finds the people, firms and groups in your document and gives each an AI character with its own view and memory.</p></div></li>
            <li><div><h3>The crowd reacts</h3><p>The characters post, reply and argue on two simulated social platforms over many rounds.</p></div></li>
            <li><div><h3>Read the report and ask follow-ups</h3><p>An AI analyst writes a report. You can then chat with the analyst or any single character.</p></div></li>
          </ol>
        </div>

        <div class="form-col">
          <h2>Start a simulation</h2>
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
                <span>PDF, MD or TXT. You can add more than one.</span>
              </div>
              <ul v-else class="file-list">
                <li v-for="(file, index) in files" :key="index" class="file-item">
                  <span class="file-name">{{ file.name }}</span>
                  <button type="button" class="remove-btn" @click.stop="removeFile(index)" :aria-label="'Remove ' + file.name">Remove</button>
                </li>
              </ul>
            </div>

            <label class="field-label" for="question">Your question</label>
            <textarea
              id="question"
              v-model="formData.simulationRequirement"
              class="question"
              placeholder="Example: How will small logistics firms react if this grant ends next year?"
              rows="5"
              :disabled="loading"
            ></textarea>

            <button
              type="button"
              class="btn btn-main btn-full"
              @click="startSimulation"
              :disabled="!canSubmit || loading"
            >
              {{ loading ? 'Starting…' : 'Start simulation' }}
            </button>
            <p class="hint" v-if="!canSubmit">Add a document and a question to start.</p>
          </div>
        </div>
      </section>

      <!-- Fit -->
      <section class="block">
        <h2>What it is good for</h2>
        <div class="fit">
          <div class="fit-card yes">
            <h3>Use it to</h3>
            <ul>
              <li>See how different groups may respond to the same change</li>
              <li>Find reactions you did not expect</li>
              <li>Test a message or plan before you release it</li>
            </ul>
          </div>
          <div class="fit-card no">
            <h3>Do not use it to</h3>
            <ul>
              <li>Predict exact numbers or outcomes</li>
              <li>Replace human judgement or real consultation</li>
              <li>Process sensitive or classified material. Your files go to an external AI service.</li>
            </ul>
          </div>
        </div>
        <p class="note">Each run uses paid AI services. A standard run costs about US$5. Leave the tab open while the crowd reacts.</p>
      </section>

      <!-- History -->
      <section class="block history">
        <HistoryDatabase />
      </section>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
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
const crowdSvg = ref(null)

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

// Crowd graphic: dots start mixed, then drift into two camps once.
onMounted(() => {
  const svg = crowdSvg.value
  if (!svg) return
  const NS = 'http://www.w3.org/2000/svg'
  const colours = ['#2E8B7A', '#C4553C', '#8A97A6']
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  let seed = 7
  const rand = () => { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646 }
  const dots = []
  for (let i = 0; i < 90; i++) {
    const camp = i < 40 ? 0 : i < 80 ? 1 : 2
    const start = { x: 40 + rand() * 320, y: 30 + rand() * 240 }
    const cx = camp === 0 ? 110 : camp === 1 ? 290 : 200
    const spreadX = camp === 2 ? 30 : 120
    const spreadY = camp === 2 ? 200 : 150
    const end = { x: cx + (rand() - 0.5) * spreadX, y: 150 + (rand() - 0.5) * spreadY }
    const c = document.createElementNS(NS, 'circle')
    c.setAttribute('r', 4.5)
    c.setAttribute('fill', colours[camp])
    svg.appendChild(c)
    dots.push({ c, start, end })
  }
  const place = (t) => {
    const e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2
    for (const d of dots) {
      d.c.setAttribute('cx', d.start.x + (d.end.x - d.start.x) * e)
      d.c.setAttribute('cy', d.start.y + (d.end.y - d.start.y) * e)
    }
  }
  if (reduce) { place(1); return }
  place(0)
  let t0 = null
  const step = (ts) => {
    if (t0 === null) t0 = ts
    const t = Math.min(Math.max((ts - t0 - 600) / 2400, 0), 1)
    place(t)
    if (t < 1) requestAnimationFrame(step)
  }
  requestAnimationFrame(step)
})
</script>

<style scoped>
.home {
  --paper: #EEF1F4;
  --card: #FFFFFF;
  --ink: #1B2A3A;
  --muted: #56667A;
  --line: #CDD5DE;
  --green: #2E8B7A;
  --red: #C4553C;
  min-height: 100vh;
  background: var(--paper);
  color: var(--ink);
  font-family: 'Inter', 'Segoe UI', Roboto, Arial, sans-serif;
  font-size: 1.0625rem;
  line-height: 1.6;
}
.home :focus-visible { outline: 3px solid var(--green); outline-offset: 3px; }

.nav {
  display: flex; justify-content: space-between; align-items: center;
  max-width: 1040px; margin: 0 auto; padding: 20px 24px;
}
.brand { font-weight: 800; font-size: 1.25rem; letter-spacing: -0.01em; }
.nav-right { display: flex; align-items: center; gap: 20px; }
.nav-link { color: var(--muted); text-decoration: none; font-size: 0.95rem; }
.nav-link:hover { color: var(--ink); }

.wrap { max-width: 1040px; margin: 0 auto; padding: 0 24px 64px; }

.hero { display: grid; grid-template-columns: 1.1fr 1fr; gap: 48px; align-items: center; padding: 48px 0 56px; }
.hero h1 { font-size: clamp(2.2rem, 5vw, 3.4rem); line-height: 1.08; font-weight: 800; letter-spacing: -0.02em; margin: 0 0 20px; max-width: 14ch; }
.lede { color: var(--muted); max-width: 46ch; margin: 0 0 28px; font-size: 1.15rem; }
.crowd { width: 100%; height: auto; display: block; }
.crowd-caption { font-size: 0.875rem; color: var(--muted); margin: 8px 0 0; text-align: center; }

.btn { display: inline-block; padding: 14px 24px; border-radius: 6px; font-weight: 700; font-size: 1rem; text-decoration: none; border: 0; cursor: pointer; font-family: inherit; }
.btn-main { background: var(--ink); color: #fff; }
.btn-main:hover:not(:disabled) { background: var(--green); }
.btn-main:disabled { background: #C9D1DA; color: #6B7A8A; cursor: not-allowed; }
.btn-full { width: 100%; margin-top: 20px; }

.block { padding: 48px 0; border-top: 1px solid var(--line); }
.block h2 { font-size: 1.6rem; line-height: 1.2; margin: 0 0 24px; font-weight: 800; letter-spacing: -0.01em; }

.start { display: grid; grid-template-columns: 1fr 1fr; gap: 48px; align-items: start; }
.steps { list-style: none; margin: 0; padding: 0; counter-reset: step; }
.steps li { counter-increment: step; display: grid; grid-template-columns: 44px 1fr; gap: 12px; padding: 16px 0; border-bottom: 1px solid var(--line); }
.steps li:last-child { border-bottom: 0; }
.steps li::before { content: counter(step); font-size: 1.5rem; font-weight: 800; color: var(--green); line-height: 1.1; }
.steps h3 { margin: 0 0 4px; font-size: 1.05rem; }
.steps p { margin: 0; color: var(--muted); font-size: 0.975rem; }

.form-card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 24px; }
.field-label { display: block; font-weight: 700; font-size: 0.95rem; margin: 0 0 8px; }
.field-label + .upload-zone { margin-bottom: 20px; }
.upload-zone {
  border: 2px dashed var(--line); border-radius: 6px; min-height: 140px;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer; background: var(--paper); padding: 16px;
}
.upload-zone:hover, .upload-zone.drag-over { border-color: var(--green); }
.upload-zone.has-files { align-items: flex-start; }
.upload-empty { text-align: center; display: flex; flex-direction: column; gap: 4px; }
.upload-empty span { color: var(--muted); font-size: 0.9rem; }
.file-list { list-style: none; margin: 0; padding: 0; width: 100%; display: flex; flex-direction: column; gap: 8px; }
.file-item { display: flex; align-items: center; gap: 12px; background: var(--card); border: 1px solid var(--line); border-radius: 4px; padding: 8px 12px; font-size: 0.925rem; }
.file-name { flex: 1; overflow-wrap: anywhere; }
.remove-btn { background: none; border: 0; color: var(--red); cursor: pointer; font-family: inherit; font-size: 0.875rem; }
.question {
  width: 100%; border: 1px solid var(--line); border-radius: 6px; background: var(--paper);
  padding: 14px; font-family: inherit; font-size: 1rem; line-height: 1.5; resize: vertical; color: var(--ink);
}
.question:focus { outline: 2px solid var(--green); outline-offset: 1px; }
.hint { color: var(--muted); font-size: 0.875rem; margin: 10px 0 0; text-align: center; }

.fit { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
.fit-card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 24px; }
.fit-card h3 { margin: 0 0 12px; font-size: 1.1rem; }
.fit-card.yes h3 { color: var(--green); }
.fit-card.no h3 { color: var(--red); }
.fit-card ul { margin: 0; padding-left: 20px; }
.fit-card li { margin-bottom: 8px; }
.note { color: var(--muted); margin: 24px 0 0; max-width: 62ch; }

@media (max-width: 820px) {
  .hero, .start, .fit { grid-template-columns: 1fr; }
  .hero { padding: 24px 0 40px; gap: 24px; }
  .nav-right { gap: 12px; }
}
</style>
