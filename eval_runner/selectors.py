from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ModelDeployment


def resolve_deployment(db: Session, selector: str) -> ModelDeployment:
    selector = selector.strip()
    if not selector:
        raise ValueError("model selector cannot be empty")
    if selector.isdecimal():
        deployment = db.get(ModelDeployment, int(selector))
        if deployment is None:
            raise ValueError(f"no ModelDeployment with id {selector}")
        return deployment
    matches = list(
        db.scalars(select(ModelDeployment).where(ModelDeployment.model_version == selector))
    )
    if not matches:
        raise ValueError(f"no ModelDeployment with model_version {selector!r}")
    if len(matches) != 1:
        raise ValueError(f"model_version {selector!r} is not unique; select by numeric id")
    return matches[0]


def resolve_deployments(db: Session, selectors: Sequence[str]) -> list[ModelDeployment]:
    deployments = [resolve_deployment(db, selector) for selector in selectors]
    ids = [deployment.id for deployment in deployments]
    if len(ids) != len(set(ids)):
        raise ValueError("model selectors resolve to duplicate deployments")
    return deployments
