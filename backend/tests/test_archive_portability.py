from api.routes_files import _portable_path_conflicts


def test_portable_path_conflicts_detect_directory_case_collision():
    assert _portable_path_conflicts([
        "Backend/src/a.js",
        "backend/src/b.js",
    ]) == [("Backend", "backend")]


def test_portable_path_conflicts_allow_distinct_portable_paths():
    assert _portable_path_conflicts([
        "backend/src/a.js",
        "backend/src/b.js",
        "frontend/src/App.tsx",
    ]) == []
