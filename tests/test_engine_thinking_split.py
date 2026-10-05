

def test_blank_lines_before_the_answer_are_dropped():
    """vLLM's reasoning parser strips the think block but leaves the ``\\n\\n``
    after ``</think>`` — query().result came back as "\\n\\nANSWER=…" on a
    self-hosted Qwen3.8. Whole blank lines go; indentation stays."""
    from mantis_agent.agent import _split_inline_thinking
    from mantis_agent.types import AssistantMessage, TextBlock

    def first(t):
        return _split_inline_thinking(AssistantMessage(content=[TextBlock(text=t)])).content[0].text

    assert first("\n\nANSWER=832040") == "ANSWER=832040"
    assert first(" \n\n    code()") == "    code()"
    assert first("already clean\n\n") == "already clean\n\n"
