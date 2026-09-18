"""Hidden tests for D3 — minimal notification dispatch."""
import pytest

from solution import notify, notify_all


def test_email():
    assert notify({"channel": "email", "address": "a@b.c"}, "hi") == "email:a@b.c:hi"


def test_sms():
    assert notify({"channel": "sms", "address": "+7999"}, "hi") == "sms:+7999:hi"


def test_unknown_channel():
    with pytest.raises(ValueError):
        notify({"channel": "pigeon", "address": "coop"}, "hi")


def test_notify_all_order():
    users = [{"channel": "email", "address": "a@b.c"},
             {"channel": "sms", "address": "+1"},
             {"channel": "email", "address": "x@y.z"}]
    assert notify_all(users, "m") == ["email:a@b.c:m", "sms:+1:m", "email:x@y.z:m"]


def test_notify_all_empty():
    assert notify_all([], "m") == []


def test_notify_all_propagates():
    users = [{"channel": "email", "address": "a@b.c"},
             {"channel": "bad", "address": "?"}]
    with pytest.raises(ValueError):
        notify_all(users, "m")


def test_empty_message():
    assert notify({"channel": "sms", "address": "+1"}, "") == "sms:+1:"
