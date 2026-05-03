"""FakeClaudeSession behaviour."""

from __future__ import annotations

from daeyeon_bot.infra.claude import FakeClaudeSession


async def test_default_echo() -> None:
    session = FakeClaudeSession()
    async with session as s:
        out = await s.query("hello")
    assert out == "[fake] hello"
    assert session.calls == [{"prompt": "hello", "system": None}]
    assert session.closed is True


async def test_scripted_responses_then_default() -> None:
    session = FakeClaudeSession(responses=["a", "b"], default="z")
    async with session as s:
        assert await s.query("1") == "a"
        assert await s.query("2") == "b"
        assert await s.query("3", system="sys") == "z"
    assert [c["prompt"] for c in session.calls] == ["1", "2", "3"]
    assert session.calls[-1]["system"] == "sys"
