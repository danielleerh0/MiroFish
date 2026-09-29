/**
 * Run-lifecycle decisions for the run page.
 *
 * The browser observes runs; the server owns them. Everything here is a pure
 * function so the rules can be tested without a DOM.
 */

const ACTIVE = new Set(['starting', 'running', 'paused', 'stopping'])
const FINISHED = new Set(['completed', 'stopped'])

/**
 * What the run page should do when it opens, given the server's view of the run.
 *
 *  adopt         run is in flight   -> attach and poll, never restart
 *  show_result   run finished       -> render results, no polling, no restart
 *  show_failure  run failed         -> render the failure, no polling, no restart
 *  start         nothing has run and the user just pressed "Run" (launch flag)
 *  offer_start   nothing has run and the page was merely opened (refresh, bookmark)
 */
export function decideEntryAction({ runnerStatus, launchRequested = false }) {
  if (ACTIVE.has(runnerStatus)) return 'adopt'
  if (FINISHED.has(runnerStatus)) return 'show_result'
  if (runnerStatus === 'failed') return 'show_failure'
  return launchRequested ? 'start' : 'offer_start'
}

/**
 * Body for POST /api/simulation/start. ``force`` is only ever sent when the
 * caller passes exactly ``force === true``, i.e. after an explicit,
 * confirmed user action. It is never a default.
 */
export function buildStartParams({ simulationId, maxRounds, force = false }) {
  const params = {
    simulation_id: simulationId,
    platform: 'parallel',
    enable_graph_memory_update: true
  }
  if (maxRounds) params.max_rounds = maxRounds
  if (force === true) params.force = true
  return params
}

const FAILURE_MESSAGE_KEYS = {
  SIMULATION_WORKER_LOST: 'step3.workerLost'
}

/** i18n key for a stable server error code, or null when there is none. */
export function failureMessageKey(errorCode) {
  return FAILURE_MESSAGE_KEYS[errorCode] || null
}
