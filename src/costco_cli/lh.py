"""Optional LittleHorse orchestration for the 30-day price-adjustment window.

The CLI works without any of this. SQLite stays the record of what happened
(`claims`); LittleHorse only drives the waiting: it alerts you to a drop, holds
a User Task open until you claim it, nudges you twice, and gives up at the
deadline. `costco claim` completes the User Task; `costco claims` reads SQLite
and never needs the server.

Requires the `littlehorse` extra. The SDK caps at Python <3.14, so the worker
runs from its own interpreter; the rest of the CLI does not import this module.
"""
import asyncio
import hashlib
import inspect
import re
import time
from datetime import date, datetime

import grpc
import littlehorse
from littlehorse.config import LHConfig
from littlehorse.model import (Comparator, CompleteUserTaskRunRequest,
                               ListUserTaskRunRequest, PutUserTaskDefRequest, RunWfRequest,
                               SearchWfSpecRequest, TaskDefId, UserTaskField,
                               UserTaskRunId, UserTaskRunStatus,
                               VariableMutationType, VariableType, WfRunId,
                               WfRunVariableAccessLevel)
from littlehorse.worker import LHTaskWorker
from littlehorse.workflow import Workflow

from . import db, notify

WF_NAME = "costco-price-adjustment"
USER_TASK_DEF = "claim-price-adjustment"
TASK_NOTIFY = "costco-notify-drop"
TASK_RECORD = "costco-record-claim"

# Thrown by the User Task when cancel_user_task_run_after fires, i.e. the
# 30-day window closed with no answer from you.
EXC_WINDOW_CLOSED = "claim-window-closed"

# User Tasks need an assignee. There is no identity provider here, so this is
# just a label; it is who `costco claim` completes the task as.
CLAIM_USER = "me"

WHERE_WAREHOUSE = "Bring the receipt to the membership counter"
WHERE_ONLINE = "Use costco.com chat for online orders"

FIRST_NUDGE_DAYS_LEFT = 7
FINAL_NUDGE_DAYS_LEFT = 1

# Fallbacks for a run started by hand, i.e. as if bought today.
_DAY = 24 * 60 * 60
DEFAULT_EXPIRES_IN_SECONDS = 30 * _DAY
DEFAULT_FIRST_NUDGE_SECONDS = (30 - FIRST_NUDGE_DAYS_LEFT) * _DAY
DEFAULT_FINAL_NUDGE_SECONDS = (30 - FINAL_NUDGE_DAYS_LEFT) * _DAY


# ---------- run identity (decision 1A) ----------

def run_id_for(purchase_id, item_number):
    """A deterministic WfRun id for one purchase line.

    LittleHorse requires run ids to be valid hostnames, so the natural
    "purchase_id:item_number" key cannot be used directly -- a colon is
    rejected outright, and receipt barcodes and online order numbers may hold
    other characters a hostname disallows. The readable part is a best-effort
    slug; the trailing hash of the exact key means two different lines can
    never collide on one run, however the slug comes out.

    Deterministic, so starting the same line twice is a no-op on the server.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", f"{purchase_id}-{item_number}".lower()).strip("-")[:40]
    digest = hashlib.sha256(f"{purchase_id}:{item_number}".encode()).hexdigest()[:8]
    return f"claim-{slug}-{digest}" if slug else f"claim-{digest}"


def seconds_until(when, now=None):
    """Whole seconds from now until `when` (a date), never negative."""
    now = now or datetime.now()
    target = datetime.combine(when, datetime.min.time().replace(hour=9))
    return max(0, int((target - now).total_seconds()))


# ---------- task handlers ----------
#
# Handlers must be `async def` with named, annotated parameters: the worker
# rejects sync callables and *args outright. The work underneath is ordinary
# blocking code, so it goes to a thread -- an unreachable mail server would
# otherwise stall every other task for the SMTP timeout.

def _send_drop_alert(name, dollars_back, deadline, where):
    con = db.connect()
    try:
        return notify.send(con, "Costco price drop",
                           f"${dollars_back:.2f} back on {name}, claim by {deadline}. {where}.")
    finally:
        con.close()


def _write_claim(purchase_id, item_number, amount, outcome):
    con = db.connect()
    try:
        db.record_claim(con, purchase_id, item_number, amount, outcome)
        con.commit()
        return outcome
    finally:
        con.close()


async def notify_drop(name: str, dollars_back: float, deadline: str, where: str) -> str:
    """Alert you to a claimable drop. Raises on failure so LittleHorse retries."""
    return await asyncio.to_thread(_send_drop_alert, name, dollars_back, deadline, where)


def outcome_for(window_closed, claimed):
    """Turn the workflow's two booleans into a claims outcome.

    Kept out of the workflow on purpose: the DSL would need nested conditionals
    to build a string from two booleans, and a plain function is both clearer
    and directly unit-testable.
    """
    if window_closed:
        return "expired"
    return "claimed" if claimed else "skipped"


async def record_claim(purchase_id: str, item_number: str, amount: float,
                       window_closed: bool, claimed: bool) -> str:
    """Write the outcome to SQLite. The upsert in db.record_claim makes
    redelivery harmless."""
    return await asyncio.to_thread(_write_claim, purchase_id, item_number, amount,
                                   outcome_for(window_closed, claimed))


# ---------- the workflow ----------

def claim_workflow(thread):
    purchase_id = thread.add_variable("purchase_id", VariableType.STR)
    item_number = thread.add_variable("item_number", VariableType.STR)
    name = thread.add_variable("name", VariableType.STR)
    dollars_back = thread.add_variable("dollars_back", VariableType.DOUBLE)
    deadline = thread.add_variable("deadline", VariableType.STR)
    where = thread.add_variable("where", VariableType.STR)
    # Defaults matter: these feed schedule_reminder_task and
    # cancel_user_task_run_after, and an unset INT fails the node outright with
    # "Delay for User Task Action was not an INT, got a DEFINEDTYPE_NOT_SET"
    # before the User Task is even created. ensure_run() always supplies real
    # values; these keep a run started by hand (the dashboard's Run button)
    # working, using the timings of a purchase made today.
    first_nudge = thread.add_variable("first_nudge_seconds", VariableType.INT,
                                      default_value=DEFAULT_FIRST_NUDGE_SECONDS)
    final_nudge = thread.add_variable("final_nudge_seconds", VariableType.INT,
                                      default_value=DEFAULT_FINAL_NUDGE_SECONDS)
    expires_in = thread.add_variable("expires_in_seconds", VariableType.INT,
                                     default_value=DEFAULT_EXPIRES_IN_SECONDS)

    # PUBLIC_VAR so the failure-handler thread can set it on the parent.
    # INHERITED_VAR is rejected here -- that access level is only for child
    # workflows, and a failure handler is a child *thread* of this one.
    window_closed = thread.add_variable(
        "window_closed", VariableType.BOOL, default_value=False,
        access_level=WfRunVariableAccessLevel.PUBLIC_VAR)
    claimed = thread.add_variable("claimed", VariableType.BOOL, default_value=False)

    # No alert on discovery: cmd_daily already does that, and it works with the
    # server down. This workflow owns the *nudges* -- the thing a cron job
    # cannot do -- so alerting here too would just double every message.
    user_task = thread.assign_user_task(USER_TASK_DEF, user_id=CLAIM_USER)
    # Both builders rebuild the *last* node, so they have to come straight after
    # assign_user_task. with_notes must come first: it rebuilds the node without
    # carrying over the cancellation exception name, so the reverse order
    # silently loses the deadline branch.
    user_task.with_notes(where)
    user_task.with_on_cancellation_exception(EXC_WINDOW_CLOSED)

    thread.schedule_reminder_task(user_task, first_nudge, TASK_NOTIFY,
                                  name, dollars_back, deadline, where)
    thread.schedule_reminder_task(user_task, final_nudge, TASK_NOTIFY,
                                  name, dollars_back, deadline, where)
    thread.cancel_user_task_run_after(user_task, expires_in)

    # The deadline passed with no answer. A handler that finishes normally lets
    # the parent thread RESUME after the failed node, so this only records the
    # fact in a variable -- it cannot stop what follows.
    thread.handle_exception(
        user_task,
        lambda h: h.mutate(window_closed, VariableMutationType.ASSIGN, True),
        EXC_WINDOW_CLOSED)

    # Guarded by the variable, never by the node's output: reading
    # user_task.with_json_path on the cancelled path throws
    # "Cannot get output from a non-completed NodeRun" server-side, because a
    # cancelled User Task never completes.
    thread.do_if(
        thread.condition(window_closed, Comparator.EQUALS, False),
        lambda h: h.mutate(claimed, VariableMutationType.ASSIGN,
                           user_task.with_json_path("$.claimed")))

    # One record node for all three outcomes.
    thread.execute(TASK_RECORD, purchase_id, item_number, dollars_back,
                   window_closed, claimed, retries=2)


READY_POLL_SECONDS = 2
READY_TIMEOUT_SECONDS = 180


def wait_until_ready(config=None, timeout=READY_TIMEOUT_SECONDS, poll=READY_POLL_SECONDS):
    """Block until the server can actually serve requests.

    `lhctl version` answers within a few seconds of boot, but the Kafka Streams
    application underneath needs roughly half a minute more before it accepts
    anything else -- until then every call fails with UNAVAILABLE and a detail
    of "Kafka Streams not ready yet" or "Streams application is in REBALANCING
    state". A worker that registers inside that window just spins in its retry
    loop printing errors, so wait for a real query to succeed first.

    Raises the underlying RpcError if the server is unreachable for some other
    reason, or if it is still not ready after `timeout` seconds.
    """
    config = config or LHConfig()
    deadline = time.monotonic() + timeout
    while True:
        try:
            config.stub().SearchWfSpec(SearchWfSpecRequest())
            return True
        except grpc.RpcError as e:
            if e.code() is not grpc.StatusCode.UNAVAILABLE or time.monotonic() >= deadline:
                raise
            time.sleep(poll)


def _put_task_def(handler, name, config):
    """Register one TaskDef, tolerating one that is already there.

    TaskDefs are immutable server-side, so re-registering an unchanged handler
    is a harmless ALREADY_EXISTS. But if the handler's parameters have changed,
    the server keeps the old schema and the mismatch surfaces later as puzzling
    task failures -- so say so plainly instead.
    """
    try:
        littlehorse.create_task_def(handler, name, config)
    except grpc.RpcError as e:
        if e.code() is not grpc.StatusCode.ALREADY_EXISTS:
            raise
        existing = [v.name for v in config.stub().GetTaskDef(TaskDefId(name=name)).input_vars]
        wanted = list(inspect.signature(handler).parameters)
        if existing != wanted:
            raise RuntimeError(
                f"TaskDef {name!r} is registered with parameters {existing}, but the handler "
                f"now takes {wanted}. TaskDefs are immutable: remove it with "
                f"`lhctl delete taskDef {name}` or register under a new name.") from e


def register(config=None):
    """Register the task defs, the user task def and the wf spec. Idempotent."""
    config = config or LHConfig()
    _put_task_def(notify_drop, TASK_NOTIFY, config)
    _put_task_def(record_claim, TASK_RECORD, config)
    config.stub().PutUserTaskDef(PutUserTaskDefRequest(
        name=USER_TASK_DEF,
        description="Claim a Costco price adjustment before the window closes",
        fields=[
            UserTaskField(name="claimed", type=VariableType.BOOL, display_name="Claimed?",
                          description="Yes if you got the money back; No to pass on it",
                          required=True),
            UserTaskField(name="note", type=VariableType.STR, display_name="Note",
                          description="Anything worth remembering", required=False),
        ],
    ))
    littlehorse.create_workflow_spec(Workflow(WF_NAME, claim_workflow), config)


def variables_for(drop, today=None):
    """The WfRun variables for one Drop from prices.find_drops()."""
    today = today or date.today()
    where = WHERE_ONLINE if drop.source == "online" else WHERE_WAREHOUSE
    return {
        "purchase_id": drop.purchase_id,
        "item_number": drop.item_number,
        "name": drop.name or drop.item_number,
        "dollars_back": float(drop.total),
        "deadline": drop.deadline.isoformat(),
        "where": where,
        "first_nudge_seconds": seconds_until(drop.deadline - _days(FIRST_NUDGE_DAYS_LEFT)),
        "final_nudge_seconds": seconds_until(drop.deadline - _days(FINAL_NUDGE_DAYS_LEFT)),
        "expires_in_seconds": seconds_until(drop.deadline),
    }


def _days(n):
    from datetime import timedelta
    return timedelta(days=n)


def worker_main():
    """Entry point for the task worker process. Runs until killed.

    Two traps worth knowing if you ever embed a worker somewhere else:

    * LHConfig caches its gRPC channel, and an async channel belongs to the
      event loop that created it. Sharing one config across threads with
      separate loops fails with "got Future attached to a different loop" --
      intermittently, which is worse than always. Build the config inside the
      loop that will use it, as below, or one per thread.
    * littlehorse.start() installs signal handlers, so it only works on the
      main thread. That is exactly where it belongs for a long-running process
      like this one, and it gives clean shutdown under launchd or Docker. Off
      the main thread it fails *silently*: use asyncio.run(worker.start()).
    """
    print("waiting for the LittleHorse server...", flush=True)
    wait_until_ready()
    # Registering here is what makes the worker self-sufficient: start the
    # server, start this, and the workflow exists. register() is idempotent.
    register()
    print(f"registered {WF_NAME}; serving {TASK_NOTIFY} and {TASK_RECORD}", flush=True)

    async def main():
        config = LHConfig()
        workers = [LHTaskWorker(notify_drop, TASK_NOTIFY, config),
                   LHTaskWorker(record_claim, TASK_RECORD, config)]
        await littlehorse.start(*workers)

    asyncio.run(main())


# ---------- starting and completing runs ----------

def ensure_run(drop, config=None, today=None):
    """Start the claim workflow for one drop, unless it is already running.

    The run id is derived from the purchase line, so calling this every morning
    for the same drop is a no-op on the server: a duplicate id comes back as
    ALREADY_EXISTS, which is the outcome we want, not an error.

    Returns the run id if a run was started, None if one already existed.
    """
    config = config or LHConfig()
    run_id = run_id_for(drop.purchase_id, drop.item_number)
    variables = variables_for(drop, today=today)
    try:
        config.stub().RunWf(RunWfRequest(
            wf_spec_name=WF_NAME, id=run_id,
            variables={k: littlehorse.to_variable_value(v) for k, v in variables.items()}))
        return run_id
    except grpc.RpcError as e:
        if e.code() is grpc.StatusCode.ALREADY_EXISTS:
            return None
        raise


def pending_user_task(run_id, config=None):
    """The open UserTaskRun for a run, or None if there is nothing to answer."""
    config = config or LHConfig()
    listed = config.stub().ListUserTaskRuns(
        ListUserTaskRunRequest(wf_run_id=WfRunId(id=run_id)))
    for utr in listed.results:
        if UserTaskRunStatus.Name(utr.status) in ("ASSIGNED", "UNASSIGNED"):
            return utr
    return None


def complete_claim(purchase_id, item_number, claimed=True, note=None, config=None):
    """Answer the User Task for one purchase line.

    Returns True if a task was completed, False if the server had nothing open
    for it -- which is normal when the window already closed, or when the run
    was never started because the server was down that morning.
    """
    config = config or LHConfig()
    run_id = run_id_for(purchase_id, item_number)
    utr = pending_user_task(run_id, config=config)
    if utr is None:
        return False
    results = {"claimed": littlehorse.to_variable_value(claimed)}
    if note:
        results["note"] = littlehorse.to_variable_value(note)
    config.stub().CompleteUserTaskRun(CompleteUserTaskRunRequest(
        user_task_run_id=UserTaskRunId(wf_run_id=WfRunId(id=run_id),
                                       user_task_guid=utr.id.user_task_guid),
        user_id=CLAIM_USER,
        results=results))
    return True
