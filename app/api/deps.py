"""Shared request dependencies."""

from typing import Annotated

from fastapi import Depends, Request

from app.pipeline.candidate import CandidateStore


def candidate_store(request: Request) -> CandidateStore:
    store: CandidateStore = request.app.state.candidate_store
    return store


CandidateStoreDep = Annotated[CandidateStore, Depends(candidate_store)]
