from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _requirement_lines(name: str) -> set[str]:
    return {
        line.strip()
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def test_full_install_includes_all_supported_profiles():
    assert _requirement_lines("requirements.txt") == {
        "-r requirements-base.txt",
        "-r requirements-kronos.txt",
        "-r requirements-mcp.txt",
        "-r requirements-training.txt",
    }


def test_ci_profile_excludes_optional_heavy_dependencies():
    ci_lines = _requirement_lines("requirements-ci.txt")
    base_lines = _requirement_lines("requirements-base.txt")
    combined = "\n".join(sorted(ci_lines | base_lines)).lower()

    assert "-r requirements-base.txt" in ci_lines
    assert "pytest>=8.0.0" in ci_lines
    for package in (
        "torch",
        "einops",
        "huggingface_hub",
        "safetensors",
        "mcp",
        "lightgbm",
    ):
        assert package not in combined


def test_model_workflow_tracks_model_paths_and_dependencies():
    workflow = (ROOT / ".github/workflows/model-tests.yml").read_text(
        encoding="utf-8"
    )

    assert "app/services/kronos_model/**" in workflow
    assert "app/services/kronos_predict_service.py" in workflow
    assert "strategy/first_limit_alpha/train_sequence.py" in workflow
    assert "requirements*.txt" in workflow
    assert "requirements-kronos.txt" in workflow
    assert "from app.services.kronos_model import" in workflow
    assert "-k sequence" in workflow


def test_core_service_lazy_loads_torch_sequence_backend():
    service = (ROOT / "app/services/first_limit_alpha_service.py").read_text(
        encoding="utf-8"
    )

    import_line = "from strategy.first_limit_alpha.train_sequence import FirstLimitSequenceTrainer"
    assert service.index(import_line) > service.index("def train_sequence(")
