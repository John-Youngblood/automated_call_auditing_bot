"""Saved calls and contacts: the Store, below the registry and the routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.schemas.calls import Call, Caller, CallStatus, Contact, utc_now
from app.services.broadcaster import Broadcaster
from app.services.call_registry import CallRegistry
from app.services.store import DEGRADED_WARNING, Store, _from_ms, _to_ms

NUMBER = "+15035551234"
T0 = datetime(2026, 10, 22, 19, 30, 5, 123000, tzinfo=UTC)


def call(call_id: str, **fields: object) -> Call:
    """A finished call from NUMBER, unless told otherwise."""
    fields = {"status": CallStatus.ENDED, "started_at": T0, **fields}
    number = fields.pop("number", NUMBER)
    return Call(call_id=call_id, caller=Caller(number=number), **fields)


class TestTimestamps:
    def test_round_trip_as_aware_utc(self, store: Store) -> None:
        pacific = T0.astimezone(timezone(timedelta(hours=-7)))

        assert _from_ms(_to_ms(pacific)) == T0
        assert _from_ms(_to_ms(T0)).tzinfo is UTC

        store.save_call(call("CA1", started_at=pacific, ended_at=T0 + timedelta(minutes=3)))
        saved = store.get_call("CA1")
        assert saved.started_at == T0
        assert saved.ended_at == T0 + timedelta(minutes=3)

    def test_a_naive_datetime_is_refused(self) -> None:
        """Guessing its zone is how times once shifted by the UTC offset."""
        with pytest.raises(ValueError, match="naive"):
            _to_ms(datetime(2026, 10, 22, 19, 30))


@pytest.mark.parametrize("number", ["anonymous", "266696687", "+0123456789", "+1503555012x"])
def test_the_schema_refuses_a_number_that_is_not_one(store: Store, number: str) -> None:
    """A tripwire under phone.caller_number: if it ever lets "anonymous"
    through, every withheld caller would share one contact and one history.
    (A well-formed placeholder like +266696687 needs that gate; it is E.164.)"""
    assert store.save_call(call("CA1", number=number)) is False
    assert store.put_contact(Contact(number=number, name="Anon", updated_at=T0)) is False
    assert store.get_call("CA1") is None


def test_the_caller_profile_leaves_out_the_call_asking(store: Store) -> None:
    """A retried webhook for this ring must not count itself as a previous call."""
    store.save_call(call("CA-rejected", status=CallStatus.REJECTED))
    store.save_call(
        call(
            "CA-on-air",
            started_at=T0 + timedelta(days=1),
            was_accepted=True,
            on_air_seconds=240,
        )
    )
    # Accepted, but the host never picked up: not on air.
    store.save_call(call("CA-missed", started_at=T0 + timedelta(days=2), was_accepted=True))
    store.save_call(call("CA-now", status=CallStatus.SCREENING, started_at=T0 + timedelta(days=3)))
    store.save_call(call("CA-stranger", number="+15035559999"))
    store.put_contact(Contact(number=NUMBER, name="Pat", updated_at=T0))

    contact, history = store.caller_profile(NUMBER, exclude_sid="CA-now")

    assert contact.name == "Pat"
    assert history.previous_calls == 3
    assert history.times_on_air == 1
    assert history.last_on_air_at == T0 + timedelta(days=1)
    assert history.times_rejected == 1
    assert history.last_called_at == T0 + timedelta(days=2)


def test_a_first_time_caller_has_an_empty_profile(store: Store) -> None:
    contact, history = store.caller_profile(NUMBER, exclude_sid="CA-now")

    assert contact is None
    assert history.previous_calls == 0
    assert history.last_called_at is None


class TestHistory:
    def test_pages_are_stable_when_calls_share_a_start_time(self, store: Store) -> None:
        """Keyset on (started_at, call_sid): a tie on the first is broken by
        the second, so no call repeats or goes missing across a page break."""
        for n in range(5):
            store.save_call(call(f"CA{n}"))

        seen: list[str] = []
        cursor = None
        for _ in range(3):
            page, cursor = store.history(limit=2, before=cursor)
            seen += [c.call_id for c in page]
        assert seen == ["CA4", "CA3", "CA2", "CA1", "CA0"]
        assert cursor is None

    def test_only_finished_calls_are_listed(self, store: Store) -> None:
        store.save_call(call("CA-holding", status=CallStatus.ON_HOLD))
        store.save_call(call("CA-on-air", status=CallStatus.ACCEPTED))

        page, _ = store.history(limit=10)

        assert [c.call_id for c in page] == ["CA-on-air"]

    def test_search_takes_percent_and_underscore_literally(self, store: Store) -> None:
        store.save_call(call("CA-percent", transcript="I am 100% SURE about this"))
        store.save_call(call("CA-digits", transcript="I said it 1000 times"))
        store.save_call(call("CA-underscore", transcript="my handle is a_b"))
        store.save_call(call("CA-letter", transcript="my handle is axb"))

        def found(q: str) -> list[str]:
            return [c.call_id for c in store.history(limit=10, q=q)[0]]

        assert found("100%") == ["CA-percent"]
        assert found("a_b") == ["CA-underscore"]
        assert found("sure") == ["CA-percent"]

    def test_search_finds_contacts_and_numbers(self, store: Store) -> None:
        store.save_call(call("CA-pat"))
        store.save_call(call("CA-other", number="+15035559999"))
        store.put_contact(Contact(number=NUMBER, name="Pat", note="Regular", updated_at=T0))

        def found(q: str) -> list[str]:
            return [c.call_id for c in store.history(limit=10, q=q)[0]]

        assert found("pat") == ["CA-pat"]
        assert found("regular") == ["CA-pat"]
        # However it is punctuated.
        assert found("(503) 555-9999") == ["CA-other"]

    def test_a_number_filters_exactly(self, store: Store) -> None:
        store.save_call(call("CA-mine"))
        store.save_call(call("CA-theirs", number="+15035559999"))

        page, _ = store.history(limit=10, number=NUMBER)

        assert [c.call_id for c in page] == ["CA-mine"]


def test_purging_clears_old_transcripts_and_keeps_the_calls(store: Store) -> None:
    now = utc_now()
    store.save_call(call("CA-old", started_at=now - timedelta(days=91), transcript="old"))
    store.save_call(call("CA-new", started_at=now - timedelta(days=1), transcript="new"))

    assert store.purge_transcripts(0) == 0
    assert store.get_call("CA-old").transcript == "old"

    assert store.purge_transcripts(90) == 1
    assert store.get_call("CA-old").transcript is None
    assert store.get_call("CA-new").transcript == "new"


class TestFallingBackToMemory:
    """An unattended update must never turn into a backend that will not
    start: the phone line would go down with it."""

    def test_an_unusable_path_still_gives_a_working_store(self, tmp_path: Path) -> None:
        (tmp_path / "not-a-directory").touch()

        store = Store.open(tmp_path / "not-a-directory" / "screener.db")

        assert store.warning == DEGRADED_WARNING
        assert store.path == ":memory:"
        assert store.save_call(call("CA1"))
        assert store.get_call("CA1") is not None
        store.close()

    def test_the_app_still_takes_calls(self, make_client, tmp_path: Path) -> None:
        (tmp_path / "not-a-directory").touch()
        client = make_client(DATABASE_PATH=str(tmp_path / "not-a-directory" / "screener.db"))

        with client:
            response = client.post(
                "/webhook/incoming-call", data={"CallSid": "CA1", "From": NUMBER}
            )
            with client.websocket_connect("/ws/frontend") as ws:
                snapshot = ws.receive_json()
            ready = client.get("/readyz").json()

        assert "<Gather" in response.text
        assert [c["callId"] for c in snapshot["data"]["calls"]] == ["CA1"]
        assert snapshot["data"]["storageWarning"] == DEGRADED_WARNING
        assert ready["storage"] == DEGRADED_WARNING


async def test_a_failed_write_warns_every_dashboard_once(store: Store) -> None:
    broadcaster = Broadcaster(queue_max=50)
    registry = CallRegistry(broadcaster, store)
    store._conn.close()  # noqa: SLF001 -- the quickest way to make every write fail

    async with broadcaster.subscribe() as dashboard:
        registry.register_incoming("CA1", caller=Caller(number=NUMBER))
        registry.register_incoming("CA2", caller=Caller(number=NUMBER))
        events = [dashboard.queue.get_nowait() for _ in range(dashboard.queue.qsize())]

    # The calls still reach the queue: storage failing must not drop a caller.
    assert [e["type"] for e in events] == ["error", "call.incoming", "call.incoming"]
    # Marked, so the dashboard keeps it up as a banner, not a dismissable alert.
    assert events[0]["data"] == {"message": store.warning, "storage": True}
    assert "stopped saving" in store.warning
    assert [c.call_id for c in registry.open_calls()] == ["CA1", "CA2"]


def test_the_storage_line_is_in_readiness(client: TestClient) -> None:
    assert client.get("/readyz").json()["storage"] == "ok"
