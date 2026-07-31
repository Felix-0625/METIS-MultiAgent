from core.hermes_client import HermesClient, Message, MessageRole


def test_semantic_cache_key_includes_source_after_requirements_prefix():
    client = HermesClient(api_key="test")
    system = Message(role=MessageRole.SYSTEM, content="代码审查专家")
    first = [
        system,
        Message(role=MessageRole.USER, content="功能需求：same\n" + "x" * 200 + "old source"),
    ]
    repaired = [
        system,
        Message(role=MessageRole.USER, content="功能需求：same\n" + "x" * 200 + "new source"),
    ]

    assert client._compute_semantic_key(first) != client._compute_semantic_key(repaired)


def test_all_cache_layers_are_scoped_by_model_and_purpose_profile():
    client = HermesClient(api_key="test")
    messages = [
        Message(role=MessageRole.SYSTEM, content="review"),
        Message(role=MessageRole.USER, content="same evidence"),
    ]

    assert client._compute_exact_key(messages, "model-a", "reviewer") != client._compute_exact_key(messages, "model-b", "reviewer")
    assert client._compute_semantic_key(messages, "model-a", "reviewer") != client._compute_semantic_key(messages, "model-b", "reviewer")
    assert client._compute_prompt_key(messages, "model-a", "reviewer") != client._compute_prompt_key(messages, "model-b", "reviewer")
    assert client._compute_semantic_key(messages, "model-a", "generator") != client._compute_semantic_key(messages, "model-a", "reviewer")
    assert client._compute_prompt_key(messages, "model-a", "generator") != client._compute_prompt_key(messages, "model-a", "reviewer")
