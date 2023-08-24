import inspect
from functools import partial
from typing import TYPE_CHECKING, Dict, List, Optional, Union
from uuid import UUID, uuid4

import anyio
import anyio.abc
import pendulum
from pydantic import BaseModel

from prefect.client.orchestration import PrefectClient, get_client
from prefect.client.schemas.filters import (
    DeploymentFilter,
    DeploymentFilterId,
    FlowRunFilter,
    FlowRunFilterId,
    FlowRunFilterNextScheduledStartTime,
    FlowRunFilterState,
    FlowRunFilterStateName,
    FlowRunFilterStateType,
)
from prefect.client.schemas.objects import StateType
from prefect.deployments import Deployment
from prefect.engine import propose_state
from prefect.exceptions import (
    Abort,
    InfrastructureNotFound,
    ObjectNotFound,
)
from prefect.logging.loggers import PrefectLogAdapter, flow_run_logger, get_logger
from prefect.settings import (
    PREFECT_API_URL,
    PREFECT_WORKER_HEARTBEAT_SECONDS,
    PREFECT_WORKER_PREFETCH_SECONDS,
    PREFECT_WORKER_QUERY_SECONDS,
    get_current_settings,
)
from prefect.states import Crashed, Pending, exception_to_failed_state
from prefect.utilities.services import critical_service_loop

if TYPE_CHECKING:
    from prefect.client.schemas.objects import FlowRun

import asyncio
import os
import signal
import subprocess
import sys

import anyio
import anyio.abc
import sniffio

from prefect.utilities.processutils import run_process

if sys.platform == "win32":
    # exit code indicating that the process was terminated by Ctrl+C or Ctrl+Break
    STATUS_CONTROL_C_EXIT = 0xC000013A


def _use_threaded_child_watcher():
    if (
        sys.version_info < (3, 8)
        and sniffio.current_async_library() == "asyncio"
        and sys.platform != "win32"
    ):
        from prefect.utilities.compat import ThreadedChildWatcher

        # Python < 3.8 does not use a `ThreadedChildWatcher` by default which can
        # lead to errors in tests on unix as the previous default `SafeChildWatcher`
        # is not compatible with threaded event loops.
        asyncio.get_event_loop_policy().set_child_watcher(ThreadedChildWatcher())


def prepare_environment(flow_run: "FlowRun") -> Dict[str, str]:
    env = get_current_settings().to_environment_variables(exclude_unset=True)
    env.update({"PREFECT__FLOW_RUN_ID": flow_run.id.hex})
    env.update(**os.environ)  # is this really necessary??
    return env


class ProcessRunnerResult(BaseModel):
    identifier: str
    status_code: int

    def __bool__(self):
        return self.status_code == 0


class Runner:
    def __init__(
        self,
        name: Optional[str] = None,
        deployment_ids: List[str] = None,
        prefetch_seconds: Optional[float] = None,
        limit: Optional[int] = None,
        pause_on_shutdown: bool = True,
    ):
        """
        Responsible for managing the execution of remotely initiated flow runs.

        Args:
            name: The name of the runner. If not provided, a random one
                will be generated. If provided, it cannot contain '/' or '%'.
                The name is used to identify the runner in the UI; if two
                processes have the same name, they will be treated as the same
                runner.
            prefetch_seconds: The number of seconds to prefetch flow runs for.
            limit: The maximum number of flow runs this runner should be running at
                a given time.
            pause_on_shutdown: A boolean for whether or not to automatically pause
                deployment schedules on shutdown; defaults to `True`
        """
        if name and ("/" in name or "%" in name):
            raise ValueError("Runner name cannot contain '/' or '%'")
        self.name = name or f"{self.__class__.__name__} {uuid4()}"
        self.logger = get_logger()

        self.is_setup = False
        self.pause_on_shutdown = pause_on_shutdown
        self.deployment_ids = deployment_ids or []

        self._prefetch_seconds: float = (
            prefetch_seconds or PREFECT_WORKER_PREFETCH_SECONDS.value()
        )

        self._runs_task_group: Optional[anyio.abc.TaskGroup] = None
        self._client: Optional[PrefectClient] = None
        self._last_polled_time: pendulum.DateTime = pendulum.now("utc")
        self._limit = limit
        self._limiter: Optional[anyio.CapacityLimiter] = None
        self._submitting_flow_run_ids = set()
        self._cancelling_flow_run_ids = set()
        self._scheduled_task_scopes = set()
        self._flow_run_process_map = dict()

    def get_flow_run_logger(self, flow_run: "FlowRun") -> PrefectLogAdapter:
        return flow_run_logger(flow_run=flow_run).getChild(
            "runner",
            extra={
                "runner_name": self.name,
            },
        )

    async def run(
        self,
        flow_run: "FlowRun",
        task_status: Optional[anyio.abc.TaskStatus] = None,
    ):
        command = f"{sys.executable} -m prefect.engine"

        flow_run_logger = self.get_flow_run_logger(flow_run)

        # We must add creationflags to a dict so it is only passed as a function
        # parameter on Windows, because the presence of creationflags causes
        # errors on Unix even if set to None
        kwargs: Dict[str, object] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        _use_threaded_child_watcher()
        flow_run_logger.info("Opening process...")

        process = await run_process(
            command.split(" "),
            stream_output=True,
            task_status=task_status,
            env=prepare_environment(flow_run),
            **kwargs,
        )

        # Use the pid for display if no name was given
        display_name = f" {process.pid}"

        if process.returncode:
            help_message = None
            if process.returncode == -9:
                help_message = (
                    "This indicates that the process exited due to a SIGKILL signal. "
                    "Typically, this is either caused by manual cancellation or "
                    "high memory usage causing the operating system to "
                    "terminate the process."
                )
            if process.returncode == -15:
                help_message = (
                    "This indicates that the process exited due to a SIGTERM signal. "
                    "Typically, this is caused by manual cancellation."
                )
            elif process.returncode == 247:
                help_message = (
                    "This indicates that the process was terminated due to high "
                    "memory usage."
                )
            elif (
                sys.platform == "win32" and process.returncode == STATUS_CONTROL_C_EXIT
            ):
                help_message = (
                    "Process was terminated due to a Ctrl+C or Ctrl+Break signal. "
                    "Typically, this is caused by manual cancellation."
                )

            flow_run_logger.error(
                f"Process{display_name} exited with status code: {process.returncode}"
                + (f"; {help_message}" if help_message else "")
            )
        else:
            flow_run_logger.info(f"Process{display_name} exited cleanly.")

        return ProcessRunnerResult(
            status_code=process.returncode, identifier=str(process.pid)
        )

    async def kill_process(
        self,
        pid: int,
        grace_seconds: int = 30,
    ):
        # In a non-windows environment first send a SIGTERM, then, after
        # `grace_seconds` seconds have passed subsequent send SIGKILL. In
        # Windows we use CTRL_BREAK_EVENT as SIGTERM is useless:
        # https://bugs.python.org/issue26350
        if sys.platform == "win32":
            try:
                os.kill(pid, signal.CTRL_BREAK_EVENT)
            except (ProcessLookupError, WindowsError):
                raise InfrastructureNotFound(
                    f"Unable to kill process {pid!r}: The process was not found."
                )
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                raise InfrastructureNotFound(
                    f"Unable to kill process {pid!r}: The process was not found."
                )

            # Throttle how often we check if the process is still alive to keep
            # from making too many system calls in a short period of time.
            check_interval = max(grace_seconds / 10, 1)

            with anyio.move_on_after(grace_seconds):
                while True:
                    await anyio.sleep(check_interval)

                    # Detect if the process is still alive. If not do an early
                    # return as the process respected the SIGTERM from above.
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return

            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                # We shouldn't ever end up here, but it's possible that the
                # process ended right after the check above.
                return

    @classmethod
    def __dispatch_key__(cls):
        if cls.__name__ == "BaseRunner":
            return None  # The base class is abstract
        return cls.type

    async def setup(self):
        """Prepares the runner to run."""
        self.logger.debug("Setting up runner...")
        self._runs_task_group = anyio.create_task_group()
        self._limiter = (
            anyio.CapacityLimiter(self._limit) if self._limit is not None else None
        )
        self._client = get_client()
        await self._client.__aenter__()
        await self._runs_task_group.__aenter__()

        self.is_setup = True

    async def teardown(self, *exc_info):
        """Cleans up resources after the runner is stopped."""
        self.logger.debug("Tearing down runner...")
        if self.pause_on_shutdown:
            await self.pause_schedules()
        self.is_setup = False
        for scope in self._scheduled_task_scopes:
            scope.cancel()
        if self._runs_task_group:
            await self._runs_task_group.__aexit__(*exc_info)
        if self._client:
            await self._client.__aexit__(*exc_info)
        self._runs_task_group = None
        self._client = None

    async def pause_schedules(self):
        """
        Pauses all deployment schedules.
        """
        for deployment_id in self.deployment_ids:
            await self._client.update_schedule(deployment_id, active=False)

    def is_runner_still_polling(self, query_interval_seconds: int) -> bool:
        """
        This method is invoked by a webserver healthcheck handler
        and returns a boolean indicating if the runner has recorded a
        scheduled flow run poll within a variable amount of time.

        The `query_interval_seconds` is the same value that is used by
        the loop services - we will evaluate if the _last_polled_time
        was within that interval x 30 (so 10s -> 5m)

        The instance property `self._last_polled_time`
        is currently set/updated in `get_and_submit_flow_runs()`
        """
        threshold_seconds = query_interval_seconds * 30

        seconds_since_last_poll = (
            pendulum.now("utc") - self._last_polled_time
        ).in_seconds()

        is_still_polling = seconds_since_last_poll <= threshold_seconds

        if not is_still_polling:
            self.logger.error(
                f"Runner has not polled in the last {seconds_since_last_poll} seconds "
                "and should be restarted"
            )

        return is_still_polling

    async def get_and_submit_flow_runs(self):
        runs_response = await self._get_scheduled_flow_runs()

        self._last_polled_time = pendulum.now("utc")

        return await self._submit_scheduled_flow_runs(flow_run_response=runs_response)

    async def check_for_cancelled_flow_runs(self):
        if not self.is_setup:
            raise RuntimeError(
                "Runner is not set up. Please make sure you are running this runner "
                "as an async context manager."
            )

        # To stop loop service checking for cancelled runs.
        # Need to find a better way to stop runner spawned by
        # a worker.
        if not self._flow_run_process_map and not self.deployment_ids:
            raise Exception("No flow runs to watch for cancel.")

        self.logger.debug("Checking for cancelled flow runs...")

        named_cancelling_flow_runs = await self._client.read_flow_runs(
            flow_run_filter=FlowRunFilter(
                state=FlowRunFilterState(
                    type=FlowRunFilterStateType(any_=[StateType.CANCELLED]),
                    name=FlowRunFilterStateName(any_=["Cancelling"]),
                ),
                # Avoid duplicate cancellation calls
                id=FlowRunFilterId(
                    any_=list(
                        self._flow_run_process_map.keys()
                        - self._cancelling_flow_run_ids
                    )
                ),
            ),
        )

        typed_cancelling_flow_runs = await self._client.read_flow_runs(
            flow_run_filter=FlowRunFilter(
                state=FlowRunFilterState(
                    type=FlowRunFilterStateType(any_=[StateType.CANCELLING]),
                ),
                # Avoid duplicate cancellation calls
                id=FlowRunFilterId(
                    any_=list(
                        self._flow_run_process_map.keys()
                        - self._cancelling_flow_run_ids
                    )
                ),
            ),
        )

        cancelling_flow_runs = named_cancelling_flow_runs + typed_cancelling_flow_runs

        if cancelling_flow_runs:
            self.logger.info(
                f"Found {len(cancelling_flow_runs)} flow runs awaiting cancellation."
            )

        for flow_run in cancelling_flow_runs:
            self._cancelling_flow_run_ids.add(flow_run.id)
            self._runs_task_group.start_soon(self.cancel_run, flow_run)

        return cancelling_flow_runs

    async def cancel_run(self, flow_run: "FlowRun"):
        run_logger = self.get_flow_run_logger(flow_run)

        pid = self._flow_run_process_map.get(flow_run.id)
        if not pid:
            await self._mark_flow_run_as_cancelled(
                flow_run,
                state_updates={
                    "message": (
                        "Could not find process ID for flow run"
                        " and cancellation cannot be guaranteed."
                    )
                },
            )
            return

        try:
            await self.kill_process(pid)
        except InfrastructureNotFound as exc:
            self.logger.warning(f"{exc} Marking flow run as cancelled.")
            await self._mark_flow_run_as_cancelled(flow_run)
        except Exception:
            run_logger.exception(
                "Encountered exception while killing process for flow run "
                f"'{flow_run.id}'. Flow run may not be cancelled."
            )
            # We will try again on generic exceptions
            self._cancelling_flow_run_ids.remove(flow_run.id)
            return
        else:
            await self._mark_flow_run_as_cancelled(flow_run)
            run_logger.info(f"Cancelled flow run '{flow_run.id}'!")

    async def _send_runner_heartbeat(self):
        """
        Will need to reconsider how to heartbeat a runner for crash detection.
        """
        pass

    async def sync_with_backend(self):
        """
        Sends a runner heartbeat to the API.
        """
        await self._send_runner_heartbeat()

        self.logger.debug("Runner synchronized with the Prefect API server.")

    async def _get_scheduled_flow_runs(
        self,
    ) -> List["FlowRun"]:
        """
        Retrieve scheduled flow runs for this runner.
        """
        scheduled_before = pendulum.now("utc").add(seconds=int(self._prefetch_seconds))
        self.logger.debug(f"Querying for flow runs scheduled before {scheduled_before}")

        scheduled_flow_runs = await self._client.read_flow_runs(
            deployment_filter=DeploymentFilter(
                id=DeploymentFilterId(any_=list(self.deployment_ids))
            ),
            flow_run_filter=FlowRunFilter(
                next_scheduled_start_time=FlowRunFilterNextScheduledStartTime(
                    before_=scheduled_before
                ),
                state=FlowRunFilterState(
                    type=FlowRunFilterStateType(any_=[StateType.SCHEDULED]),
                ),
                # possible unnecessary
                id=FlowRunFilterId(not_any_=list(self._submitting_flow_run_ids)),
            ),
        )
        self.logger.debug(f"Discovered {len(scheduled_flow_runs)} scheduled_flow_runs")
        return scheduled_flow_runs

    async def _submit_scheduled_flow_runs(
        self, flow_run_response: List["FlowRun"]
    ) -> List["FlowRun"]:
        """
        Takes a list of FlowRuns and submits the referenced flow runs
        for execution by the runner.
        """
        submittable_flow_runs = flow_run_response
        submittable_flow_runs.sort(key=lambda run: run.next_scheduled_start_time)
        for flow_run in submittable_flow_runs:
            if flow_run.id in self._submitting_flow_run_ids:
                continue

            try:
                if self._limiter:
                    self._limiter.acquire_on_behalf_of_nowait(flow_run.id)
            except anyio.WouldBlock:
                self.logger.info(
                    f"Flow run limit reached; {self._limiter.borrowed_tokens} flow runs"
                    " in progress."
                )
                break
            else:
                run_logger = self.get_flow_run_logger(flow_run)
                run_logger.info(
                    f"Runner '{self.name}' submitting flow run '{flow_run.id}'"
                )
                self._submitting_flow_run_ids.add(flow_run.id)
                self._runs_task_group.start_soon(
                    self._submit_run,
                    flow_run,
                )

        return list(
            filter(
                lambda run: run.id in self._submitting_flow_run_ids,
                submittable_flow_runs,
            )
        )

    async def _check_flow_run(self, flow_run: "FlowRun") -> None:
        """
        Performs a check on a submitted flow run to warn the user if the flow run
        was created from a deployment with a storage block.
        """
        if flow_run.deployment_id:
            deployment = await self._client.read_deployment(flow_run.deployment_id)
            if deployment.storage_document_id:
                raise ValueError(
                    f"Flow run {flow_run.id!r} was created from deployment"
                    f" {deployment.name!r} which is configured with a storage block."
                    " Runners currently only support local storage. Please use an"
                    " agent to execute this flow run."
                )

    async def execute_flow_run(self, flow_run_id: UUID):
        async with self as runner:
            async with anyio.create_task_group() as tg:
                self._submitting_flow_run_ids.add(flow_run_id)
                flow_run = await runner._client.read_flow_run(flow_run_id)

                pid = await runner._runs_task_group.start(
                    self._submit_run_and_capture_errors, flow_run
                )

                self._flow_run_process_map[flow_run.id] = pid

                tg.start_soon(
                    partial(
                        critical_service_loop,
                        workload=runner.check_for_cancelled_flow_runs,
                        interval=PREFECT_WORKER_QUERY_SECONDS.value() * 2,
                        jitter_range=0.3,
                    )
                )

    async def _submit_run(self, flow_run: "FlowRun") -> None:
        """
        Submits a given flow run for execution by the runner.
        """
        run_logger = self.get_flow_run_logger(flow_run)

        try:
            await self._check_flow_run(flow_run)
        except (ValueError, ObjectNotFound):
            self.logger.exception(
                (
                    "Flow run %s did not pass checks and will not be submitted for"
                    " execution"
                ),
                flow_run.id,
            )
            self._submitting_flow_run_ids.remove(flow_run.id)
            return

        ready_to_submit = await self._propose_pending_state(flow_run)

        if ready_to_submit:
            readiness_result = await self._runs_task_group.start(
                self._submit_run_and_capture_errors, flow_run
            )

            if readiness_result and not isinstance(readiness_result, Exception):
                self._flow_run_process_map[flow_run.id] = readiness_result

            run_logger.info(f"Completed submission of flow run '{flow_run.id}'")

        else:
            # If the run is not ready to submit, release the concurrency slot
            if self._limiter:
                self._limiter.release_on_behalf_of(flow_run.id)

        self._submitting_flow_run_ids.remove(flow_run.id)

    async def _submit_run_and_capture_errors(
        self, flow_run: "FlowRun", task_status: anyio.abc.TaskStatus = None
    ) -> Union[ProcessRunnerResult, Exception]:
        run_logger = self.get_flow_run_logger(flow_run)

        try:
            result = await self.run(
                flow_run=flow_run,
                task_status=task_status,
            )
        except Exception as exc:
            if not task_status._future.done():
                # This flow run was being submitted and did not start successfully
                run_logger.exception(
                    f"Failed to start proces for flow run '{flow_run.id}'."
                )
                # Mark the task as started to prevent agent crash
                task_status.started(exc)
                await self._propose_crashed_state(
                    flow_run, "Flow run process could not be started"
                )
            else:
                run_logger.exception(
                    f"An error occurred while monitoring flow run '{flow_run.id}'. "
                    "The flow run will not be marked as failed, but an issue may have "
                    "occurred."
                )
            return exc
        finally:
            if self._limiter:
                self._limiter.release_on_behalf_of(flow_run.id)
            self._flow_run_process_map.pop(flow_run.id, None)

        if result.status_code != 0:
            await self._propose_crashed_state(
                flow_run,
                (
                    "Flow run process exited with non-zero status code"
                    f" {result.status_code}."
                ),
            )

        return result

    def get_status(self):
        """
        Retrieves basic info about this runner.
        """
        return {
            "name": self.name,
            "settings": {
                "prefetch_seconds": self._prefetch_seconds,
            },
        }

    async def _propose_pending_state(self, flow_run: "FlowRun") -> bool:
        run_logger = self.get_flow_run_logger(flow_run)
        state = flow_run.state
        try:
            state = await propose_state(
                self._client, Pending(), flow_run_id=flow_run.id
            )
        except Abort as exc:
            run_logger.info(
                (
                    f"Aborted submission of flow run '{flow_run.id}'. "
                    f"Server sent an abort signal: {exc}"
                ),
            )
            return False
        except Exception:
            run_logger.exception(
                f"Failed to update state of flow run '{flow_run.id}'",
            )
            return False

        if not state.is_pending():
            run_logger.info(
                (
                    f"Aborted submission of flow run '{flow_run.id}': "
                    f"Server returned a non-pending state {state.type.value!r}"
                ),
            )
            return False

        return True

    async def _propose_failed_state(self, flow_run: "FlowRun", exc: Exception) -> None:
        run_logger = self.get_flow_run_logger(flow_run)
        try:
            await propose_state(
                self._client,
                await exception_to_failed_state(message="Submission failed.", exc=exc),
                flow_run_id=flow_run.id,
            )
        except Abort:
            # We've already failed, no need to note the abort but we don't want it to
            # raise in the agent process
            pass
        except Exception:
            run_logger.error(
                f"Failed to update state of flow run '{flow_run.id}'",
                exc_info=True,
            )

    async def _propose_crashed_state(self, flow_run: "FlowRun", message: str) -> None:
        run_logger = self.get_flow_run_logger(flow_run)
        try:
            state = await propose_state(
                self._client,
                Crashed(message=message),
                flow_run_id=flow_run.id,
            )
        except Abort:
            # Flow run already marked as failed
            pass
        except Exception:
            run_logger.exception(f"Failed to update state of flow run '{flow_run.id}'")
        else:
            if state.is_crashed():
                run_logger.info(
                    f"Reported flow run '{flow_run.id}' as crashed: {message}"
                )

    async def _mark_flow_run_as_cancelled(
        self, flow_run: "FlowRun", state_updates: Optional[dict] = None
    ) -> None:
        state_updates = state_updates or {}
        state_updates.setdefault("name", "Cancelled")
        state_updates.setdefault("type", StateType.CANCELLED)
        state = flow_run.state.copy(update=state_updates)

        await self._client.set_flow_run_state(flow_run.id, state, force=True)

        # Do not remove the flow run from the cancelling set immediately because
        # the API caches responses for the `read_flow_runs` and we do not want to
        # duplicate cancellations.
        await self._schedule_task(
            60 * 10, self._cancelling_flow_run_ids.remove, flow_run.id
        )

    async def _schedule_task(self, __in_seconds: int, fn, *args, **kwargs):
        """
        Schedule a background task to start after some time.

        These tasks will be run immediately when the runner exits instead of waiting.

        The function may be async or sync. Async functions will be awaited.
        """

        async def wrapper(task_status):
            # If we are shutting down, do not sleep; otherwise sleep until the scheduled
            # time or shutdown
            if self.is_setup:
                with anyio.CancelScope() as scope:
                    self._scheduled_task_scopes.add(scope)
                    task_status.started()
                    await anyio.sleep(__in_seconds)

                self._scheduled_task_scopes.remove(scope)
            else:
                task_status.started()

            result = fn(*args, **kwargs)
            if inspect.iscoroutine(result):
                await result

        await self._runs_task_group.start(wrapper)

    async def __aenter__(self):
        self.logger.debug("Entering runner context...")
        await self.setup()
        return self

    async def __aexit__(self, *exc_info):
        self.logger.debug("Exiting runner context...")
        await self.teardown(*exc_info)

    def __repr__(self):
        return f"Runner(name={self.name!r})"

    async def create_deployment(self, flow, **kwargs):
        """
        Creates a deployment from the provided flow information and kwargs and stores the
        deployment ID to monitor for scheduled work.
        """
        # TODO: make a more ergonomic interface and dont use deployment class
        if "work_pool_name" in kwargs:
            raise ValueError(
                "Cannot specify a work pool name for a runner-managed deployment"
            )
        if "work_queue_name" in kwargs:
            raise ValueError(
                "Cannot specify a work queue name for a runner-managed deployment"
            )
        kwargs.setdefault("name", self.name)
        deployment = await Deployment.build_from_flow(
            flow,
            work_queue_name=None,
            apply=False,
            skip_upload=True,
            load_existing=False,
            **kwargs,
        )
        deployment.storage = None
        deployment_id = await deployment.apply(ignore_infra=True, upload=False)
        self.deployment_ids.append(deployment_id)

    async def load(self, flow, **kwargs):
        """
        Main method for creating deployments out of provided flow specification.

        TODO: expose a filesystem interface with hot reloading (which is why this method is
        distinct from `create_deployment`)
        """
        api = PREFECT_API_URL.value()
        if kwargs.get("schedule") and not api:
            self.logger.warning(
                "Cannot schedule flows on an ephemeral server; run `prefect server"
                " start` to start the scheduler."
            )
        await self.create_deployment(flow, **kwargs)

    async def start(self):
        """
        Main entrypoint for running a runner.
        """
        async with self as runner:
            async with anyio.create_task_group() as tg:
                await runner.sync_with_backend()
                tg.start_soon(
                    partial(
                        critical_service_loop,
                        workload=runner.get_and_submit_flow_runs,
                        interval=PREFECT_WORKER_QUERY_SECONDS.value(),
                        jitter_range=0.3,
                    )
                )
                # schedule the sync loop
                tg.start_soon(
                    partial(
                        critical_service_loop,
                        workload=runner.sync_with_backend,
                        interval=PREFECT_WORKER_HEARTBEAT_SECONDS.value(),
                        jitter_range=0.3,
                    )
                )
                tg.start_soon(
                    partial(
                        critical_service_loop,
                        workload=runner.check_for_cancelled_flow_runs,
                        interval=PREFECT_WORKER_QUERY_SECONDS.value() * 2,
                        jitter_range=0.3,
                    )
                )
