from backend.prompt import parse_prompt, protected_text, restore_protected, serialize_prompt, split_prompt


def test_split_keeps_nested_commas():
    assert split_prompt("masterpiece, (red hair, blue eyes:1.2), <lora:style:0.8>, BREAK") == ["masterpiece", "(red hair, blue eyes:1.2)", "<lora:style:0.8>", "BREAK"]


def test_round_trip_preserves_special_tokens():
    text = "masterpiece, (red hair, blue eyes:1.2), <lora:style:0.8>, BREAK"
    assert serialize_prompt(parse_prompt(text)) == text


def test_protection():
    masked, placeholders = protected_text("a girl, <lora:foo:0.7>, BREAK")
    assert "<lora" not in masked and "BREAK" not in masked
    assert restore_protected(masked, placeholders) == "a girl, <lora:foo:0.7>, BREAK"

