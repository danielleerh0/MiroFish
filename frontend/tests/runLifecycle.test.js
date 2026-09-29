import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

import {
  decideEntryAction,
  buildStartParams,
  failureMessageKey
} from '../src/utils/runLifecycle.js'

const here = dirname(fileURLToPath(import.meta.url))

test('an in-flight run is adopted, whether or not the user just pressed Run', () => {
  for (const runnerStatus of ['starting', 'running', 'paused', 'stopping']) {
    assert.equal(decideEntryAction({ runnerStatus }), 'adopt')
    assert.equal(decideEntryAction({ runnerStatus, launchRequested: true }), 'adopt')
  }
})

test('a finished run is shown, never restarted, even when launch is requested', () => {
  for (const runnerStatus of ['completed', 'stopped']) {
    assert.equal(decideEntryAction({ runnerStatus }), 'show_result')
    assert.equal(decideEntryAction({ runnerStatus, launchRequested: true }), 'show_result')
  }
})

test('a failed run is shown as failed, never restarted', () => {
  assert.equal(decideEntryAction({ runnerStatus: 'failed' }), 'show_failure')
  assert.equal(
    decideEntryAction({ runnerStatus: 'failed', launchRequested: true }),
    'show_failure'
  )
})

test('opening a page for a run that never started only offers to start it', () => {
  for (const runnerStatus of ['idle', undefined, null, 'something-new']) {
    assert.equal(decideEntryAction({ runnerStatus }), 'offer_start')
  }
})

test('a run starts implicitly only on the explicit launch signal', () => {
  assert.equal(decideEntryAction({ runnerStatus: 'idle', launchRequested: true }), 'start')
})

test('start params never carry force unless it is exactly true', () => {
  const base = { simulationId: 'sim_1', maxRounds: 40 }
  assert.equal('force' in buildStartParams(base), false)
  assert.equal('force' in buildStartParams({ ...base, force: false }), false)
  for (const truthyButNotTrue of ['true', 1, {}, 'yes']) {
    assert.equal('force' in buildStartParams({ ...base, force: truthyButNotTrue }), false)
  }
  assert.equal(buildStartParams({ ...base, force: true }).force, true)
})

test('start params keep the fields the backend requires', () => {
  const params = buildStartParams({ simulationId: 'sim_1', maxRounds: 12 })
  assert.equal(params.simulation_id, 'sim_1')
  assert.equal(params.max_rounds, 12)
  assert.equal(params.enable_graph_memory_update, true)
  assert.equal('max_rounds' in buildStartParams({ simulationId: 'sim_1' }), false)
})

test('known error codes map to a localized message key', () => {
  assert.equal(failureMessageKey('SIMULATION_WORKER_LOST'), 'step3.workerLost')
  assert.equal(failureMessageKey('UNKNOWN'), null)
  assert.equal(failureMessageKey(undefined), null)
})

// Structural guard. The frontend has no component-test runner, so pin the two
// hazards that caused runs to be destroyed by merely opening the page.
const step3 = readFileSync(resolve(here, '../src/components/Step3Simulation.vue'), 'utf8')

test('Step3Simulation sends force only from the confirmed restart handler', () => {
  const occurrences = step3.match(/force\s*:\s*true/g) || []
  assert.equal(occurrences.length, 1, 'force: true must appear exactly once')

  const handler = step3.match(/const handleRestartClick = \(\) => \{[\s\S]*?\n\}\n/)
  assert.ok(handler, 'handleRestartClick not found')
  assert.match(handler[0], /force\s*:\s*true/)
  // the confirmation must come before the forced start
  assert.ok(
    handler[0].indexOf('window.confirm') !== -1 &&
      handler[0].indexOf('window.confirm') < handler[0].indexOf('force'),
    'restart must be confirmed before force is sent'
  )
})

test('Step3Simulation does not start a run directly from onMounted', () => {
  const mounted = step3.match(/onMounted\(\(\) => \{[\s\S]*?\n\}\)/)
  assert.ok(mounted, 'onMounted block not found')
  assert.doesNotMatch(mounted[0], /doStartSimulation/)
  assert.match(mounted[0], /attachToRun/)
})

const runView = readFileSync(resolve(here, '../src/views/SimulationRunView.vue'), 'utf8')

test('leaving the run page never stops a run', () => {
  const goBack = runView.match(/const handleGoBack = async \(\) => \{[\s\S]*?\n\}\n/)
  assert.ok(goBack, 'handleGoBack not found')
  // Stopping/closing is only permitted on the not-simulating branch.
  const beforeGuard = goBack[0].split('isSimulating.value')[0]
  assert.doesNotMatch(beforeGuard, /stopSimulation\(|closeSimulationEnv\(/)
})
