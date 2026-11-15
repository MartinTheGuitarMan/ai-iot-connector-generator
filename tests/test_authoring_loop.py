import pytest

from authoring.author import ModelReply, author_spec, extract_yaml
from authoring.eval import PROTOCOLS


def _fenced(text):
    return f"Here you go:\n```yaml\n{text}```\n"


def scripted(replies):
    """Fake model: returns canned replies in order and records what it was sent."""
    calls = []

    def call(system, messages):
        calls.append([dict(m) for m in messages])
        return replies[len(calls) - 1]

    call.calls = calls
    return call


def ref(key):
    return PROTOCOLS[key].reference_spec.read_text()


def test_extract_yaml_takes_last_block_and_none_when_absent():
    assert extract_yaml("x ```yaml\na: 1\n``` y ```yaml\nb: 2\n```") == "b: 2\n"
    assert extract_yaml("no code here") is None


def test_first_try_success():
    m = scripted([ModelReply(_fenced(ref("modbus")), 10, 20, "end_turn")])
    run = author_spec("modbus", m)
    assert run.ok and run.attempts_used == 1
    assert run.attempts[0].input_tokens == 10


def test_doc_is_sent_and_examples_do_not_leak_references():
    from authoring.prompt import SYSTEM_PROMPT
    m = scripted([ModelReply(_fenced(ref("wxobs")))])
    author_spec("wxobs", m)
    assert "WXOBS" in m.calls[0][0]["content"]
    for leak in ("WXOBS", "temperature_c", "modbus_weather", "rain_mm", "wx_ascii"):
        assert leak not in SYSTEM_PROMPT


def test_retries_after_validation_error_and_feeds_it_back():
    bad = ref("modbus").replace("type: uint16", "type: float", 1)
    m = scripted([ModelReply(_fenced(bad)), ModelReply(_fenced(ref("modbus")))])
    run = author_spec("modbus", m)
    assert run.ok and run.attempts_used == 2
    assert run.attempts[0].result.stage == "spec"
    feedback = m.calls[1][-1]["content"]
    assert "'spec' stage" in feedback and "float" in feedback


def test_errors_mode_hides_expected_output_diff_mode_shows_it():
    wrong_scale = ref("modbus").replace("scale: 0.01", "scale: 0.1")
    for mode, expect_diff in (("errors", False), ("diff", True)):
        m = scripted([ModelReply(_fenced(wrong_scale)), ModelReply(_fenced(ref("modbus")))])
        run = author_spec("modbus", m, feedback=mode)
        fb = m.calls[1][-1]["content"]
        assert run.ok
        assert "Failing cases" in fb
        assert ("expected:" in fb) == expect_diff


def test_gives_up_after_max_attempts():
    bad = ref("modbus").replace("scale: 0.01", "scale: 0.1")
    m = scripted([ModelReply(_fenced(bad))] * 2)
    run = author_spec("modbus", m, max_attempts=2)
    assert not run.ok and run.attempts_used == 2


def test_missing_yaml_block_is_retried():
    m = scripted([ModelReply("I think the answer is 42"), ModelReply(_fenced(ref("modbus")))])
    run = author_spec("modbus", m)
    assert run.ok and run.attempts[0].note == "no yaml block"


@pytest.mark.parametrize("reason", ["refusal", "max_tokens"])
def test_unrecoverable_stop_reasons_end_the_run(reason):
    m = scripted([ModelReply("", stop_reason=reason)])
    run = author_spec("modbus", m)
    assert not run.ok and run.attempts_used == 1 and run.attempts[0].note == reason


def test_bad_feedback_mode_rejected():
    with pytest.raises(ValueError):
        author_spec("modbus", scripted([]), feedback="hints")
