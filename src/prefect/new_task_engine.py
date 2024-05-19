import inspect
import logging
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    Generator,
    Generic,
    Iterable,
    Literal,
    Optional,
    Set,
    TypeVar,
    Union,
    cast,
)
from uuid import UUID

import pendulum
from typing_extensions import ParamSpec

from prefect import Task, get_client
from prefect.client.orchestration import SyncPrefectClient
from prefect.client.schemas import TaskRun
from prefect.client.schemas.objects import State, TaskRunInput
from prefect.context import FlowRunContext, TaskRunContext
from prefect.exceptions import Abort, Pause, PrefectException, UpstreamTaskError
from prefect.logging.handlers import APILogHandler
from prefect.logging.loggers import get_logger, patch_print, task_run_logger
from prefect.new_futures import PrefectFuture
from prefect.results import ResultFactory
from prefect.settings import PREFECT_DEBUG_MODE, PREFECT_TASKS_REFRESH_CACHE
from prefect.states import (
    Failed,
    Paused,
    Pending,
    Retrying,
    Running,
    StateDetails,
    exception_to_crashed_state,
    exception_to_failed_state,
    return_value_to_state,
)
from prefect.utilities.asyncutils import run_sync
from prefect.utilities.callables import parameters_to_args_kwargs
from prefect.utilities.collections import visit_collection
from prefect.utilities.engine import (
    _get_hook_name,
    propose_state_sync,
    resolve_to_final_result,
)
from prefect.utilities.math import clamped_poisson_interval
from prefect.utilities.timeout import timeout, timeout_async

P = ParamSpec("P")
R = TypeVar("R")


@dataclass
class TaskRunEngine(Generic[P, R]):
    task: Union[Task[P, R], Task[P, Coroutine[Any, Any, R]]]
    logger: logging.Logger = field(default_factory=lambda: get_logger("engine"))
    parameters: Optional[Dict[str, Any]] = None
    task_run: Optional[TaskRun] = None
    retries: int = 0
    wait_for: Optional[Iterable[PrefectFuture]] = None
    _initial_run_context: Optional[TaskRunContext] = None
    _is_started: bool = False
    _client: Optional[SyncPrefectClient] = None
    _task_name_set: bool = False

    def __post_init__(self):
        if self.parameters is None:
            self.parameters = {}

    @property
    def client(self) -> SyncPrefectClient:
        if not self._is_started or self._client is None:
            raise RuntimeError("Engine has not started.")
        return self._client

    @property
    def state(self) -> State:
        if not self.task_run:
            raise ValueError("Task run is not set")
        return self.task_run.state

    @property
    def can_retry(self) -> bool:
        retry_condition: Optional[
            Callable[[Task[P, Coroutine[Any, Any, R]], TaskRun, State], bool]
        ] = self.task.retry_condition_fn
        if not self.task_run:
            raise ValueError("Task run is not set")
        try:
            self.logger.debug(
                f"Running `retry_condition_fn` check {retry_condition!r} for task"
                f" {self.task.name!r}"
            )
            return not retry_condition or retry_condition(
                self.task, self.task_run, self.state
            )
        except Exception:
            self.logger.error(
                (
                    "An error was encountered while running `retry_condition_fn` check"
                    f" '{retry_condition!r}' for task {self.task.name!r}"
                ),
                exc_info=True,
            )
            return False

    def get_hooks(self, state: State, as_async: bool = False) -> Iterable[Callable]:
        task = self.task
        task_run = self.task_run

        if not task_run:
            raise ValueError("Task run is not set")

        hooks = None
        if state.is_failed() and task.on_failure:
            hooks = task.on_failure
        elif state.is_completed() and task.on_completion:
            hooks = task.on_completion

        for hook in hooks or []:
            hook_name = _get_hook_name(hook)

            @contextmanager
            def hook_context():
                try:
                    self.logger.info(
                        f"Running hook {hook_name!r} in response to entering state"
                        f" {state.name!r}"
                    )
                    yield
                except Exception:
                    self.logger.error(
                        f"An error was encountered while running hook {hook_name!r}",
                        exc_info=True,
                    )
                else:
                    self.logger.info(
                        f"Hook {hook_name!r} finished running successfully"
                    )

            if as_async:

                async def _hook_fn():
                    with hook_context():
                        result = hook(task, task_run, state)
                        if inspect.isawaitable(result):
                            await result
            else:

                def _hook_fn():
                    with hook_context():
                        result = hook(task, task_run, state)
                        if inspect.isawaitable(result):
                            run_sync(result)

            yield _hook_fn

    def _compute_state_details(
        self, include_cache_expiration: bool = False
    ) -> StateDetails:
        from prefect.utilities.engine import should_log_prints

        # We sometimes call this prior to setting up the run context, so we create a temporary
        # context here
        # TODO: Should we set up the run context earlier in Engine.start?
        log_prints = should_log_prints(self.task)
        task_run_context = TaskRunContext(
            task=self.task,
            log_prints=log_prints,
            task_run=self.task_run,
            parameters=self.parameters,
            result_factory=run_sync(ResultFactory.from_autonomous_task(self.task)),  # type: ignore
            client=self.client,
        )
        ## setup cache metadata
        cache_key = (
            self.task.cache_key_fn(
                task_run_context,
                self.parameters or {},
            )
            if self.task.cache_key_fn
            else None
        )
        # Ignore the cached results for a cache key, default = false
        # Setting on task level overrules the Prefect setting (env var)
        refresh_cache = (
            self.task.refresh_cache
            if self.task.refresh_cache is not None
            else PREFECT_TASKS_REFRESH_CACHE.value()
        )

        if include_cache_expiration:
            cache_expiration = (
                (pendulum.now("utc") + self.task.cache_expiration)
                if self.task.cache_expiration
                else None
            )
        else:
            cache_expiration = None

        return StateDetails(
            cache_key=cache_key,
            refresh_cache=refresh_cache,
            cache_expiration=cache_expiration,
        )

    def _resolve_parameters(self):
        if not self.parameters:
            return {}

        resolved_parameters = {}
        for parameter, value in self.parameters.items():
            try:
                resolved_parameters[parameter] = visit_collection(
                    value,
                    visit_fn=resolve_to_final_result,
                    return_data=True,
                    max_depth=-1,
                    remove_annotations=True,
                    context={},
                )
            except UpstreamTaskError:
                raise
            except Exception as exc:
                raise PrefectException(
                    f"Failed to resolve inputs in parameter {parameter!r}. If your"
                    " parameter type is not supported, consider using the `quote`"
                    " annotation to skip resolution of inputs."
                ) from exc

        self.parameters = resolved_parameters

    def _wait_for_dependencies(self):
        if not self.wait_for:
            return

        visit_collection(
            self.wait_for,
            visit_fn=resolve_to_final_result,
            return_data=False,
            max_depth=-1,
            remove_annotations=True,
            context={},
        )

    def begin_run(self):
        try:
            self._resolve_parameters()
            self._wait_for_dependencies()
        except UpstreamTaskError as upstream_exc:
            state = self.set_state(
                Pending(
                    name="NotReady",
                    message=str(upstream_exc),
                ),
                # if orchestrating a run already in a pending state, force orchestration to
                # update the state name
                force=self.state.is_pending(),
            )
            return

        state_details = self._compute_state_details()
        new_state = Running(state_details=state_details)
        state = self.set_state(new_state)

        BACKOFF_MAX = 10
        backoff_count = 0

        # TODO: Could this listen for state change events instead of polling?
        while state.is_pending() or state.is_paused():
            if backoff_count < BACKOFF_MAX:
                backoff_count += 1
            interval = clamped_poisson_interval(
                average_interval=backoff_count, clamping_factor=0.3
            )
            time.sleep(interval)
            state = self.set_state(new_state)

    def set_state(self, state: State, force: bool = False) -> State:
        if not self.task_run:
            raise ValueError("Task run is not set")
        try:
            new_state = propose_state_sync(
                self.client, state, task_run_id=self.task_run.id, force=force
            )
        except Pause as exc:
            # We shouldn't get a pause signal without a state, but if this happens,
            # just use a Paused state to assume an in-process pause.
            new_state = exc.state if exc.state else Paused()
            if new_state.state_details.pause_reschedule:
                # If we're being asked to pause and reschedule, we should exit the
                # task and expect to be resumed later.
                raise

        # currently this is a hack to keep a reference to the state object
        # that has an in-memory result attached to it; using the API state
        # could result in losing that reference
        self.task_run.state = new_state
        return new_state

    def result(self, raise_on_failure: bool = True) -> "Union[R, State, None]":
        _result = self.state.result(raise_on_failure=raise_on_failure, fetch=True)
        # state.result is a `sync_compatible` function that may or may not return an awaitable
        # depending on whether the parent frame is sync or not
        if inspect.isawaitable(_result):
            _result = run_sync(_result)
        return _result

    def handle_success(self, result: R) -> R:
        result_factory = getattr(TaskRunContext.get(), "result_factory", None)
        if result_factory is None:
            raise ValueError("Result factory is not set")
        terminal_state = run_sync(
            return_value_to_state(
                result,
                result_factory=result_factory,
            )
        )
        terminal_state.state_details = self._compute_state_details(
            include_cache_expiration=True
        )
        self.set_state(terminal_state)
        return result

    def handle_retry(self, exc: Exception) -> bool:
        """
        If the task has retries left, and the retry condition is met, set the task to retrying.
        - If the task has no retries left, or the retry condition is not met, return False.
        - If the task has retries left, and the retry condition is met, return True.
        """
        if self.retries < self.task.retries and self.can_retry:
            self.set_state(Retrying(), force=True)
            self.retries = self.retries + 1
            return True
        return False

    def handle_exception(self, exc: Exception) -> None:
        # If the task fails, and we have retries left, set the task to retrying.
        if not self.handle_retry(exc):
            # If the task has no retries left, or the retry condition is not met, set the task to failed.
            context = TaskRunContext.get()
            state = run_sync(
                exception_to_failed_state(
                    exc,
                    message="Task run encountered an exception",
                    result_factory=getattr(context, "result_factory", None),
                )
            )
            self.set_state(state)

    def handle_timeout(self, exc: TimeoutError) -> None:
        message = f"Task run exceeded timeout of {self.task.timeout_seconds} seconds"
        self.logger.error(message)
        state = Failed(
            data=exc,
            message=message,
            name="TimedOut",
        )
        self.set_state(state)

    def handle_crash(self, exc: BaseException) -> None:
        state = run_sync(exception_to_crashed_state(exc))
        self.logger.error(f"Crash detected! {state.message}")
        self.logger.debug("Crash details:", exc_info=exc)
        self.set_state(state, force=True)

    @contextmanager
    def enter_run_context(self, client: Optional[SyncPrefectClient] = None):
        from prefect.utilities.engine import (
            _resolve_custom_task_run_name,
            should_log_prints,
        )

        if client is None:
            client = self.client
        if not self.task_run:
            raise ValueError("Task run is not set")

        self.task_run = client.read_task_run(self.task_run.id)
        log_prints = should_log_prints(self.task)

        with ExitStack() as stack:
            if log_prints:
                stack.enter_context(patch_print())
            stack.enter_context(
                TaskRunContext(
                    task=self.task,
                    log_prints=log_prints,
                    task_run=self.task_run,
                    parameters=self.parameters,
                    result_factory=run_sync(
                        ResultFactory.from_autonomous_task(self.task)
                    ),  # type: ignore
                    client=client,
                )
            )
            # set the logger to the task run logger
            self.logger = task_run_logger(task_run=self.task_run, task=self.task)  # type: ignore

            # update the task run name if necessary
            if not self._task_name_set and self.task.task_run_name:
                task_run_name = _resolve_custom_task_run_name(
                    task=self.task, parameters=self.parameters
                )
                self.client.set_task_run_name(
                    task_run_id=self.task_run.id, name=task_run_name
                )
                self.logger.extra["task_run_name"] = task_run_name
                self.logger.debug(
                    f"Renamed task run {self.task_run.name!r} to {task_run_name!r}"
                )
                self.task_run.name = task_run_name
                self._task_name_set = True
            yield

    @contextmanager
    def start(
        self,
        task_run_id: Optional[UUID] = None,
        dependencies: Optional[Dict[str, Set[TaskRunInput]]] = None,
    ) -> Generator["TaskRunEngine", Any, Any]:
        """
        Enters a client context and creates a task run if needed.
        """

        with get_client(sync_client=True) as client:
            self._client = client
            self._is_started = True
            try:
                if not self.task_run:
                    self.task_run = run_sync(
                        self.task.create_run(
                            id=task_run_id,
                            client=client,
                            parameters=self.parameters,
                            flow_run_context=FlowRunContext.get(),
                            parent_task_run_context=TaskRunContext.get(),
                            wait_for=self.wait_for,
                            extra_task_inputs=dependencies,
                        )
                    )
                self.logger.info(
                    f"Created task run {self.task_run.name!r} for task {self.task.name!r}"
                )

                yield self
            except Exception:
                # regular exceptions are caught and re-raised to the user
                raise
            except (Pause, Abort):
                # Do not capture internal signals as crashes
                raise
            except BaseException as exc:
                # BaseExceptions are caught and handled as crashes
                self.handle_crash(exc)
                raise
            finally:
                # If debugging, use the more complete `repr` than the usual `str` description
                display_state = (
                    repr(self.state) if PREFECT_DEBUG_MODE else str(self.state)
                )
                self.logger.log(
                    level=logging.INFO if self.state.is_completed() else logging.ERROR,
                    msg=f"Finished in state {display_state}",
                )

                maybe_awaitable = APILogHandler.flush()
                if inspect.isawaitable(maybe_awaitable):
                    run_sync(maybe_awaitable)

                self._is_started = False
                self._client = None

    def is_running(self) -> bool:
        if getattr(self, "task_run", None) is None:
            return False
        return getattr(self, "task_run").state.is_running()


def run_task_sync(
    task: Task[P, R],
    task_run_id: Optional[UUID] = None,
    task_run: Optional[TaskRun] = None,
    parameters: Optional[Dict[str, Any]] = None,
    wait_for: Optional[Iterable[PrefectFuture]] = None,
    return_type: Literal["state", "result"] = "result",
    dependencies: Optional[Dict[str, Set[TaskRunInput]]] = None,
) -> Union[R, State, None]:
    engine = TaskRunEngine[P, R](
        task=task, parameters=parameters, task_run=task_run, wait_for=wait_for
    )

    # This is a context manager that keeps track of the run of the task run.
    with engine.start(task_run_id=task_run_id, dependencies=dependencies) as run:
        run.begin_run()

        while run.is_running():
            with run.enter_run_context():
                try:
                    # This is where the task is actually run.
                    with timeout(seconds=run.task.timeout_seconds):
                        call_args, call_kwargs = parameters_to_args_kwargs(
                            task.fn, run.parameters or {}
                        )
                        run.logger.debug(
                            f"Executing flow {task.name!r} for flow run {run.task_run.name!r}..."
                        )
                        result = cast(R, task.fn(*call_args, **call_kwargs))  # type: ignore

                    # If the task run is successful, finalize it.
                    run.handle_success(result)
                except TimeoutError as exc:
                    run.handle_timeout(exc)
                except Exception as exc:
                    run.handle_exception(exc)

        if run.state.is_final():
            for hook in run.get_hooks(run.state):
                hook()

        if return_type == "state":
            return run.state
        return run.result()


async def run_task_async(
    task: Task[P, Coroutine[Any, Any, R]],
    task_run_id: Optional[UUID] = None,
    task_run: Optional[TaskRun] = None,
    parameters: Optional[Dict[str, Any]] = None,
    wait_for: Optional[Iterable[PrefectFuture]] = None,
    return_type: Literal["state", "result"] = "result",
    dependencies: Optional[Dict[str, Set[TaskRunInput]]] = None,
) -> Union[R, State, None]:
    """
    Runs a task against the API.

    We will most likely want to use this logic as a wrapper and return a coroutine for type inference.
    """
    engine = TaskRunEngine[P, R](
        task=task, parameters=parameters, task_run=task_run, wait_for=wait_for
    )

    # This is a context manager that keeps track of the run of the task run.
    with engine.start(task_run_id=task_run_id, dependencies=dependencies) as run:
        run.begin_run()

        while run.is_running():
            with run.enter_run_context():
                try:
                    # This is where the task is actually run.
                    with timeout_async(seconds=run.task.timeout_seconds):
                        call_args, call_kwargs = parameters_to_args_kwargs(
                            task.fn, run.parameters or {}
                        )
                        run.logger.debug(
                            f"Executing flow {task.name!r} for flow run {run.task_run.name!r}..."
                        )
                        result = cast(R, await task.fn(*call_args, **call_kwargs))  # type: ignore

                    # If the task run is successful, finalize it.
                    run.handle_success(result)
                except TimeoutError as exc:
                    run.handle_timeout(exc)
                except Exception as exc:
                    run.handle_exception(exc)

        if run.state.is_final():
            for hook in run.get_hooks(run.state, as_async=True):
                await hook()

        if return_type == "state":
            return run.state
        return run.result()


def run_task(
    task: Task[P, R],
    task_run_id: Optional[UUID] = None,
    task_run: Optional[TaskRun] = None,
    parameters: Optional[Dict[str, Any]] = None,
    wait_for: Optional[Iterable[PrefectFuture]] = None,
    return_type: Literal["state", "result"] = "result",
    dependencies: Optional[Dict[str, Set[TaskRunInput]]] = None,
) -> Union[R, State, None]:
    kwargs = dict(
        task=task,
        task_run_id=task_run_id,
        task_run=task_run,
        parameters=parameters,
        wait_for=wait_for,
        return_type=return_type,
        dependencies=dependencies,
    )
    if task.isasync:
        return run_task_async(**kwargs)
    else:
        return run_task_sync(**kwargs)
