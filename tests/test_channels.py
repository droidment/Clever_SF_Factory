import pytest


@pytest.mark.parametrize("channel", ["teams", "email", "webhook"])
def test_connector_normalization_and_redelivery(client, channel):
    payload = {
        "event_id": "message-0042",
        "subject": "Review an incoming change",
        "body": "Please inspect the change and report verifiable acceptance criteria.",
        "sender": "External requestor",
        "repository": "platform/order-service",
    }
    first = client.post(f"/api/intake/{channel}", json=payload)
    assert first.status_code == 202
    second = client.post(f"/api/intake/{channel}", json=payload)
    assert first.json()["request_id"] == second.json()["request_id"]
    detail = client.get(first.json()["tracking_url"]).json()
    assert detail["data"]["source"] == channel
    assert detail["submitted_by"] == "local-operator"
