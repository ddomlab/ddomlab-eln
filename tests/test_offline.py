"""Tests that need no ELN: the package's own helpers, and what happens when the network fails."""
import datetime
import http.server
import socket
import threading

import pytest
import urllib3

import ddomlab_eln.client as client
from ddomlab_eln import ELN, ELNError, ExperimentSummary, show_experiments


# --- Connecting without a server


def test_no_key_is_refused_before_any_network(monkeypatch):
    monkeypatch.delenv("ELN_API_KEY", raising=False)
    with pytest.raises(ELNError, match="No API key found"):
        ELN(url="http://localhost:1")


@pytest.mark.parametrize("url", ["http://localhost:1", "http://no-such-host.invalid"])
def test_unreachable_server_is_one_clear_error(url):
    with pytest.raises(ELNError, match="Couldn't reach the ELN") as caught:
        ELN(url=url, api_key="x")
    assert caught.value.__suppress_context__


def test_no_retry_warnings_on_screen(caplog):
    with pytest.raises(ELNError):
        ELN(url="http://localhost:1", api_key="x")
    assert "Retrying" not in caplog.text


def test_silent_server_times_out(monkeypatch):
    monkeypatch.setattr(client, "TIMEOUT", (1, 1))
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen(4)
        with pytest.raises(ELNError, match="didn't answer in time"):
            ELN(url=f"http://127.0.0.1:{silent.getsockname()[1]}", api_key="x")


def _guarded_sender(failures):
    """A guarded sending function whose first attempts raise the given errors, then succeed."""
    import elabapi_python

    rest = elabapi_python.ApiClient(elabapi_python.Configuration()).rest_client
    calls = []

    def send(*args, **kwargs):
        calls.append(1)
        if len(calls) <= len(failures):
            raise failures[len(calls) - 1]
        return "sent"

    rest.request = send
    client._guard_connection(rest, "test")
    return rest, calls


def _failed(reason):
    return urllib3.exceptions.MaxRetryError(None, "/", reason)


def _connect_failure():
    return _failed(urllib3.exceptions.ConnectTimeoutError(None, "/", "timed out"))


def test_dropped_connection_is_tried_again():
    rest, calls = _guarded_sender([_connect_failure(), _connect_failure()])
    assert rest.request("POST", "http://x") == "sent"
    assert len(calls) == 3


def test_gives_up_after_three_connection_attempts():
    rest, calls = _guarded_sender([_connect_failure() for _ in range(5)])
    with pytest.raises(ELNError, match="Couldn't reach the ELN"):
        rest.request("POST", "http://x")
    assert len(calls) == 3


def test_a_request_that_was_sent_is_never_repeated():
    # A read timeout means the ELN may have saved it, so sending it again could make a duplicate
    rest, calls = _guarded_sender([_failed(urllib3.exceptions.ReadTimeoutError(None, "/", "timed out"))])
    with pytest.raises(ELNError, match="didn't answer in time"):
        rest.request("POST", "http://x")
    assert len(calls) == 1


class _Redirecting(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/start":
            self.send_response(308)
            self.send_header("Location", "/end")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def test_redirects_are_still_followed():
    # e.g. someone writes http:// for an ELN that sends them on to https://
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirecting)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        import elabapi_python

        rest = elabapi_python.ApiClient(elabapi_python.Configuration()).rest_client
        client._guard_connection(rest, "test")
        response = rest.GET(f"http://127.0.0.1:{server.server_address[1]}/start")
        assert response.status == 200 and response.data == b"ok"
    finally:
        server.shutdown()


# --- Checking fields before anything is sent

KNOWN = {
    "Sample ID": {"type": "text", "value": "", "required": True},
    "Mode": {"type": "select", "value": "fast"},
}


def test_good_fields_have_no_problems():
    assert client._field_problems(KNOWN, {"Sample ID": "S-1"}, "template #1", every_required=True) == []


def test_typo_gets_a_suggestion():
    problems = client._field_problems(KNOWN, {"Sample ID": "x", "Sampel ID": "y"}, "template #1", every_required=True)
    assert problems == ['"Sampel ID" is not a field of template #1. Did you mean "Sample ID"?']


@pytest.mark.parametrize("fields", [{}, {"Sample ID": ""}, {"Sample ID": None}])
def test_empty_required_field_is_a_problem_when_creating(fields):
    assert client._field_problems(KNOWN, fields, "t", every_required=True) == ['"Sample ID" is required.']


def test_zero_counts_as_a_value():
    assert client._field_problems(KNOWN, {"Sample ID": 0}, "t", every_required=True) == []


def test_updating_only_checks_the_fields_being_set():
    assert client._field_problems(KNOWN, {"Mode": "slow"}, "t", every_required=False) == []
    assert client._field_problems(KNOWN, {"Sample ID": ""}, "t", every_required=False) == ['"Sample ID" is required.']


# --- Small helpers


@pytest.mark.parametrize("stored, wanted, expected", [
    ("S-1", "S-1", True),
    ("S-12", "S-1", False),
    ("S-12", "s-12", True),
    ("S-12", "S-%", True),
    ("XS-12", "S-%", False),
    (2, 2, True),
    (12, 2, False),
    (None, "x", False),
    (["a", "b"], "b", True),
    ("a.c", "a.c", True),
    ("abc", "a.c", False),
])
def test_same_value(stored, wanted, expected):
    assert client._same_value(stored, wanted) is expected


@pytest.mark.parametrize("value, expected", [
    ("plain", '"plain"'),
    ('with "quote"', r'"with \"quote\""'),
    ("back\\slash", r'"back\\slash"'),
    (2, '"2"'),
])
def test_quoted(value, expected):
    assert client._quoted(value) == expected


def test_day_accepts_text_and_dates():
    assert client._day("2026-10-01", "since") == "2026-10-01"
    assert client._day(datetime.date(2026, 10, 1), "since") == "2026-10-01"


@pytest.mark.parametrize("bad", ["last week", "2026-13-01", "10/01/2026"])
def test_day_refuses_anything_else(bad):
    with pytest.raises(ELNError, match="since must be a date"):
        client._day(bad, "since")


def test_as_list_keeps_a_lone_string_whole():
    assert client._as_list("P3HT") == ["P3HT"]
    assert client._as_list(["a", "b"]) == ["a", "b"]


def test_as_html_escapes_and_keeps_line_breaks():
    assert client._as_html("a < b\nc & d") == "<p>a &lt; b<br>c &amp; d</p>"


@pytest.mark.parametrize("settings, expected", [
    ({"type": "number", "value": "2"}, 2),
    ({"type": "number", "value": "2.5"}, 2.5),
    ({"type": "number", "value": ""}, ""),
    ({"type": "number", "value": "fast"}, "fast"),
    ({"type": "text", "value": "2"}, "2"),
])
def test_field_value(settings, expected):
    got = client._field_value(settings)
    assert got == expected and type(got) is type(expected)


@pytest.mark.parametrize("body, expected", [
    ("<p>Goal: <b>measure</b> it</p><p>Second</p>", "Goal: measure it"),
    ("<p>a &lt; b</p>", "a < b"),
    ("line one<br>line two", "line one"),
    ("# Report\n\nok", "# Report"),
    ("", ""),
    (None, ""),
])
def test_first_line(body, expected):
    assert client._first_line(body) == expected


def test_first_line_is_cut_to_100_characters():
    assert len(client._first_line("x" * 300)) == 100


def test_fields_in_order_uses_position_and_puts_the_rest_last():
    metadata = '{"extra_fields": {"c": {}, "b": {"position": 2}, "a": {"position": 1}}}'
    assert list(client._fields_in_order(metadata)) == ["a", "b", "c"]


@pytest.mark.parametrize("metadata", [None, "", "{}", '{"extra_fields": []}', '{"extra_fields": null}'])
def test_fields_in_order_with_no_fields(metadata):
    assert client._fields_in_order(metadata) == {}


# --- The results table


def _summary(**changes):
    values = dict(id=7, title="A run", date="2026-10-28", owner="A Person", status="", template_id=None,
                  tags=[], fields={}, first_line="")
    return ExperimentSummary(**(values | changes))


def test_table_lines_up(capsys):
    show_experiments([_summary(fields={"Sample ID": "S-1"}), _summary(id=1234, title="Longer title")])
    header, first, second = capsys.readouterr().out.splitlines()
    assert header.index("Title") == first.index("A run") == second.index("Longer title")
    assert "Sample ID=S-1" in first


def test_table_shortens_long_titles(capsys):
    show_experiments([_summary(title="x" * 200)])
    assert "x" * 39 + "…" in capsys.readouterr().out


def test_table_with_no_results(capsys):
    show_experiments([])
    assert capsys.readouterr().out == "No experiments found.\n"
