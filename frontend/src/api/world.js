import service from './index'

// World model API (v2). All calls resolve to { success, data } via the shared interceptor.

export const listScenarios = () => service.get('/api/world/scenarios')
export const createScenario = (data) => service.post('/api/world/scenarios', data)
export const getVersion = (id) => service.get(`/api/world/versions/${id}`)
export const approveVersion = (id) => service.post(`/api/world/versions/${id}/approve`)
export const newVersion = (id, note) => service.post(`/api/world/versions/${id}/new-version`, { note })
export const reviewRow = (table, id, decision, edits) =>
  service.patch(`/api/world/rows/${table}/${id}`, { decision, edits })

export const loadRt05Fixture = (review = false) => service.post('/api/world/fixtures/rt05', { review })
export const getFixtureScripts = () => service.get('/api/world/fixtures/rt05/scripts')

function docPayload({ file, text, filename, terms }) {
  if (file) {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('terms', JSON.stringify(terms || []))
    return { data: fd, headers: { 'Content-Type': 'multipart/form-data' } }
  }
  return { data: { text, filename, terms } }
}

export const redactPreview = (input) => {
  const { data, headers } = docPayload(input)
  return service.post('/api/world/redact-preview', data, { headers })
}
export const addDocument = (versionId, input) => {
  const { data, headers } = docPayload(input)
  return service.post(`/api/world/versions/${versionId}/documents`, data, { headers })
}
export const extractWorld = (versionId, documentId, requirement) =>
  service.post(`/api/world/versions/${versionId}/extract`, { document_id: documentId, requirement })
export const importSpec = (versionId, spec) => service.post(`/api/world/versions/${versionId}/import`, { spec })

export const runSimulation = (versionId, actions, name) =>
  service.post(`/api/world/versions/${versionId}/simulations`, { actions, name })
export const getSimulation = (id) => service.get(`/api/world/simulations/${id}`)
export const replaySimulation = (id) => service.post(`/api/world/simulations/${id}/replay`)

// Injections (new information into a running world) and run continuation
export const createInjection = (versionId, { file, text, filename, terms, label }) => {
  if (file) {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('terms', JSON.stringify(terms || []))
    fd.append('label', label)
    return service.post(`/api/world/versions/${versionId}/injections`, fd, { headers: { 'Content-Type': 'multipart/form-data' } })
  }
  return service.post(`/api/world/versions/${versionId}/injections`, { text, filename, terms, label })
}
export const getInjection = (id) => service.get(`/api/world/injections/${id}`)
export const draftInjection = (id, autoAccept) => service.post(`/api/world/injections/${id}/draft`, { auto_accept: autoAccept })
export const importInjection = (id, spec, autoAccept) =>
  service.post(`/api/world/injections/${id}/import`, { spec, auto_accept: autoAccept })
export const reviewInjectionItem = (id, decision, edits) =>
  service.patch(`/api/world/injection-items/${id}`, { decision, edits })
export const finalizeInjection = (id) => service.post(`/api/world/injections/${id}/finalize`)
export const continueRun = (simId, body) => service.post(`/api/world/simulations/${simId}/continue`, body)
export const loadRt05Injections = (versionId) => service.post('/api/world/fixtures/rt05/injections', { version_id: versionId })
