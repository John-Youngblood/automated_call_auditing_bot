"""The Twilio call flow: greet, listen, hold."""

from __future__ import annotations

from xml.etree.ElementTree import fromstring

from fastapi.testclient import TestClient

from app.config import get_settings
from app.telephony import TWILIO_HOLD_MUSIC
from app.telephony.signature import compute_twilio_signature

TWILIO_FORM = {
    "CallSid": "CA0123456789",
    "From": "+15551230000",
    "To": "+15559990000",
    "CallerName": "Listener Line",
    "FromCity": "Portland",
    "FromState": "OR",
    "FromCountry": "US",
    # Twilio's own vocabulary for the call leg, unrelated to our CallStatus.
    "CallStatus": "ringing",
}


def test_greeting_is_the_prompt_and_twilio_listens_after_it(client: TestClient) -> None:
    response = client.post("/webhook/incoming-call", data=TWILIO_FORM)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    root = fromstring(response.text)

    gather = root.find("Gather")
    assert gather is not None
    assert gather.attrib["input"] == "speech"
    # What makes the design turn-based: Twilio decides when the caller stopped,
    # so nothing here has to stream or analyse audio.
    #
    # A number, never "auto". Twilio warns (error 13335) when "auto" is paired
    # with a speechModel, and we set one -- and "auto" stops at the first pause
    # in speech, which truncates a caller mid-explanation.
    assert gather.attrib["speechTimeout"].isdigit()
    assert gather.attrib["speechModel"] == "phone_call"
    # A silent caller must still reach the dashboard rather than vanishing.
    assert gather.attrib["actionOnEmptyResult"] == "true"
    assert gather.attrib["action"] == "https://calls.example.test/webhook/speech-result"

    # Nested inside <Gather>: the greeting doubles as the prompt and a caller
    # who talks over it is still heard. Spoken here, since no audio is set.
    assert gather.find("Say") is not None

    # Fallback so a fallen-through <Gather> does not run off the end of the
    # document and hang up on the caller.
    assert root.findtext("Redirect") == "https://calls.example.test/webhook/speech-result"


def test_call_enters_the_queue_with_caller_details(client: TestClient) -> None:
    client.post("/webhook/incoming-call", data=TWILIO_FORM)

    calls = client.get("/api/calls").json()
    assert len(calls) == 1
    assert calls[0]["callId"] == "CA0123456789"
    assert calls[0]["status"] == "screening"
    assert calls[0]["caller"]["number"] == "+15551230000"
    # State, not country: "Portland, US" tells a US operator nothing.
    assert calls[0]["caller"]["location"] == "Portland, OR"
    assert calls[0]["transcript"] is None


def test_retried_webhook_does_not_duplicate_the_call(client: TestClient) -> None:
    for _ in range(3):
        assert client.post("/webhook/incoming-call", data=TWILIO_FORM).status_code == 200

    assert len(client.get("/api/calls").json()) == 1


class TestSpeechResult:
    def speak(self, client: TestClient, text: str, confidence: str = "0.94"):
        return client.post(
            "/webhook/speech-result",
            data={"CallSid": "CA0123456789", "SpeechResult": text, "Confidence": confidence},
        )

    def test_transcript_lands_on_the_call_and_the_caller_is_held(self, client: TestClient) -> None:
        client.post("/webhook/incoming-call", data=TWILIO_FORM)

        response = self.speak(client, "I have a question for your guest.")

        assert response.status_code == 200
        # <Enqueue> holds the call open -- no queue to pre-create in Twilio.
        enqueue = fromstring(response.text).find("Enqueue")
        assert enqueue is not None
        assert enqueue.text == "screening"
        # Without an action URL nothing ever tells us the caller gave up while
        # holding -- there is no other connection to this service.
        assert enqueue.attrib["action"] == "https://calls.example.test/webhook/queue-exit"
        # Ours rather than the music: see TestHoldMusic and TestMaxHold.
        assert enqueue.attrib["waitUrl"] == "https://calls.example.test/webhook/hold-wait"

        call = client.get("/api/calls/CA0123456789").json()
        assert call["transcript"] == "I have a question for your guest."
        assert call["transcriptConfidence"] == 0.94
        # Now awaiting a human, rather than still talking.
        assert call["status"] == "on-hold"

    def test_silent_caller_still_reaches_the_dashboard(self, client: TestClient) -> None:
        """actionOnEmptyResult fires with no SpeechResult. The call must still
        be actionable rather than sitting in limbo."""
        client.post("/webhook/incoming-call", data=TWILIO_FORM)

        response = self.speak(client, "")

        assert response.status_code == 200
        call = client.get("/api/calls/CA0123456789").json()
        assert call["transcript"] is None
        assert call["status"] == "on-hold"

    def test_missing_confidence_is_tolerated(self, client: TestClient) -> None:
        client.post("/webhook/incoming-call", data=TWILIO_FORM)

        response = client.post(
            "/webhook/speech-result",
            data={"CallSid": "CA0123456789", "SpeechResult": "hello"},
        )

        assert response.status_code == 200
        assert client.get("/api/calls/CA0123456789").json()["transcriptConfidence"] is None

    def test_transcript_reaches_dashboards(self, client: TestClient) -> None:
        client.post("/webhook/incoming-call", data=TWILIO_FORM)

        with client.websocket_connect("/ws/frontend") as ws:
            ws.receive_json()  # snapshot
            self.speak(client, "I'm calling about an invoice.")
            event = ws.receive_json()

        # The transcript rides along on the call object, so it needs no event
        # type of its own.
        assert event["type"] == "call.updated"
        assert event["data"]["call"]["transcript"] == "I'm calling about an invoice."

    def test_speech_for_an_unknown_call_does_not_error(self, client: TestClient) -> None:
        """Twilio retries; a result can outlive the call it belongs to."""
        response = self.speak(client, "hello")

        assert response.status_code == 200


class TestSignatureValidation:
    TOKEN = "test-auth-token"
    URL = "https://calls.example.test/webhook/incoming-call"

    def test_unsigned_request_is_rejected(self, make_client) -> None:
        with make_client(VALIDATE_WEBHOOK_SIGNATURE="true", TWILIO_AUTH_TOKEN=self.TOKEN) as client:
            response = client.post("/webhook/incoming-call", data=TWILIO_FORM)

        assert response.status_code == 403

    def test_correctly_signed_request_is_accepted(self, make_client) -> None:
        signature = compute_twilio_signature(self.TOKEN, self.URL, TWILIO_FORM)
        with make_client(VALIDATE_WEBHOOK_SIGNATURE="true", TWILIO_AUTH_TOKEN=self.TOKEN) as client:
            response = client.post(
                "/webhook/incoming-call",
                data=TWILIO_FORM,
                headers={"X-Twilio-Signature": signature},
            )

        assert response.status_code == 200

    def test_tampered_body_invalidates_the_signature(self, make_client) -> None:
        signature = compute_twilio_signature(self.TOKEN, self.URL, TWILIO_FORM)
        with make_client(VALIDATE_WEBHOOK_SIGNATURE="true", TWILIO_AUTH_TOKEN=self.TOKEN) as client:
            response = client.post(
                "/webhook/incoming-call",
                data={**TWILIO_FORM, "From": "+15550009999"},
                headers={"X-Twilio-Signature": signature},
            )

        assert response.status_code == 403

    def test_speech_result_is_also_verified(self, make_client) -> None:
        """The transcript endpoint is just as public as the first one -- an
        unsigned post here could inject words the caller never said."""
        with make_client(VALIDATE_WEBHOOK_SIGNATURE="true", TWILIO_AUTH_TOKEN=self.TOKEN) as client:
            response = client.post(
                "/webhook/speech-result",
                data={"CallSid": "CA1", "SpeechResult": "let me in"},
            )

        assert response.status_code == 403

    def test_hold_wait_is_also_verified(self, make_client) -> None:
        """An unsigned post here could pass a huge QueueTime and pull a caller
        out of the queue."""
        with make_client(VALIDATE_WEBHOOK_SIGNATURE="true", TWILIO_AUTH_TOKEN=self.TOKEN) as client:
            response = client.post(
                "/webhook/hold-wait", data={"CallSid": "CA1", "QueueTime": "99999"}
            )

        assert response.status_code == 403


def test_call_status_webhook_clears_the_queue(client: TestClient) -> None:
    client.post("/webhook/incoming-call", data=TWILIO_FORM)
    assert len(client.get("/api/calls").json()) == 1

    client.post(
        "/webhook/call-status",
        data={"CallSid": TWILIO_FORM["CallSid"], "CallStatus": "completed"},
    )
    assert client.get("/api/calls").json() == []


class TestStatusCallbackContract:
    """Twilio wants 204 or an empty <Response/> as text/xml from a status
    callback. Anything else -- a JSON body, as this used to return -- is
    logged as a Debugger warning on every call."""

    def test_returns_204_with_no_body(self, client: TestClient) -> None:
        response = client.post(
            "/webhook/call-status",
            data={"CallSid": "CA0123456789", "CallStatus": "completed"},
        )

        assert response.status_code == 204
        assert response.content == b""

    def test_non_terminal_statuses_are_ignored(self, client: TestClient) -> None:
        client.post("/webhook/incoming-call", data=TWILIO_FORM)

        for state in ("queued", "initiated", "ringing", "in-progress"):
            client.post(
                "/webhook/call-status",
                data={"CallSid": TWILIO_FORM["CallSid"], "CallStatus": state},
            )

        # Still live -- a ringing notification must not clear the queue.
        assert len(client.get("/api/calls").json()) == 1

    def test_every_terminal_status_ends_the_call(self, client: TestClient) -> None:
        for state in ("completed", "busy", "failed", "no-answer", "canceled"):
            call_id = f"CA-{state}"
            client.post("/webhook/incoming-call", data={**TWILIO_FORM, "CallSid": call_id})
            client.post("/webhook/call-status", data={"CallSid": call_id, "CallStatus": state})
            assert client.get(f"/api/calls/{call_id}").json()["status"] == "ended"


class TestQueueExit:
    """Twilio requests the <Enqueue> action URL when a call leaves the queue.
    QueueResult says why -- and `hangup` is the only thing that tells us a
    caller gave up while holding."""

    def hold_then_exit(self, client: TestClient, result: str, waited: str = "42"):
        client.post("/webhook/incoming-call", data=TWILIO_FORM)
        client.post(
            "/webhook/speech-result",
            data={"CallSid": TWILIO_FORM["CallSid"], "SpeechResult": "Hello."},
        )
        return client.post(
            "/webhook/queue-exit",
            data={
                "CallSid": TWILIO_FORM["CallSid"],
                "QueueResult": result,
                "QueueTime": waited,
            },
        )

    def test_hangup_ends_the_call(self, client: TestClient) -> None:
        response = self.hold_then_exit(client, "hangup")

        assert response.status_code == 200
        assert client.get("/api/calls").json() == []
        assert client.get(f"/api/calls/{TWILIO_FORM['CallSid']}").json()["status"] == "ended"

    def test_returns_twiml_not_204(self, client: TestClient) -> None:
        """An action URL controls call flow, so unlike a status callback it
        must answer with TwiML."""
        response = self.hold_then_exit(client, "hangup")

        assert response.headers["content-type"].startswith("application/xml")
        assert fromstring(response.text).tag == "Response"

    def test_abandonment_is_broadcast(self, client: TestClient) -> None:
        client.post("/webhook/incoming-call", data=TWILIO_FORM)
        with client.websocket_connect("/ws/frontend") as ws:
            ws.receive_json()  # snapshot
            client.post(
                "/webhook/queue-exit",
                data={"CallSid": TWILIO_FORM["CallSid"], "QueueResult": "hangup"},
            )
            event = ws.receive_json()

        assert event["type"] == "call.ended"
        assert event["data"]["call"]["status"] == "ended"

    def test_being_connected_leaves_the_decision_alone(self, client: TestClient) -> None:
        """`bridged` means they reached a human, so an accepted call must not
        be downgraded to `ended` when the queue reports the exit."""
        client.post("/webhook/incoming-call", data=TWILIO_FORM)
        client.post(f"/api/calls/{TWILIO_FORM['CallSid']}/accept")

        client.post(
            "/webhook/queue-exit",
            data={"CallSid": TWILIO_FORM["CallSid"], "QueueResult": "bridged"},
        )

        assert client.get(f"/api/calls/{TWILIO_FORM['CallSid']}").json()["status"] == "accepted"

    def test_every_abandon_result_ends_the_call(self, client: TestClient) -> None:
        for result in ("hangup", "error", "system-error", "queue-full"):
            call_id = f"CA-q-{result}"
            client.post("/webhook/incoming-call", data={**TWILIO_FORM, "CallSid": call_id})
            client.post("/webhook/queue-exit", data={"CallSid": call_id, "QueueResult": result})
            assert client.get(f"/api/calls/{call_id}").json()["status"] == "ended"

    def test_unknown_call_does_not_error(self, client: TestClient) -> None:
        response = client.post(
            "/webhook/queue-exit", data={"CallSid": "CA-ghost", "QueueResult": "hangup"}
        )

        assert response.status_code == 200


def hold_wait(client: TestClient, waited: str = "0"):
    """What Twilio gets back when it asks what a holding caller hears next."""
    response = client.post(
        "/webhook/hold-wait", data={"CallSid": TWILIO_FORM["CallSid"], "QueueTime": waited}
    )
    assert response.status_code == 200
    return fromstring(response.text)


class TestHoldMusic:
    """Twilio asks /webhook/hold-wait what to play each time a track ends."""

    def test_the_wait_url_is_ours_and_posted(self, client: TestClient) -> None:
        """Ours, so every loop can check how long they have waited. POST, so the
        TwiML answer is never cached -- the audio inside it still is."""
        client.post("/webhook/incoming-call", data=TWILIO_FORM)
        response = client.post(
            "/webhook/speech-result",
            data={"CallSid": TWILIO_FORM["CallSid"], "SpeechResult": "Hello"},
        )
        enqueue = fromstring(response.text).find("Enqueue")

        assert enqueue.attrib["waitUrl"] == "https://calls.example.test/webhook/hold-wait"
        assert enqueue.attrib["waitUrlMethod"] == "POST"

    def test_blank_plays_twilios_own_music(self, client: TestClient) -> None:
        assert hold_wait(client).findtext("Play") in TWILIO_HOLD_MUSIC

    def test_an_absolute_url_is_used_as_given(self, make_client) -> None:
        with make_client(HOLD_MUSIC_URL="https://cdn.example.test/hold.mp3") as client:
            assert hold_wait(client).findtext("Play") == "https://cdn.example.test/hold.mp3"

    def test_a_bare_filename_is_served_from_this_backend(self, make_client, static_dir) -> None:
        """The form that survives a rotating tunnel: .env cannot interpolate
        PUBLIC_BASE_URL, so a filename is rebuilt against the current base."""
        (static_dir / "h3_podcast_theme.mp3").touch()
        with make_client(HOLD_MUSIC_URL="h3_podcast_theme.mp3") as client:
            assert (
                hold_wait(client).findtext("Play")
                == "https://calls.example.test/static/h3_podcast_theme.mp3"
            )

    def test_a_leading_slash_does_not_double_up(self, make_client, static_dir) -> None:
        (static_dir / "theme.mp3").touch()
        with make_client(HOLD_MUSIC_URL="/theme.mp3") as client:
            assert hold_wait(client).findtext("Play") == "https://calls.example.test/static/theme.mp3"

    def test_a_listed_file_that_is_missing_gets_twilios_music(self, make_client) -> None:
        with make_client(HOLD_MUSIC_URL="not-there.mp3") as client:
            assert hold_wait(client).findtext("Play") in TWILIO_HOLD_MUSIC


class TestMaxHold:
    """A hold ends at MAX_HOLD_MINUTES: hold-wait answers <Leave>, and the
    queue exit that follows plays the reject message before hanging up."""

    def test_the_default_is_an_hour(self, client: TestClient) -> None:
        assert hold_wait(client, "3599").find("Leave") is None
        assert [child.tag for child in hold_wait(client, "3600")] == ["Leave"]

    def test_the_limit_is_configurable(self, make_client) -> None:
        with make_client(MAX_HOLD_MINUTES="20") as client:
            assert hold_wait(client, "1199").find("Play") is not None
            assert [child.tag for child in hold_wait(client, "1200")] == ["Leave"]

    def test_zero_means_no_limit(self, make_client) -> None:
        """Twilio's own 4-hour cap on any call still applies."""
        with make_client(MAX_HOLD_MINUTES="0") as client:
            assert hold_wait(client, str(4 * 60 * 60)).find("Leave") is None

    def test_a_garbled_queue_time_keeps_them_holding(self, client: TestClient) -> None:
        """Better one more track than hanging up on someone by accident."""
        assert hold_wait(client, "soon").find("Play") is not None

    def test_leaving_plays_the_reject_message_and_hangs_up(self, make_client) -> None:
        with make_client(REJECT_MESSAGE="Sorry, we ran out of time.") as client:
            client.post("/webhook/incoming-call", data=TWILIO_FORM)
            client.post(
                "/webhook/speech-result",
                data={"CallSid": TWILIO_FORM["CallSid"], "SpeechResult": "Hello"},
            )
            response = client.post(
                "/webhook/queue-exit",
                data={
                    "CallSid": TWILIO_FORM["CallSid"],
                    "QueueResult": "leave",
                    "QueueTime": "3600",
                },
            )
            doc = fromstring(response.text)

            assert [child.tag for child in doc] == ["Say", "Hangup"]
            assert doc.findtext("Say") == "Sorry, we ran out of time."
            # Off the live queue and into history, like any caller who left.
            assert client.get("/api/calls").json() == []
            assert client.get(f"/api/calls/{TWILIO_FORM['CallSid']}").json()["status"] == "ended"

    def test_the_reject_recording_is_used_when_set(self, make_client, static_dir) -> None:
        (static_dir / "bye.mp3").touch()
        with make_client(REJECT_AUDIO_URL="bye.mp3") as client:
            response = client.post(
                "/webhook/queue-exit", data={"CallSid": "CA-any", "QueueResult": "leave"}
            )
        doc = fromstring(response.text)

        assert [child.tag for child in doc] == ["Play", "Hangup"]
        assert doc.findtext("Play") == "https://calls.example.test/static/bye.mp3"


class TestGreetingResolution:
    """The greeting resolves like every other prompt: audio if set, else text."""

    def greeting_url(self, client: TestClient) -> str:
        response = client.post("/webhook/incoming-call", data=TWILIO_FORM)
        return fromstring(response.text).find("Gather/Play").text

    def test_unset_speaks_the_message_even_with_a_recording_on_disk(
        self, make_client, static_dir
    ) -> None:
        """static/greeting.mp3 exists, and blank must still mean speak.

        It used to fall back to that file, so clearing GREETING_AUDIO_URL in
        production kept playing the old recording -- the setting said one
        thing and the caller heard another.
        """
        (static_dir / "greeting.mp3").touch()
        client = make_client(GREETING_MESSAGE="Tell us why you are calling.")
        with client:
            response = client.post("/webhook/incoming-call", data=TWILIO_FORM)
        gather = fromstring(response.text).find("Gather")

        assert gather.find("Play") is None
        assert gather.findtext("Say") == "Tell us why you are calling."
        assert gather.find("Say").attrib["voice"] == "Polly.Joanna"

    def test_an_absolute_url_is_used_as_given(self, make_client) -> None:
        client = make_client(GREETING_AUDIO_URL="https://cdn.example.test/hi.mp3")
        with client:
            assert self.greeting_url(client) == "https://cdn.example.test/hi.mp3"

    def test_a_bare_filename_is_served_from_this_backend(self, make_client, static_dir) -> None:
        (static_dir / "intro.mp3").touch()
        client = make_client(GREETING_AUDIO_URL="intro.mp3")
        with client:
            assert self.greeting_url(client) == "https://calls.example.test/static/intro.mp3"

    def test_a_listed_file_that_is_missing_is_spoken_instead(self, make_client) -> None:
        """A typo in the filename must not leave the caller in silence."""
        client = make_client(
            GREETING_AUDIO_URL="gretting.mp3", GREETING_MESSAGE="Tell us why you are calling."
        )
        with client:
            response = client.post("/webhook/incoming-call", data=TWILIO_FORM)
        gather = fromstring(response.text).find("Gather")

        assert gather.find("Play") is None
        assert gather.findtext("Say") == "Tell us why you are calling."

    def test_missing_files_are_reported(self, configure_env, static_dir) -> None:
        """What main.py warns about at startup. Absolute URLs are never listed:
        they are trusted, not fetched."""
        (static_dir / "here.mp3").touch()
        configure_env(
            GREETING_AUDIO_URL="gretting.mp3",
            HOLD_MUSIC_URL="here.mp3",
            REJECT_AUDIO_URL="https://cdn.example.test/sorry.mp3",
        )
        assert get_settings().missing_audio_files() == ["GREETING_AUDIO_URL=gretting.mp3"]
