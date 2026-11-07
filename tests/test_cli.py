import json
import subprocess
import sys


def tw(cwd, *args, inp=None):
    return subprocess.run([sys.executable, "-m", "tierwright", *args], cwd=cwd, input=inp,
                          capture_output=True, text=True, timeout=120)


def test_cli_end_to_end(tmp_path):
    assert tw(tmp_path, "init", "--backend", "api", "--no-probe", "-y", "--goal", "hi",
              "--done", "test -f out.txt").returncode == 0
    assert "planner    T3  api" in tw(tmp_path, "route").stdout
    assert tw(tmp_path, "adopt", "--no-model").returncode == 0
    (tmp_path / "j.job").write_text("#@ artifact out.txt\necho x > out.txt\n")
    assert tw(tmp_path, "contract", "lint", "j.job").returncode == 0
    assert tw(tmp_path, "submit", "j.job").returncode == 0
    r = tw(tmp_path, "queue", "run")
    assert "j: ok" in r.stdout, r.stdout + r.stderr
    assert json.loads(tw(tmp_path, "brief", "j").stdout)["contract_pass"] is True
    assert tw(tmp_path, "step", "check").returncode == 0
    assert tw(tmp_path, "done").returncode == 0
    st = tw(tmp_path, "status").stdout
    assert len(st.splitlines()) <= 20 and "1/" in st
    assert (tmp_path / "HANDOFF.md").exists()


def test_hook_process_exit_codes(tmp_path):
    ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "sleep 600"}, "cwd": str(tmp_path)})
    r = subprocess.run([sys.executable, "-m", "tierwright.hooks"], input=ev, capture_output=True, text=True)
    assert r.returncode == 2 and "polling is blocked" in r.stderr
    ok = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": str(tmp_path)})
    assert subprocess.run([sys.executable, "-m", "tierwright.hooks"], input=ok, text=True).returncode == 0


def test_cli_errors_are_one_line(tmp_path):
    r = tw(tmp_path, "status")
    assert r.returncode == 1 and r.stderr.startswith("tw: no tierwright.yaml")
