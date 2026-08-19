"""Engineering metrics evaluation — docs/14-engineering-metrics-plan.md.

Aggregates the four engineering-metric dimensions into one reproducible report:

- ``quality``       — correctness evals (run_all), fault injection, security
- ``e2e_runtime``   — real happy-path/timeout runs, per-phase latency, success rate
- ``observability`` — metric families, phase labels, trace/llm wiring call sites
- ``process``       — code scale, test count, coverage, mypy/ruff

Every collector is injectable so unit tests stay offline and deterministic;
production defaults call the real modules. Honest gaps (zero node_span/llm_call
call sites, missing pytest-cov) are reported as FAIL/NOT_CONFIGURED, never
papered over.

Status semantics:

- metric:  PASS / FAIL / MEASURED / SKIPPED / NOT_CONFIGURED
- group:   FAIL if any metric FAIL, else SKIPPED if the group's core run
           crashed, else PASS (a sub-metric SKIPPED/NOT_CONFIGURED does not
           downgrade the group — it is tracked per-metric and in ``gaps``)
- overall: FAIL if any group FAIL, else SKIPPED if any group SKIPPED, else
           PARTIAL if any metric is SKIPPED/NOT_CONFIGURED, else PASS

Usage::

    uv run python evals/run_engineering_metrics.py \
        --report evals/reports/engineering_metrics_<timestamp>.json
"""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import TypedDict, cast

from erp_copilot.observability.metrics import Metrics, create_metrics, generate_latest
from evals.harness import DATASET_SPECS, write_report

# --- report data types -------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"
MEASURED = "MEASURED"
SKIPPED = "SKIPPED"
NOT_CONFIGURED = "NOT_CONFIGURED"


class Metric(TypedDict):
    name: str
    value: object
    unit: str
    acceptance: str
    status: str
    evidence: str


class GroupResult(TypedDict):
    group: str
    status: str
    metrics: list[Metric]


# --- repo layout --------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src" / "erp_copilot"
APPS_DIR = REPO_ROOT / "apps"
TESTS_DIR = REPO_ROOT / "tests"
REPORT_DIR = Path(__file__).resolve().parent / "reports"

# The two files that *define* the instruments are excluded from the call-site
# scan so their own ``def node_span(...)`` / ``def llm_call(...)`` lines do not
# count as production wiring.
_NODE_SPAN_DEF_FILE = SRC_DIR / "observability" / "tracing.py"
_LLM_CALL_DEF_FILE = SRC_DIR / "observability" / "langfuse.py"

# The Prometheus families the platform exposes (observability/metrics.py).
# answer_grounded (task 7.9) and reconciliation_success (task 7.11) ride out
# with the terminal state in persist_run, so they belong to the same check.
METRIC_FAMILIES = (
    "erp_runs_created_total",
    "erp_runs_completed_total",
    "erp_runs_failed_total",
    "erp_run_retries_total",
    "erp_run_replans_total",
    "erp_run_abandoned_total",
    "erp_approval_requests_total",
    "erp_plan_outcome_total",
    "erp_answer_grounded_total",
    "erp_reconciliation_success_total",
    "erp_phase_latency_seconds",
    "erp_worker_queue_length",
)

COVERAGE_THRESHOLD = 80.0


# --- small helpers ------------------------------------------------------------


def _as_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _metric(
    name: str,
    value: object,
    unit: str,
    acceptance: str,
    status: str,
    evidence: str,
) -> Metric:
    return {
        "name": name,
        "value": value,
        "unit": unit,
        "acceptance": acceptance,
        "status": status,
        "evidence": evidence,
    }


def _group(name: str, status: str, metrics: list[Metric]) -> GroupResult:
    return {"group": name, "status": status, "metrics": metrics}


def _group_status(metrics: list[Metric]) -> str:
    """FAIL if any metric failed, else PASS — sub-metric skips do not downgrade."""
    return FAIL if any(m["status"] == FAIL for m in metrics) else PASS


def _bool_status(value: object) -> str:
    return PASS if value is True else FAIL


def _metric_family_names(text: str) -> set[str]:
    names: set[str] = set()
    for line in text.splitlines():
        if line.startswith("# HELP "):
            names.add(line.split()[2])
    return names


def _counter_value(text: str, family: str) -> float:
    """Sum one counter family's samples (all label variants) from exposition text."""
    total = 0.0
    for line in text.splitlines():
        if line.startswith(family) and line[len(family) : len(family) + 1] in (" ", "{"):
            try:
                total += float(line.split()[-1])
            except (ValueError, IndexError):
                continue
    return total


def _recovery_rate_metrics(metrics: Metrics) -> list[Metric]:
    """Retry/recovery rates derived from the real counters (task 7.3).

    The D2 structural gap is closed once the counter families are registered:
    the rates are then measurable (0.0 when no retries/replans occurred) and
    reported MEASURED; a missing family falls back to the honest NOT_CONFIGURED.
    """
    text = generate_latest(metrics)
    families = _metric_family_names(text)
    runs = _counter_value(text, "erp_runs_created_total")
    retries = _counter_value(text, "erp_run_retries_total")
    replans = _counter_value(text, "erp_run_replans_total")
    return [
        _metric(
            "retry_rate",
            round(retries / runs, 4) if runs else 0.0,
            "rate",
            "记录",
            MEASURED if "erp_run_retries_total" in families else NOT_CONFIGURED,
            (
                "erp_run_retries_total 已接线"
                if "erp_run_retries_total" in families
                else "retry_count 仅在 AgentState，无 Prometheus 指标"
            ),
        ),
        _metric(
            "recovery_rate",
            round(replans / runs, 4) if runs else 0.0,
            "rate",
            "记录",
            MEASURED if "erp_run_replans_total" in families else NOT_CONFIGURED,
            (
                "erp_run_replans_total 已接线"
                if "erp_run_replans_total" in families
                else "replan_count 仅在 AgentState，无 Prometheus 指标"
            ),
        ),
    ]


def _scan_call_sites(dirs: Iterable[Path], patterns: tuple[str, ...], exclude: set[Path]) -> int:
    """Count lines containing any of *patterns* under *dirs*, minus definitions.

    The patterns are usage shapes (``@node_span``, ``node_span(``, ``llm_call(``),
    not bare symbol names, so a re-export line (``from ... import node_span``)
    or a docstring that merely mentions the name does not count as a wiring site.
    """
    excluded = {p.resolve() for p in exclude}
    count = 0
    for d in dirs:
        for p in sorted(d.rglob("*.py")):
            if p.resolve() in excluded:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue
            count += sum(1 for line in text.splitlines() if any(pt in line for pt in patterns))
    return count


def _count_py_files(dirs: dict[str, Path]) -> dict[str, dict[str, int]]:
    """Per-label counts of .py files and non-blank lines under each directory."""
    result: dict[str, dict[str, int]] = {}
    for label, d in dirs.items():
        files = 0
        loc = 0
        if d.is_dir():
            for p in sorted(d.rglob("*.py")):
                files += 1
                try:
                    loc += sum(
                        1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip()
                    )
                except OSError:
                    continue
        result[label] = {"files": files, "loc": loc}
    return result


def _count_test_functions(tests_dir: Path) -> int:
    count = 0
    if not tests_dir.is_dir():
        return 0
    for p in sorted(tests_dir.rglob("*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        count += sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        )
    return count


def _counter_value(text: str, family: str) -> float:
    """Sum one counter family's samples (all label variants) from exposition text."""
    total = 0.0
    for line in text.splitlines():
        if line.startswith(family) and line[len(family) : len(family) + 1] in (" ", "{"):
            total += float(line.split()[-1])
    return total


def _read_phase_latency_ms(text: str) -> dict[str, float]:
    """Average per-phase latency in ms from the histogram's sum/count samples."""
    result: dict[str, float] = {}
    for phase in ("plan", "execute", "verify"):
        sum_ = 0.0
        count_ = 0.0
        for line in text.splitlines():
            if f'phase="{phase}"' not in line:
                continue
            if line.startswith("erp_phase_latency_seconds_sum"):
                sum_ = float(line.split()[-1])
            elif line.startswith("erp_phase_latency_seconds_count"):
                count_ = float(line.split()[-1])
        result[phase] = (sum_ / count_ * 1000.0) if count_ else 0.0
    return result


def _run_cmd(cmd: list[str]) -> int:
    """Run a command and return its exit code (absent binary → non-zero)."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True).returncode
    except OSError:
        return 1


# --- real defaults ------------------------------------------------------------


def _run_real_harness() -> dict[str, object]:
    from evals.harness import run_all
    from evals.run_all import CATEGORY_RUNNERS

    return run_all(CATEGORY_RUNNERS)


def _run_real_faults() -> dict[str, object]:
    from tests.performance.fault_injection import run_all as fault_run_all

    return cast(dict[str, object], fault_run_all())


def _make_e2e_tenant() -> str:
    from erp_copilot.domain.entities import Tenant
    from erp_copilot.infrastructure.database import get_session

    session = get_session()
    try:
        tenant = Tenant(name="Engineering Metrics", slug="engineering_metrics")
        session.add(tenant)
        session.commit()
        return str(tenant.id)
    finally:
        session.close()


def _make_e2e_reader_user(tenant_id: str) -> str:
    from erp_copilot.domain.entities import Role, RoleScope, User, UserRole
    from erp_copilot.infrastructure.database import get_session

    session = get_session()
    try:
        role = Role(tenant_id=tenant_id, name="metrics-reader")
        session.add(role)
        session.flush()
        for resource, action in [("product", "read"), ("supplier", "read")]:
            session.add(RoleScope(role_id=role.id, resource=resource, action=action))
        user = User(
            tenant_id=tenant_id,
            email="metrics-reader@example.com",
            hashed_password="x",
            is_active=True,
        )
        session.add(user)
        session.flush()
        session.add(UserRole(user_id=user.id, role_id=role.id))
        session.commit()
        return user.id
    finally:
        session.close()


def _run_real_e2e() -> dict[str, object]:
    """Drive one happy-path and one timeout run through the real stack.

    Runs in-process (TestClient + Celery eager) exactly like tests/e2e, against
    the dedicated test database, and reads the module-singleton Prometheus
    registry afterwards — the same collector production code writes to.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from apps.api.main import create_app
    from apps.worker.celery_app import celery_app
    from erp_copilot.infrastructure.config import Settings
    from erp_copilot.infrastructure.database import Base, get_engine, get_session, init_db
    from erp_copilot.observability.metrics import METRICS

    # Hermetic pins: header auth, empty pepper, in-process simulator — never the
    # cloud ERP or a production-scoped secret (mirrors tests/conftest.py).
    os.environ["AUTH_MODE"] = "header"
    os.environ.setdefault("API_KEY_PEPPER", "")
    os.environ.setdefault("ERP_API_BASE_URL", "")
    os.environ.setdefault("ERP_API_KEY", "")

    from tests.conftest import TEST_DATABASE_URL, _ensure_test_database

    url = _ensure_test_database(TEST_DATABASE_URL)

    import erp_copilot.domain.entities  # noqa: F401  (register table metadata)

    settings = Settings(database_url=url, llm_api_key="sk-test")
    init_db(settings)
    Base.metadata.create_all(get_engine())
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True

    session = get_session()
    try:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(text(f"DELETE FROM {table.name} CASCADE"))
        session.commit()
    finally:
        session.close()

    tenant_id = _make_e2e_tenant()
    user_id = _make_e2e_reader_user(tenant_id)
    headers = {"X-Tenant-ID": tenant_id, "X-User-ID": user_id}
    client = TestClient(create_app())

    happy = client.post(
        "/v1/runs",
        json={"tenant_id": tenant_id, "title": "e2e-metrics-happy", "product_name": "苹果"},
        headers=headers,
    )
    happy_ok = happy.status_code == 202 and happy.json().get("status") == "COMPLETED"

    import apps.erp_simulator.scenarios as _scenarios

    _scenarios._current_scenario = "timeout"
    timeout = client.post(
        "/v1/runs",
        json={"tenant_id": tenant_id, "title": "e2e-metrics-timeout"},
        headers=headers,
    )
    timeout_ok = timeout.status_code == 202 and timeout.json().get("status") == "FAILED"
    _scenarios._current_scenario = "happy_path"

    latest = generate_latest(METRICS)
    return {
        "happy_path_success": happy_ok,
        "timeout_detected": timeout_ok,
        "phase_latency_ms": _read_phase_latency_ms(latest),
        "runs_completed": int(_counter_value(latest, "erp_runs_completed_total")),
        "runs_failed": int(_counter_value(latest, "erp_runs_failed_total")),
    }


# --- collectors ---------------------------------------------------------------


def collect_quality(
    run_harness: Callable[[], dict[str, object]] | None = None,
    run_faults: Callable[[], dict[str, object]] | None = None,
) -> GroupResult:
    """System-quality: correctness evals + fault injection + security.

    Delegates to the real ``run_all(CATEGORY_RUNNERS)`` so the five categories
    run the exact production logic; a DB-dependent category that fails (e.g.
    knowledge_rag with Postgres down) is recorded as SKIPPED via the harness's
    ``errors`` map rather than aborting the group.
    """
    if run_harness is None:
        run_harness = _run_real_harness
    if run_faults is None:
        run_faults = _run_real_faults

    report = run_harness()
    summary = _as_dict(report.get("summary"))
    categories = _as_dict(report.get("categories"))
    errors = _as_dict(report.get("errors"))

    metrics: list[Metric] = [
        _metric(
            "eval_overall_score",
            float(summary.get("overall_score", 0.0)),
            "score",
            "≥ 基线",
            MEASURED,
            "run_all 按 case 数加权",
        )
    ]
    for category, _ in DATASET_SPECS:
        if category in errors:
            metrics.append(
                _metric(
                    f"category:{category}",
                    0.0,
                    "score",
                    "≥ 基线",
                    SKIPPED,
                    f"{errors[category]}",
                )
            )
            continue
        cat = categories.get(category)
        score = float(cat.get("primary_score", 0.0)) if isinstance(cat, dict) else 0.0
        metrics.append(
            _metric(
                f"category:{category}",
                round(score, 4),
                "score",
                "≥ 基线",
                MEASURED,
                "真实生产逻辑确定性评测",
            )
        )

    sec = categories.get("security")
    if isinstance(sec, dict):
        sec_metrics = _as_dict(sec.get("metrics"))
        metrics.append(
            _metric(
                "security_interception_rate",
                float(sec_metrics.get("interception_rate", 0.0)),
                "rate",
                "100%",
                MEASURED,
                "攻击用例被拦截比例",
            )
        )
        metrics.append(
            _metric(
                "security_false_positive_rate",
                float(sec_metrics.get("false_positive_rate", 0.0)),
                "rate",
                "0%",
                MEASURED,
                "正常用例被误拦比例",
            )
        )

    faults = run_faults()
    total = int(_as_dict(faults).get("total", 0))
    passed = int(_as_dict(faults).get("passed", 0))
    rate = passed / total if total else 0.0
    metrics.append(
        _metric(
            "fault_injection_pass_rate",
            round(rate, 4),
            "rate",
            "3/3",
            PASS if rate >= 1.0 else FAIL,
            f"{passed}/{total} 场景",
        )
    )
    return _group("quality", _group_status(metrics), metrics)


def collect_e2e_runtime(
    driver: Callable[[], dict[str, object]] | None = None,
    metrics: Metrics | None = None,
) -> GroupResult:
    """End-to-end Agent runtime: real runs, phase latency, success rate.

    The retry/recovery rates (task 7.3) are derived from the Prometheus counters
    the recovery node advances; they are MEASURED once the counter families are
    registered (0.0 when no retries/replans occurred), NOT_CONFIGURED otherwise.
    *metrics* is the registry the rates are read from — tests inject a fresh one,
    production defaults to the module singleton the e2e driver writes to.
    """
    if driver is None:
        driver = _run_real_e2e
    if metrics is None:
        from erp_copilot.observability.metrics import METRICS

        metrics = METRICS

    try:
        m = driver()
    except Exception as exc:
        skipped = [
            _metric(
                "happy_path_success",
                False,
                "",
                "100%",
                SKIPPED,
                f"e2e 驱动失败: {type(exc).__name__}: {exc}",
            ),
            _metric("timeout_detected", False, "", "正确 FAILED", SKIPPED, ""),
            _metric("phase_latency_plan_ms", 0.0, "ms", "记录", SKIPPED, ""),
            _metric("phase_latency_execute_ms", 0.0, "ms", "记录", SKIPPED, ""),
            _metric("phase_latency_verify_ms", 0.0, "ms", "记录", SKIPPED, ""),
            _metric("execution_success_rate", 0.0, "rate", "≥ 0.99", SKIPPED, ""),
        ]
        return _group("e2e_runtime", SKIPPED, skipped + _recovery_rate_metrics(metrics))

    phase = _as_dict(m.get("phase_latency_ms"))
    happy_ok = m.get("happy_path_success") is True
    timeout_ok = m.get("timeout_detected") is True
    # "执行成功率" counts scenarios reaching their *expected* terminal state
    # (happy -> COMPLETED, timeout -> FAILED). The timeout run is intentionally
    # expected to fail, so completed/(completed+failed) would read 0.5 and flag
    # a healthy driver as failed.
    success_rate = (int(happy_ok) + int(timeout_ok)) / 2

    measured: list[Metric] = [
        _metric(
            "happy_path_success",
            happy_ok,
            "",
            "100%",
            _bool_status(happy_ok),
            "真实 Run 达 COMPLETED",
        ),
        _metric(
            "timeout_detected",
            timeout_ok,
            "",
            "正确 FAILED",
            _bool_status(timeout_ok),
            "timeout 场景正确 FAILED",
        ),
        _metric(
            "phase_latency_plan_ms",
            float(phase.get("plan", 0.0)),
            "ms",
            "记录",
            MEASURED,
            "erp_phase_latency_seconds 直方图均值",
        ),
        _metric(
            "phase_latency_execute_ms",
            float(phase.get("execute", 0.0)),
            "ms",
            "记录",
            MEASURED,
            "erp_phase_latency_seconds 直方图均值",
        ),
        _metric(
            "phase_latency_verify_ms",
            float(phase.get("verify", 0.0)),
            "ms",
            "记录",
            MEASURED,
            "erp_phase_latency_seconds 直方图均值",
        ),
        _metric(
            "execution_success_rate",
            round(success_rate, 4),
            "rate",
            "1.0",
            PASS if success_rate >= 1.0 else FAIL,
            "场景到达预期终态比例 (happy=COMPLETED, timeout=FAILED)",
        ),
    ]
    return _group(
        "e2e_runtime",
        _group_status(measured),
        measured + _recovery_rate_metrics(metrics),
    )


def collect_observability(
    metrics: Metrics | None = None,
    code_dirs: Iterable[Path] | None = None,
    metrics_route_file: Path | None = None,
) -> GroupResult:
    """Observability verification: metric families, phase labels, wiring sites.

    The Trace/LLM wiring checks are static scans of production code (src/ +
    apps/); zero call sites is a genuine gap and is reported as FAIL.
    """
    if metrics is None:
        metrics = create_metrics()
    if code_dirs is None:
        code_dirs = (SRC_DIR, APPS_DIR)
    if metrics_route_file is None:
        metrics_route_file = APPS_DIR / "api" / "routes" / "metrics.py"

    families = _metric_family_names(generate_latest(metrics))
    missing = [f for f in METRIC_FAMILIES if f not in families]
    phase_labels_ok = tuple(metrics.phase_latency._labelnames) == ("phase",)
    # Broad substrings: node_span is used as a decorator (@node_span) or a call
    # (node_span(...)); both are production wiring. The defining files are
    # excluded so their own ``def`` lines do not count.
    node_sites = _scan_call_sites(code_dirs, ("@node_span", "node_span("), {_NODE_SPAN_DEF_FILE})
    llm_sites = _scan_call_sites(code_dirs, ("llm_call(",), {_LLM_CALL_DEF_FILE})
    route_ok = metrics_route_file.is_file()

    metrics_list: list[Metric] = [
        _metric(
            "metric_families_present",
            f"{len(METRIC_FAMILIES) - len(missing)}/{len(METRIC_FAMILIES)}",
            "families",
            f"{len(METRIC_FAMILIES)} 族",
            PASS if not missing else FAIL,
            f"缺失: {missing}" if missing else f"{len(METRIC_FAMILIES)} 族齐全",
        ),
        _metric(
            "phase_latency_phase_label",
            phase_labels_ok,
            "",
            "plan/execute/verify",
            PASS if phase_labels_ok else FAIL,
            "erp_phase_latency_seconds 标签",
        ),
        _metric(
            "metrics_route_registered",
            route_ok,
            "",
            "已注册",
            PASS if route_ok else FAIL,
            str(metrics_route_file),
        ),
        _metric(
            "node_span_call_sites",
            node_sites,
            "处",
            "≥ 1",
            PASS if node_sites else FAIL,
            "生产代码 Trace 接线" + ("（缺口：0 调用点）" if not node_sites else ""),
        ),
        _metric(
            "llm_call_call_sites",
            llm_sites,
            "处",
            "≥ 1",
            PASS if llm_sites else FAIL,
            "生产代码 LLM 采集接线" + ("（缺口：0 调用点）" if not llm_sites else ""),
        ),
    ]
    return _group("observability", _group_status(metrics_list), metrics_list)


def collect_process(
    code_dirs: dict[str, Path] | None = None,
    tests_dir: Path | None = None,
    run_cmd: Callable[[list[str]], int] | None = None,
    coverage_available: bool | None = None,
    coverage_runner: Callable[[], dict[str, object]] | None = None,
) -> GroupResult:
    """Engineering-process: code scale, test count, coverage, static checks."""
    if code_dirs is None:
        code_dirs = {"src": SRC_DIR, "apps": APPS_DIR, "tests": TESTS_DIR}
    if tests_dir is None:
        tests_dir = TESTS_DIR
    if run_cmd is None:
        run_cmd = _run_cmd
    if coverage_available is None:
        import importlib.util

        coverage_available = importlib.util.find_spec("pytest_cov") is not None
    if coverage_runner is None:
        coverage_runner = _run_real_coverage

    counts = _count_py_files(code_dirs)
    metrics: list[Metric] = []
    for label, label_dirs in code_dirs.items():
        per = counts.get(label, {"files": 0, "loc": 0})
        metrics.append(
            _metric(f"code_files_{label}", per["files"], "文件", "记录", MEASURED, str(label_dirs))
        )
        metrics.append(_metric(f"code_loc_{label}", per["loc"], "行", "记录", MEASURED, "非空行"))
    metrics.append(
        _metric(
            "test_functions_count",
            _count_test_functions(tests_dir),
            "个",
            "记录",
            MEASURED,
            "def test_*",
        )
    )

    if coverage_available:
        try:
            cov = coverage_runner()
        except Exception as exc:
            cov = {"percent_covered": 0.0, "error": f"{type(exc).__name__}: {exc}"}
        percent = float(cov.get("percent_covered", 0.0))
        metrics.append(
            _metric(
                "line_coverage_percent",
                round(percent, 2),
                "%",
                f"≥ {COVERAGE_THRESHOLD:.0f}%",
                PASS if percent >= COVERAGE_THRESHOLD else FAIL,
                str(cov.get("error", "pytest-cov 实测")),
            )
        )
    else:
        metrics.append(
            _metric(
                "line_coverage_percent",
                0.0,
                "%",
                f"≥ {COVERAGE_THRESHOLD:.0f}%",
                NOT_CONFIGURED,
                "pytest-cov 未安装",
            )
        )

    mypy_clean = run_cmd(["uv", "run", "mypy", "src/", "apps/"]) == 0
    ruff_clean = run_cmd(["uv", "run", "ruff", "check", "."]) == 0
    metrics.append(
        _metric(
            "mypy_clean",
            mypy_clean,
            "",
            "0 error",
            PASS if mypy_clean else FAIL,
            "uv run mypy src/ apps/",
        )
    )
    metrics.append(
        _metric(
            "ruff_clean",
            ruff_clean,
            "",
            "0 error",
            PASS if ruff_clean else FAIL,
            "uv run ruff check .",
        )
    )
    return _group("process", _group_status(metrics), metrics)


def _run_real_coverage() -> dict[str, object]:
    """Measure line coverage via pytest-cov and return the JSON totals."""
    json_path = REPORT_DIR / f"coverage_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    # Pin the coverage data file to the reports dir so the run never drops a
    # stray .coverage binary in the repo root.
    os.environ["COVERAGE_FILE"] = str(REPORT_DIR / ".coverage")
    run = _run_cmd(["uv", "run", "coverage", "run", "-m", "pytest", "tests/", "-q"])
    if run != 0:
        return {"percent_covered": 0.0, "error": f"coverage run exit {run}"}
    run = _run_cmd(["uv", "run", "coverage", "json", "-o", str(json_path)])
    if run != 0:
        return {"percent_covered": 0.0, "error": f"coverage json exit {run}"}
    import json as _json

    data = _json.loads(json_path.read_text(encoding="utf-8"))
    totals = _as_dict(data.get("totals"))
    return {
        "percent_covered": float(totals.get("percent_covered", 0.0)),
        "error": "",
    }


# --- orchestration ------------------------------------------------------------


def build_report(
    collectors: dict[str, Callable[[], GroupResult]] | None = None,
) -> dict[str, object]:
    """Run every collector and aggregate into the unified report.

    A collector that raises (e.g. Postgres down during the e2e driver) is
    recorded as a SKIPPED group so one broken group never aborts the rest.
    """
    if collectors is None:
        collectors = {
            "quality": collect_quality,
            "e2e_runtime": collect_e2e_runtime,
            "observability": collect_observability,
            "process": collect_process,
        }

    groups: dict[str, GroupResult] = {}
    for name, fn in collectors.items():
        try:
            groups[name] = fn()
        except Exception as exc:
            groups[name] = _group(
                name,
                SKIPPED,
                [_metric("run_error", "", "", "", SKIPPED, f"{type(exc).__name__}: {exc}")],
            )

    gaps: list[dict[str, str]] = []
    for group in groups.values():
        for m in group["metrics"]:
            if m["status"] in (FAIL, NOT_CONFIGURED, SKIPPED):
                gaps.append({"name": m["name"], "status": m["status"], "detail": m["evidence"]})

    overall = _overall_status(groups)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "groups": groups,
        "gaps": gaps,
        "overall_status": overall,
    }


def _overall_status(groups: dict[str, GroupResult]) -> str:
    if any(g["status"] == FAIL for g in groups.values()):
        return FAIL
    if any(g["status"] == SKIPPED for g in groups.values()):
        return SKIPPED
    if any(m["status"] in (SKIPPED, NOT_CONFIGURED) for g in groups.values() for m in g["metrics"]):
        return "PARTIAL"
    return PASS


def format_report(report: dict[str, object]) -> str:
    """Render the unified report as a human-readable text table."""
    groups = _as_dict(report.get("groups"))
    gaps = report.get("gaps", [])
    gaps = gaps if isinstance(gaps, list) else []
    lines = [
        "=" * 66,
        "Engineering Metrics Report",
        "=" * 66,
        f"Overall : {report.get('overall_status')}",
        "",
    ]
    for name, group in groups.items():
        g = cast(GroupResult, group)
        lines.append(f"[{name}] status={g['status']}")
        for m in g["metrics"]:
            lines.append(
                f"  {m['name']:<32} {str(m['value']):>12} {m['unit']:<10} "
                f"{m['status']:<14} {m['evidence']}"
            )
        lines.append("")
    if gaps:
        lines.append(f"Gaps ({len(gaps)}):")
        for gap in gaps:
            detail = gap["detail"] if isinstance(gap, dict) else ""
            name = gap["name"] if isinstance(gap, dict) else gap
            lines.append(f"  - [{name}] {detail}")
    lines.append("=" * 66)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the engineering metrics evaluation")
    parser.add_argument(
        "--report",
        type=str,
        default=str(
            REPORT_DIR / f"engineering_metrics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        ),
        help="JSON report output path",
    )
    args = parser.parse_args()

    report = build_report()
    print(format_report(report))

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_report(report, report_path)
    print(f"\nReport saved to {report_path}")


if __name__ == "__main__":
    sys.exit(main())
