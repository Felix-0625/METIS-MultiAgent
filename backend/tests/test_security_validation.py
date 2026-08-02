from core.security_validation import validate_generated_files


def test_css_class_fragment_is_not_reported_as_api_key():
    result = validate_generated_files([(
        "TaskItem.vue",
        ':class="{ \'task-item__complete--done\': task.status === \'completed\' }"',
    )])

    assert result["issues"] == []


def test_standalone_sk_secret_is_still_reported():
    result = validate_generated_files([(
        "settings.js",
        'const key = "sk-abcdefghijklmnopqrstuvwxyz"',
    )])

    assert result["issue_count"] == 1
    assert result["issues"][0]["severity"] == "error"
