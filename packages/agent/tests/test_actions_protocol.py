from __future__ import annotations

from linling_agent.actions_protocol import parse_actions_envelope


def test_parse_actions_envelope_accepts_json_embedded_in_prose() -> None:
    content = """(用力点头,耳朵跟着晃了晃)记住了记住了!
{"actions":[
  {"type":"send","text":"苏苏这次一定好好用短句子"},
  {"type":"send","text":"大哥哥监督苏苏哦！"}
]}
(又补了一句,尾巴轻轻摇晃)新规则可把苏苏的脑袋绕晕啦……"""

    outcome = parse_actions_envelope(content)

    assert outcome.recognised is True
    assert [entry.text for entry in outcome.entries] == [
        "苏苏这次一定好好用短句子",
        "大哥哥监督苏苏哦！",
    ]


def test_parse_actions_envelope_ignores_non_actions_json_before_envelope() -> None:
    content = 'metadata {"debug": true} then {"actions":[{"text":"ok"}]}'

    outcome = parse_actions_envelope(content)

    assert outcome.recognised is True
    assert len(outcome.entries) == 1
    assert outcome.entries[0].text == "ok"

