"""Named callers and favourites."""

from __future__ import annotations

from fastapi.testclient import TestClient

NUMBER = "+15035550142"
INCOMING = {"CallSid": "CA-contact-1", "From": NUMBER, "To": "+15039990000"}


def ring(client: TestClient, **overrides: str) -> None:
    client.post("/webhook/incoming-call", data={**INCOMING, **overrides})


def speak(client: TestClient, call_id: str = "CA-contact-1", text: str = "Hello there.") -> None:
    client.post("/webhook/speech-result", data={"CallSid": call_id, "SpeechResult": text})


class TestNaming:
    def test_saved_name_is_applied_to_a_new_call(self, client: TestClient) -> None:
        client.post("/api/contacts", json={"number": NUMBER, "displayName": "Dana Rivera"})

        ring(client)

        caller = client.get("/api/calls/CA-contact-1").json()["caller"]
        assert caller["name"] == "Dana Rivera"
        assert caller["number"] == NUMBER

    def test_saved_name_beats_the_carrier_caller_id(self, client: TestClient) -> None:
        """CNAM is frequently stale or generic; a name someone typed is worth
        more than 'WIRELESS CALLER'."""
        client.post("/api/contacts", json={"number": NUMBER, "displayName": "Dana Rivera"})

        ring(client, CallerName="WIRELESS CALLER")

        assert client.get("/api/calls/CA-contact-1").json()["caller"]["name"] == "Dana Rivera"

    def test_carrier_name_is_used_when_there_is_no_contact(self, client: TestClient) -> None:
        ring(client, CallerName="Northgate Medical")

        assert client.get("/api/calls/CA-contact-1").json()["caller"]["name"] == "Northgate Medical"

    def test_name_matches_regardless_of_formatting(self, client: TestClient) -> None:
        client.post("/api/contacts", json={"number": "(503) 555-0142", "displayName": "Dana"})

        ring(client, **{"From": "+15035550142"})

        assert client.get("/api/calls/CA-contact-1").json()["caller"]["name"] == "Dana"

    def test_unusable_number_is_a_400(self, client: TestClient) -> None:
        response = client.post("/api/contacts", json={"number": "nope", "displayName": "X"})

        assert response.status_code == 400
        assert client.get("/api/contacts").json() == []

    def test_empty_payload_is_rejected(self, client: TestClient) -> None:
        """Neither field set means the request asks for nothing."""
        assert client.post("/api/contacts", json={"number": NUMBER}).status_code == 422


class TestFavorites:
    def test_starring_shows_on_a_new_call(self, client: TestClient) -> None:
        client.post("/api/contacts", json={"number": NUMBER, "isFavorite": True})

        ring(client)

        assert client.get("/api/calls/CA-contact-1").json()["caller"]["isFavorite"] is True

    def test_unknown_caller_is_not_a_favorite(self, client: TestClient) -> None:
        ring(client)

        assert client.get("/api/calls/CA-contact-1").json()["caller"]["isFavorite"] is False

    def test_starring_does_not_clear_an_existing_name(self, client: TestClient) -> None:
        """Partial updates are the norm -- the star button sends only
        isFavorite, and must not wipe a name someone else typed."""
        client.post("/api/contacts", json={"number": NUMBER, "displayName": "Dana Rivera"})

        client.post("/api/contacts", json={"number": NUMBER, "isFavorite": True})

        contact = client.get("/api/contacts").json()[0]
        assert contact["displayName"] == "Dana Rivera"
        assert contact["isFavorite"] is True

    def test_naming_does_not_clear_the_star(self, client: TestClient) -> None:
        client.post("/api/contacts", json={"number": NUMBER, "isFavorite": True})

        client.post("/api/contacts", json={"number": NUMBER, "displayName": "Dana"})

        contact = client.get("/api/contacts").json()[0]
        assert contact["isFavorite"] is True
        assert contact["displayName"] == "Dana"

    def test_unstarring_a_named_caller_keeps_the_name(self, client: TestClient) -> None:
        client.post(
            "/api/contacts",
            json={"number": NUMBER, "displayName": "Dana", "isFavorite": True},
        )

        client.post("/api/contacts", json={"number": NUMBER, "isFavorite": False})

        contact = client.get("/api/contacts").json()[0]
        assert contact["isFavorite"] is False
        assert contact["displayName"] == "Dana"

    def test_listing_puts_favorites_first(self, client: TestClient) -> None:
        client.post("/api/contacts", json={"number": "+15035550001", "displayName": "Zoe"})
        client.post("/api/contacts", json={"number": "+15035550002", "displayName": "Adam"})
        client.post(
            "/api/contacts",
            json={"number": "+15035550003", "displayName": "Mia", "isFavorite": True},
        )

        listed = client.get("/api/contacts").json()

        assert [c["displayName"] for c in listed] == ["Mia", "Adam", "Zoe"]


class TestLiveUpdates:
    def test_naming_a_caller_mid_call_updates_the_dashboard(self, client: TestClient) -> None:
        """The call resolved its name when it arrived, so it holds stale
        details until the contact change is pushed out."""
        ring(client)

        with client.websocket_connect("/ws/frontend") as ws:
            ws.receive_json()  # snapshot
            client.post(
                "/api/contacts",
                json={"number": NUMBER, "displayName": "Dana Rivera", "isFavorite": True},
            )
            event = ws.receive_json()

        assert event["type"] == "call.updated"
        assert event["data"]["call"]["caller"]["name"] == "Dana Rivera"
        assert event["data"]["call"]["caller"]["isFavorite"] is True

    def test_only_that_caller_s_calls_are_touched(self, client: TestClient) -> None:
        ring(client)
        ring(client, CallSid="CA-other", From="+15035559999")

        client.post("/api/contacts", json={"number": NUMBER, "isFavorite": True})

        assert client.get("/api/calls/CA-contact-1").json()["caller"]["isFavorite"] is True
        assert client.get("/api/calls/CA-other").json()["caller"]["isFavorite"] is False


class TestHistory:
    def test_naming_retroactively_labels_past_calls(self, client: TestClient) -> None:
        """Names resolve against the current contact, not the one stored on the
        row -- so naming someone labels every call they ever made."""
        ring(client)
        speak(client)
        client.post("/api/calls/CA-contact-1/reject")
        assert client.get("/api/call-history").json()[0]["fromName"] is None

        client.post("/api/contacts", json={"number": NUMBER, "displayName": "Dana Rivera"})

        row = client.get("/api/call-history").json()[0]
        assert row["fromName"] == "Dana Rivera"

    def test_history_reflects_the_current_star(self, client: TestClient) -> None:
        ring(client)
        client.post("/api/calls/CA-contact-1/reject")
        assert client.get("/api/call-history").json()[0]["isFavorite"] is False

        client.post("/api/contacts", json={"number": NUMBER, "isFavorite": True})

        assert client.get("/api/call-history").json()[0]["isFavorite"] is True


class TestDeletion:
    def test_forgetting_a_contact(self, client: TestClient) -> None:
        client.post("/api/contacts", json={"number": NUMBER, "displayName": "Dana"})

        response = client.delete(f"/api/contacts/{NUMBER.replace('+', '%2B')}")

        assert response.status_code == 200
        assert client.get("/api/contacts").json() == []

    def test_forgetting_clears_the_name_on_a_live_call(self, client: TestClient) -> None:
        client.post("/api/contacts", json={"number": NUMBER, "isFavorite": True})
        ring(client)
        assert client.get("/api/calls/CA-contact-1").json()["caller"]["isFavorite"] is True

        client.delete(f"/api/contacts/{NUMBER.replace('+', '%2B')}")

        assert client.get("/api/calls/CA-contact-1").json()["caller"]["isFavorite"] is False

    def test_forgetting_an_unknown_contact_is_404(self, client: TestClient) -> None:
        assert client.delete("/api/contacts/%2B15039998888").status_code == 404


def test_a_caller_can_be_both_named_and_blocked(client: TestClient) -> None:
    """Not a contradiction: the point of naming a nuisance caller is to
    recognise them on sight."""
    client.post("/api/contacts", json={"number": NUMBER, "displayName": "Repeat Nuisance"})
    client.post("/api/block-number", json={"number": NUMBER})

    ring(client)
    client.post("/api/calls/CA-contact-1/reject")

    row = client.get("/api/call-history").json()[0]
    assert row["fromName"] == "Repeat Nuisance"
    assert row["isBlocked"] is True


class TestSelfCleaning:
    def test_unstarring_an_unnamed_caller_drops_the_row(self, client: TestClient) -> None:
        """Otherwise every star-then-unstar leaves an empty row behind."""
        client.post("/api/contacts", json={"number": NUMBER, "isFavorite": True})
        assert len(client.get("/api/contacts").json()) == 1

        client.post("/api/contacts", json={"number": NUMBER, "isFavorite": False})

        assert client.get("/api/contacts").json() == []

    def test_clearing_the_name_of_a_favorite_keeps_the_row(self, client: TestClient) -> None:
        """The star is still information worth keeping."""
        client.post(
            "/api/contacts",
            json={"number": NUMBER, "displayName": "Dana", "isFavorite": True},
        )

        client.post("/api/contacts", json={"number": NUMBER, "displayName": ""})

        remaining = client.get("/api/contacts").json()
        assert len(remaining) == 1
        assert remaining[0]["displayName"] is None
        assert remaining[0]["isFavorite"] is True

    def test_clearing_a_name_with_no_star_drops_the_row(self, client: TestClient) -> None:
        client.post("/api/contacts", json={"number": NUMBER, "displayName": "Dana"})

        client.post("/api/contacts", json={"number": NUMBER, "displayName": ""})

        assert client.get("/api/contacts").json() == []

    def test_dropping_a_contact_clears_it_from_a_live_call(self, client: TestClient) -> None:
        client.post("/api/contacts", json={"number": NUMBER, "isFavorite": True})
        ring(client)
        assert client.get("/api/calls/CA-contact-1").json()["caller"]["isFavorite"] is True

        client.post("/api/contacts", json={"number": NUMBER, "isFavorite": False})

        assert client.get("/api/calls/CA-contact-1").json()["caller"]["isFavorite"] is False
