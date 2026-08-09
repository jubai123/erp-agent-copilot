"""Celery tasks for asynchronous run execution."""

from __future__ import annotations

from datetime import UTC, datetime

from celery import Task
from celery.utils.log import get_task_logger

from apps.worker.celery_app import celery_app

logger = get_task_logger(__name__)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5)
def execute_run(self: Task, run_id: str, product_name: str = "苹果") -> dict:
    """Execute a run by querying the ERP simulator and recording the result.

    Lifecycle: QUEUED -> PROCESSING -> COMPLETED | FAILED
    """
    from apps.erp_simulator.data.products import PRODUCT_BY_NAME
    from apps.erp_simulator.scenarios import get_scenario
    from erp_copilot.domain.entities import Run, RunStep
    from erp_copilot.infrastructure.database import get_session

    session = get_session()
    try:
        run = session.query(Run).filter_by(id=run_id).first()
        if run is None:
            logger.error("Run %s not found", run_id)
            return {"status": "error", "detail": f"Run {run_id} not found"}

        run.status = "PROCESSING"
        run.started_at = datetime.now(UTC)
        session.commit()

        scenario = get_scenario()

        if scenario == "timeout":
            step = RunStep(
                run_id=run_id,
                step_index=1,
                step_type="TOOL_CALL",
                status="FAILED",
                error_message="Request timed out contacting ERP simulator",
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
            session.add(step)
            run.status = "FAILED"
            run.completed_at = datetime.now(UTC)
            session.commit()
            logger.info("Run %s failed due to timeout scenario", run_id)
            return {"status": "failed", "error": "timeout"}

        product = PRODUCT_BY_NAME.get(product_name)

        step = RunStep(
            run_id=run_id,
            step_index=1,
            step_type="TOOL_CALL",
            status="COMPLETED" if product else "FAILED",
            input=f'{{"product_name": "{product_name}"}}',
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )

        if product is None:
            step.error_message = f"Product '{product_name}' not found"
            session.add(step)
            run.status = "FAILED"
            run.completed_at = datetime.now(UTC)
            session.commit()
            return {"status": "failed", "error": step.error_message}

        stock = 0 if scenario == "stock_insufficient" else product.quantity_in_stock
        result = {
            "product_id": product.product_id,
            "name": product.name,
            "description": product.description,
            "price": product.price,
            "stock": stock,
            "unit": product.unit,
        }
        step.output = str(result)
        session.add(step)
        run.status = "COMPLETED"
        run.completed_at = datetime.now(UTC)
        session.commit()

        logger.info("Run %s completed successfully", run_id)
        return {"status": "completed", "result": result}

    except Exception:
        session.rollback()
        try:
            run = session.query(Run).filter_by(id=run_id).first()
            if run:
                run.status = "FAILED"
                run.completed_at = datetime.now(UTC)
                session.commit()
        except Exception:
            session.rollback()
        raise
    finally:
        session.close()
