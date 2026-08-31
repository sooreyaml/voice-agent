from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.domains.businesses.repository import BusinessRepository


def get_business_repository(request: Request) -> BusinessRepository:
    return request.app.state.business_repository


BusinessRepositoryDep = Annotated[BusinessRepository, Depends(get_business_repository)]
