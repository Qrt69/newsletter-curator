"""
Tests for starting a pipeline run as its own process (src/web/runner.py).

The point of the module is that the run happens somewhere else, so what is
worth pinning down is the handover: the command that gets built, the process
group it lands in, and that a failed run's output survives for the UI to
report. Actually running the pipeline is not something a test can do -- it
needs M365, an LLM and Notion -- so Popen is always faked here.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.web import runner


class FakeProc:
    """Stands in for the child; records how Popen was called."""

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs


@pytest.fixture
def spawn(tmp_path, monkeypatch):
    """runner.start() writing to a scratch dir, with Popen faked out."""
    monkeypatch.setattr(runner, "OUTPUT_FILE", str(tmp_path / ".pipeline_output"))
    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    return runner.start


def test_model_is_passed_through(spawn):
    assert spawn("qwen2.5-14b-instruct").cmd[-2:] == ["--model", "qwen2.5-14b-instruct"]


@pytest.mark.parametrize("model", [None, "", "auto"])
def test_auto_means_no_model_flag(spawn, model):
    """"auto" is the UI's word for "let the pipeline detect it", not a model."""
    assert "--model" not in spawn(model).cmd


def test_runs_the_pipeline_script_with_this_interpreter(spawn):
    cmd = spawn(None).cmd
    assert cmd[0] == sys.executable
    assert Path(cmd[1]).name == "run_weekly.py"
    assert Path(cmd[1]).exists(), "the script path handed to Popen must be real"


def test_child_gets_its_own_process_group(spawn):
    """So a signal aimed at the web process cannot take a 90-minute run with it."""
    kwargs = spawn(None).kwargs
    if os.name == "nt":
        assert kwargs["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs["start_new_session"] is True


def test_child_does_not_inherit_the_web_process_stdin(spawn):
    assert spawn(None).kwargs["stdin"] == subprocess.DEVNULL


def test_output_is_truncated_per_run(spawn):
    """A previous run's traceback must not be reported as this run's."""
    Path(runner.OUTPUT_FILE).write_text("failure from the run before")
    spawn(None)
    assert runner.error_tail() == ""


def test_error_tail_returns_the_end_of_the_output(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "OUTPUT_FILE", str(tmp_path / ".pipeline_output"))
    Path(runner.OUTPUT_FILE).write_text("x" * 500 + "the actual traceback")
    tail = runner.error_tail(limit=30)
    assert tail.endswith("the actual traceback")
    assert len(tail) == 30


def test_error_tail_without_a_run_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "OUTPUT_FILE", str(tmp_path / "nothing-here"))
    assert runner.error_tail() == ""
