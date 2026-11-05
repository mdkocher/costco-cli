"""Tests for the optional LittleHorse layer.

Skipped wholesale when the `littlehorse` extra is absent, which is the normal
state on an interpreter the SDK does not support (it caps at Python <3.14).
"""
import re

import pytest

pytest.importorskip("littlehorse", reason="needs the littlehorse extra")

from costco_cli import lh  # noqa: E402

HOSTNAME_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


# ---------- run ids (decision 1A) ----------

def test_run_id_is_a_valid_hostname_label():
    """The server rejects anything else: "Optional argument 'id' must be a
    valid hostname". A colon separator fails outright."""
    assert HOSTNAME_LABEL.match(lh.run_id_for("R1", "1707952"))


def test_run_id_is_deterministic():
    """Idempotency rests on this: the same purchase line must always map to the
    same run, so starting it every morning is a no-op server-side."""
    assert lh.run_id_for("R1", "1707952") == lh.run_id_for("R1", "1707952")


def test_run_ids_differ_per_purchase_line():
    assert lh.run_id_for("R1", "1707952") != lh.run_id_for("R2", "1707952")
    assert lh.run_id_for("R1", "1707952") != lh.run_id_for("R1", "1819379")


@pytest.mark.parametrize("purchase_id", [
    "R1", "0070120281234", "ORDER/123", "a b c", "WC_99.8", "ÜMLAUT", "!!!", "",
])
def test_awkward_purchase_ids_still_make_a_valid_hostname(purchase_id):
    """Receipt barcodes and online order numbers are not our format to choose."""
    run_id = lh.run_id_for(purchase_id, "1707952")
    assert HOSTNAME_LABEL.match(run_id), run_id


def test_ids_that_slugify_identically_do_not_collide():
    """"ORDER/1" and "ORDER-1" slug the same way; the hash keeps them apart."""
    assert lh.run_id_for("ORDER/1", "1") != lh.run_id_for("ORDER-1", "1")


# ---------- outcome decision ----------

@pytest.mark.parametrize("window_closed,claimed,expected", [
    (True, False, "expired"),
    (True, True, "expired"),      # deadline wins: the task was never answered
    (False, True, "claimed"),
    (False, False, "skipped"),
])
def test_outcome_for(window_closed, claimed, expected):
    assert lh.outcome_for(window_closed, claimed) == expected


def test_every_outcome_is_one_the_schema_allows():
    from costco_cli import db
    for wc in (True, False):
        for c in (True, False):
            assert lh.outcome_for(wc, c) in db.CLAIM_OUTCOMES


# ---------- nudge timing ----------

def test_seconds_until_is_never_negative():
    """A drop discovered after its deadline must expire at once, not schedule a
    timer in the past."""
    from datetime import date, timedelta
    assert lh.seconds_until(date.today() - timedelta(days=5)) == 0


def test_seconds_until_grows_with_distance():
    from datetime import date, timedelta
    today = date.today()
    assert lh.seconds_until(today + timedelta(days=7)) > lh.seconds_until(today + timedelta(days=1))


# ---------- workflow variables ----------

class _Drop:
    purchase_id = "R1"
    item_number = "1707952"
    name = "Kirkland Oxi Powder"
    quantity = 4
    total = 12.0
    source = "warehouse"
    from datetime import date as _d, timedelta as _td
    deadline = _d.today() + _td(days=20)


def test_variables_for_covers_every_workflow_input():
    v = lh.variables_for(_Drop())
    assert set(v) == {"purchase_id", "item_number", "name", "dollars_back", "deadline",
                      "where", "first_nudge_seconds", "final_nudge_seconds",
                      "expires_in_seconds"}


def test_nudges_land_before_the_deadline_in_the_right_order():
    v = lh.variables_for(_Drop())
    assert v["first_nudge_seconds"] < v["final_nudge_seconds"] < v["expires_in_seconds"]


def test_where_depends_on_where_you_bought_it():
    warehouse = lh.variables_for(_Drop())
    online = _Drop()
    online.source = "online"
    assert warehouse["where"] == lh.WHERE_WAREHOUSE
    assert lh.variables_for(online)["where"] == lh.WHERE_ONLINE


def test_an_unnamed_item_falls_back_to_its_number():
    d = _Drop()
    d.name = None
    assert lh.variables_for(d)["name"] == "1707952"
