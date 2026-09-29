"""
Run-lifecycle invariants: the browser observes runs, the server owns them.

1. POST /start without ``force`` never disturbs an existing run (active or
   finished); it returns that run with ``adopted: true``.
2. Only an explicit ``force`` restarts (and only then are logs cleaned).
3. A run recorded as RUNNING whose process is gone is reported FAILED with a
   stable error code instead of "running" forever.
"""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from flask import Flask

from app.api import simulation as simulation_api
from app.services.simulation_manager import SimulationStatus
from app.services.simulation_runner import (
    RunnerStatus,
    SimulationRunState,
    SimulationRunner,
)


@pytest.fixture
def runner_env(monkeypatch, tmp_path):
    """Isolate the runner's persistence and in-memory registries."""
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(SimulationRunner, "_run_states", {})
    monkeypatch.setattr(SimulationRunner, "_processes", {})
    synced = []
    monkeypatch.setattr(
        SimulationRunner,
        "_sync_simulation_status",
        classmethod(lambda _cls, sim_id, status, error=None: synced.append((sim_id, status, error))),
    )
    monkeypatch.setattr(
        simulation_api.ZepGraphMemoryManager,
        "get_updater",
        classmethod(lambda _cls, _sim_id: None),
    )
    monkeypatch.setattr(
        "app.services.simulation_runner.ZepGraphMemoryManager.get_updater",
        classmethod(lambda _cls, _sim_id: None),
    )
    return SimpleNamespace(dir=tmp_path, synced=synced)


def _write_run_state(env, simulation_id, status, pid=None, **extra):
    sim_dir = env.dir / simulation_id
    sim_dir.mkdir(parents=True, exist_ok=True)
    (sim_dir / "run_state.json").write_text(
        json.dumps(
            {
                "simulation_id": simulation_id,
                "runner_status": status,
                "current_round": 7,
                "total_rounds": 40,
                "process_pid": pid,
                **extra,
            }
        ),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# reconcile_run_state
# --------------------------------------------------------------------------

def test_dead_process_marks_running_run_failed_and_persists(runner_env, monkeypatch):
    _write_run_state(runner_env, "sim-dead", "running", pid=424242)
    monkeypatch.setattr(SimulationRunner, "_pid_is_alive", staticmethod(lambda _pid: False))

    state = SimulationRunner.reconcile_run_state("sim-dead")

    assert state.runner_status == RunnerStatus.FAILED
    assert state.error_code == "SIMULATION_WORKER_LOST"
    assert state.completed_at
    assert state.twitter_running is False and state.reddit_running is False
    # completed work is preserved
    assert state.current_round == 7 and state.total_rounds == 40
    # persisted, so a fresh process sees the same answer
    on_disk = json.loads((runner_env.dir / "sim-dead" / "run_state.json").read_text())
    assert on_disk["runner_status"] == "failed"
    assert on_disk["error_code"] == "SIMULATION_WORKER_LOST"
    assert runner_env.synced[-1][:2] == ("sim-dead", RunnerStatus.FAILED)


def test_live_process_keeps_running_state(runner_env, monkeypatch):
    _write_run_state(runner_env, "sim-live", "running", pid=424242)
    monkeypatch.setattr(SimulationRunner, "_pid_is_alive", staticmethod(lambda _pid: True))

    state = SimulationRunner.reconcile_run_state("sim-live")

    assert state.runner_status == RunnerStatus.RUNNING
    assert state.error_code is None


def test_process_owned_by_this_api_is_never_demoted(runner_env, monkeypatch):
    _write_run_state(runner_env, "sim-owned", "running", pid=424242)
    SimulationRunner._processes["sim-owned"] = object()
    monkeypatch.setattr(SimulationRunner, "_pid_is_alive", staticmethod(lambda _pid: False))

    assert SimulationRunner.reconcile_run_state("sim-owned").runner_status == RunnerStatus.RUNNING


@pytest.mark.parametrize("status", ["starting", "stopping", "completed", "stopped", "failed"])
def test_only_running_or_paused_runs_are_reconciled(runner_env, monkeypatch, status):
    _write_run_state(runner_env, "sim-x", status, pid=424242)
    monkeypatch.setattr(SimulationRunner, "_pid_is_alive", staticmethod(lambda _pid: False))

    assert SimulationRunner.reconcile_run_state("sim-x").runner_status.value == status


def test_run_without_recorded_pid_is_left_alone(runner_env, monkeypatch):
    _write_run_state(runner_env, "sim-nopid", "running", pid=None)
    monkeypatch.setattr(SimulationRunner, "_pid_is_alive", staticmethod(lambda _pid: False))

    assert SimulationRunner.reconcile_run_state("sim-nopid").runner_status == RunnerStatus.RUNNING


def test_missing_run_state_returns_none(runner_env):
    assert SimulationRunner.reconcile_run_state("sim-none") is None


def test_pid_probe_distinguishes_live_and_reaped_processes():
    assert SimulationRunner._pid_is_alive(None) is False
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    live_check_pid = child.pid
    child.wait()
    assert SimulationRunner._pid_is_alive(live_check_pid) is False
    import os
    assert SimulationRunner._pid_is_alive(os.getpid()) is True


# --------------------------------------------------------------------------
# POST /start  (adopt vs. restart)
# --------------------------------------------------------------------------

@pytest.fixture
def start_env(monkeypatch):
    calls = SimpleNamespace(start=[], stop=[], cleanup=[], reconcile=[])
    holder = SimpleNamespace(
        sim=SimpleNamespace(
            simulation_id="sim-1",
            project_id="proj-1",
            graph_id="graph-1",
            status=SimulationStatus.COMPLETED,
        ),
        run=None,
    )
    monkeypatch.setattr(
        simulation_api,
        "SimulationManager",
        lambda: SimpleNamespace(
            get_simulation=lambda _sim_id: holder.sim,
            _save_simulation_state=lambda _state: None,
        ),
    )
    monkeypatch.setattr(
        simulation_api, "_check_simulation_prepared", lambda _sim_id: (True, {})
    )

    def reconcile(_cls, sim_id):
        calls.reconcile.append(sim_id)
        return holder.run

    monkeypatch.setattr(SimulationRunner, "reconcile_run_state", classmethod(reconcile))
    monkeypatch.setattr(SimulationRunner, "get_run_state", classmethod(lambda _cls, _s: holder.run))
    monkeypatch.setattr(
        SimulationRunner,
        "start_simulation",
        classmethod(
            lambda _cls, **kwargs: calls.start.append(kwargs)
            or SimulationRunState(
                simulation_id=kwargs["simulation_id"],
                runner_status=RunnerStatus.RUNNING,
                total_rounds=40,
            )
        ),
    )
    monkeypatch.setattr(
        SimulationRunner,
        "stop_simulation",
        classmethod(
            lambda _cls, sim_id: calls.stop.append(sim_id)
            or SimulationRunState(simulation_id=sim_id, runner_status=RunnerStatus.STOPPED)
        ),
    )
    monkeypatch.setattr(
        SimulationRunner,
        "cleanup_simulation_logs",
        classmethod(lambda _cls, sim_id: calls.cleanup.append(sim_id) or {"success": True}),
    )
    return SimpleNamespace(calls=calls, holder=holder)


def _post_start(**body):
    app = Flask(__name__)
    with app.test_request_context(
        "/api/simulation/start",
        method="POST",
        json={"simulation_id": "sim-1", **body},
    ):
        result = simulation_api.start_simulation()
    response, status = result if isinstance(result, tuple) else (result, 200)
    return status, response.get_json()


@pytest.mark.parametrize(
    "runner_status,sim_status",
    [
        (RunnerStatus.STARTING, SimulationStatus.RUNNING),
        (RunnerStatus.RUNNING, SimulationStatus.RUNNING),
        (RunnerStatus.PAUSED, SimulationStatus.PAUSED),
        (RunnerStatus.STOPPING, SimulationStatus.STOPPING),
        (RunnerStatus.COMPLETED, SimulationStatus.COMPLETED),
        (RunnerStatus.STOPPED, SimulationStatus.STOPPED),
        (RunnerStatus.FAILED, SimulationStatus.FAILED),
    ],
)
def test_start_without_force_adopts_existing_run(start_env, runner_status, sim_status):
    start_env.holder.sim.status = sim_status
    start_env.holder.run = SimulationRunState(
        simulation_id="sim-1",
        runner_status=runner_status,
        current_round=18,
        total_rounds=40,
        error="boom" if runner_status == RunnerStatus.FAILED else None,
    )

    status, body = _post_start()

    assert status == 200 and body["success"] is True
    assert body["data"]["adopted"] is True
    assert body["data"]["force_restarted"] is False
    assert body["data"]["runner_status"] == runner_status.value
    assert body["data"]["current_round"] == 18
    # the existing run and its data are untouched
    assert start_env.calls.start == []
    assert start_env.calls.stop == []
    assert start_env.calls.cleanup == []


def test_start_without_force_begins_a_run_for_a_freshly_prepared_simulation(start_env):
    start_env.holder.sim.status = SimulationStatus.READY
    start_env.holder.run = None

    status, body = _post_start()

    assert status == 200
    assert body["data"]["adopted"] is False
    assert len(start_env.calls.start) == 1
    assert start_env.calls.cleanup == []


def test_explicit_force_still_restarts_a_finished_run(start_env):
    start_env.holder.run = SimulationRunState(
        simulation_id="sim-1", runner_status=RunnerStatus.COMPLETED
    )

    status, body = _post_start(force=True)

    assert status == 200
    assert body["data"]["adopted"] is False
    assert body["data"]["force_restarted"] is True
    assert start_env.calls.cleanup == ["sim-1"]
    assert len(start_env.calls.start) == 1


def test_explicit_force_stops_an_active_run_before_restarting(start_env):
    start_env.holder.sim.status = SimulationStatus.RUNNING
    start_env.holder.run = SimulationRunState(
        simulation_id="sim-1", runner_status=RunnerStatus.RUNNING
    )

    status, body = _post_start(force=True)

    assert status == 200
    assert start_env.calls.stop == ["sim-1"]
    assert start_env.calls.cleanup == ["sim-1"]
    assert body["data"]["force_restarted"] is True


# --------------------------------------------------------------------------
# GET /run-status
# --------------------------------------------------------------------------

def test_run_status_reports_phantom_run_as_failed_with_code(runner_env, monkeypatch):
    _write_run_state(runner_env, "sim-dead", "running", pid=424242)
    monkeypatch.setattr(SimulationRunner, "_pid_is_alive", staticmethod(lambda _pid: False))

    app = Flask(__name__)
    with app.test_request_context("/api/simulation/sim-dead/run-status"):
        response = simulation_api.get_run_status("sim-dead")

    data = response.get_json()["data"]
    assert data["runner_status"] == "failed"
    assert data["error_code"] == "SIMULATION_WORKER_LOST"
    assert data["current_round"] == 7


def test_run_status_is_idle_when_nothing_ever_ran(runner_env):
    app = Flask(__name__)
    with app.test_request_context("/api/simulation/sim-new/run-status"):
        response = simulation_api.get_run_status("sim-new")

    assert response.get_json()["data"]["runner_status"] == "idle"
