"""Contacts: naming, noting, favoriting and blocking a number."""

from __future__ import annotations

import sqlite3
from xml.etree.ElementTree import fromstring

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import patch_twilio

NUMBER = "+15035551234"
INCOMING = {"CallSid": "CA-c-1", "From": NUMBER, "To": "+15039990000"}


def ring(client: TestClient, call_sid: str, number: str = NUMBER) -> None:
    client.post("/webhook/incoming-call", data={**INCOMING, "CallSid": call_sid, "From": number})


def hold(client: TestClient, call_sid: str, number: str = NUMBER) -> None:
    ring(client, call_sid, number)
    client.post(
        "/webhook/speech-result",
        data={"CallSid": call_sid, "SpeechResult": "A question", "Confidence": "0.9"},
    )


def change(client: TestClient, body: dict, number: str = NUMBER) -> httpx.Response:
    return client.patch(f"/api/contacts/{number}", json=body)


class TestEditing:
    def test_only_the_fields_sent_change(self, client: TestClient) -> None:
        """The dialog saves only what the operator touched, so two operators
        editing different fields of one contact do not undo each other."""
        change(client, {"name": "Pat", "note": "Calls about gardening"})

        contact = change(client, {"standing": "favorite"}).json()["contact"]
        assert (contact["name"], contact["note"], contact["standing"]) == (
            "Pat",
            "Calls about gardening",
            "favorite",
        )

        # Null clears that one field, and only it.
        contact = change(client, {"note": None}).json()["contact"]
        assert (contact["name"], contact["note"]) == ("Pat", None)

    def test_blank_text_is_no_text(self, client: TestClient) -> None:
        contact = change(client, {"name": "  Pat  ", "note": "   "}).json()["contact"]

        assert (contact["name"], contact["note"]) == ("Pat", None)

    def test_clearing_every_field_removes_the_contact(self, client: TestClient) -> None:
        change(client, {"name": "Pat", "standing": "favorite"})

        body = change(client, {"name": None, "standing": None}).json()

        assert body["contact"] is None
        assert client.get("/api/contacts").json() == []

    def test_the_number_is_taken_as_an_operator_types_it(self, client: TestClient) -> None:
        response = client.patch("/api/contacts/(503) 555-1234", json={"name": "Pat"})

        assert response.json()["contact"]["number"] == NUMBER

    def test_the_plus_survives_the_dashboards_encoding(self, client: TestClient) -> None:
        """encodeURIComponent sends the leading + as %2B. Without it an
        international number reads as a bare one and is refused."""
        response = client.patch("/api/contacts/%2B442079460958", json={"name": "Pat"})

        assert response.json()["contact"]["number"] == "+442079460958"

    @pytest.mark.parametrize("typed", ["anonymous", "555-0147", "+266696687"])
    def test_something_that_is_not_a_number_is_a_400(self, client: TestClient, typed: str) -> None:
        response = change(client, {"name": "Pat"}, number=typed)

        assert response.status_code == 400
        assert client.get("/api/contacts").json() == []

    def test_an_overlong_name_is_refused(self, client: TestClient) -> None:
        assert change(client, {"name": "x" * 81}).status_code == 422

    def test_a_standing_keeps_its_start_until_it_changes(self, client: TestClient) -> None:
        blocked = change(client, {"standing": "blocked"}).json()["contact"]
        ring(client, "CA-refused")

        noted = change(client, {"note": "Shouts"}).json()["contact"]
        assert noted["standingSince"] == blocked["standingSince"]
        assert noted["blockedAttempts"] == 1

        cleared = change(client, {"standing": None}).json()["contact"]
        assert (cleared["standingSince"], cleared["blockedAttempts"]) == (None, 0)


def test_the_list_puts_favorites_first_and_counts_calls(client: TestClient) -> None:
    ring(client, "CA-1")
    client.post("/api/calls/CA-1/reject")
    ring(client, "CA-2")
    change(client, {"name": "Pat"})
    change(client, {"standing": "favorite"}, number="+15035559999")

    contacts = client.get("/api/contacts").json()

    assert [(c["number"], c["calls"]) for c in contacts] == [("+15035559999", 0), (NUMBER, 2)]
    assert contacts[0]["lastCalledAt"] is None
    assert contacts[1]["lastCalledAt"] is not None


class TestBlocking:
    def test_their_calls_are_turned_away(self, client: TestClient, twilio: list[str]) -> None:
        hold(client, "CA-holding")
        ring(client, "CA-screening")
        hold(client, "CA-someone-else", number="+15035559999")

        body = change(client, {"standing": "blocked"}).json()

        assert sorted(body["endedCallIds"]) == ["CA-holding", "CA-screening"]
        assert body["failedCallIds"] == []
        assert sorted(twilio) == ["CA-holding", "CA-screening"]
        assert [c["callId"] for c in client.get("/api/calls").json()] == ["CA-someone-else"]
        assert client.get("/api/calls/CA-holding").json()["status"] == "rejected"

    def test_a_caller_twilio_could_not_reach_stays_but_the_block_holds(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Twilio first, as with Reject: a caller still connected keeps their
        place rather than vanishing, and the operator is told."""
        hold(client, "CA-holding")
        patch_twilio(monkeypatch, httpx.MockTransport(lambda r: httpx.Response(503, text="down")))

        body = change(client, {"standing": "blocked"}).json()

        assert body["contact"]["standing"] == "blocked"
        assert body["endedCallIds"] == []
        assert body["failedCallIds"] == ["CA-holding"]
        assert [c["callId"] for c in client.get("/api/calls").json()] == ["CA-holding"]
        doc = fromstring(client.post("/webhook/incoming-call", data=INCOMING).text)
        assert [child.tag for child in doc] == ["Reject"]

    def test_no_credentials_counts_as_could_not_reach(self, make_client) -> None:
        with make_client(TWILIO_ACCOUNT_SID="", TWILIO_API_KEY_SID="") as client:
            hold(client, "CA-holding")

            body = change(client, {"standing": "blocked"}).json()

        assert body["failedCallIds"] == ["CA-holding"]

    def test_a_caller_on_air_is_never_cut_off(self, client: TestClient, twilio: list[str]) -> None:
        hold(client, "CA-on-air")
        client.post("/api/calls/CA-on-air/accept")
        twilio.clear()

        body = change(client, {"standing": "blocked"}).json()

        assert (body["endedCallIds"], body["failedCallIds"], twilio) == ([], [], [])
        assert client.get("/api/calls/CA-on-air").json()["status"] == "accepted"

    def test_blocking_again_turns_nobody_away(self, client: TestClient, twilio: list[str]) -> None:
        """Only a change to blocked ends calls -- editing a blocked contact's
        note must not hang up on anyone."""
        change(client, {"standing": "blocked"})
        twilio.clear()

        body = change(client, {"note": "Shouts", "standing": "blocked"}).json()

        assert (body["endedCallIds"], twilio) == ([], [])


def test_dashboards_hear_about_it(client: TestClient) -> None:
    """The call in play shows the change as an ordinary call.updated; the
    contacts list and history hear contact.changed."""
    hold(client, "CA-holding")

    with client.websocket_connect("/ws/frontend") as ws:
        ws.receive_json()  # snapshot
        change(client, {"name": "Pat"})
        named = [ws.receive_json(), ws.receive_json()]
        change(client, {"name": None})
        removed = [ws.receive_json(), ws.receive_json()]

    assert [e["type"] for e in named] == ["call.updated", "contact.changed"]
    assert named[0]["data"]["call"]["caller"]["contact"]["name"] == "Pat"
    assert named[1]["data"]["number"] == NUMBER
    assert named[1]["data"]["contact"]["name"] == "Pat"
    assert removed[0]["data"]["call"]["caller"]["contact"] is None
    assert removed[1]["data"] == {"number": NUMBER, "contact": None}


def test_a_contact_that_cannot_be_saved_is_a_503(client: TestClient) -> None:
    """And every dashboard is told storage has failed -- once."""
    store = client.app.state.store
    broken = sqlite3.connect(":memory:", check_same_thread=False)
    broken.close()
    real, store._conn = store._conn, broken  # noqa: SLF001
    try:
        with client.websocket_connect("/ws/frontend") as ws:
            ws.receive_json()  # snapshot
            response = change(client, {"name": "Pat"})
            change(client, {"name": "Pat"})
            ws.send_json({"type": "ping"})
            events = [ws.receive_json()["type"], ws.receive_json()["type"]]
    finally:
        store._conn = real  # noqa: SLF001

    assert response.status_code == 503
    assert response.json()["detail"] == "Couldn't save the contact"
    assert events == ["error", "pong"]
