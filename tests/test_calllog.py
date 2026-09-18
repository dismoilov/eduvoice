import json

from eduvoice.calllog import CallLog
from eduvoice.registry import CallRecord, Turn


def test_call_log_writes_one_json_line(tmp_path):
    record = CallRecord(call_id="abc", caller="998901234567")
    record.turns.append(
        Turn(
            question="stipendiya qanday olinadi",
            answer="stipendiya javobi",
            intent="faq_keyword",
            latency_ms={"stt_ms": 320.0, "decision_ms": 12.0, "total_ms": 340.0},
        )
    )
    record.next_action = "hangup"

    CallLog(tmp_path).write(record, ended_reason="goodbye")

    lines = list((next(tmp_path.glob("*.jsonl"))).read_text(encoding="utf-8").splitlines())
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["caller"] == "998901234567"
    assert entry["ended_reason"] == "goodbye"
    assert entry["turns"][0]["question"] == "stipendiya qanday olinadi"
    assert entry["turns"][0]["latency_ms"]["total_ms"] == 340.0


def test_call_log_appends_calls(tmp_path):
    log = CallLog(tmp_path)
    log.write(CallRecord(call_id="one"), "goodbye")
    log.write(CallRecord(call_id="two"), "operator")

    lines = (next(tmp_path.glob("*.jsonl"))).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["call_id"] for line in lines] == ["one", "two"]


def test_call_log_survives_an_unwritable_directory(tmp_path):
    blocked = tmp_path / "file-not-a-dir"
    blocked.write_text("")

    CallLog(blocked / "logs").write(CallRecord(call_id="x"), "goodbye")  # must not raise


def test_call_log_records_what_the_panel_needs(tmp_path):
    """The supervisor panel reads this file: it must carry the recording and the markers."""
    record = CallRecord(call_id="abc", caller="998901234567")
    record.recording = "eduvoice/20260918/abc.wav"
    record.turns.append(
        Turn(
            question="stipendiya qanday olinadi",
            answer="javob matni",
            intent="faq_keyword",
            action="faq",
            faq_id="stipend",
            source="lex.uz 12-band",
            at_ms=4300.0,
        )
    )

    CallLog(tmp_path).write(record, ended_reason="goodbye")

    entry = json.loads(next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8"))
    assert entry["recording"] == "eduvoice/20260918/abc.wav"
    turn = entry["turns"][0]
    assert turn["at_ms"] == 4300.0, "without this the panel cannot jump to the question"
    assert turn["faq_id"] == "stipend"
    assert turn["source"] == "lex.uz 12-band"
    assert turn["action"] == "faq"
