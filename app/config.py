"""Runtime settings. Everything here is overridable without a rebuild."""

from enum import StrEnum
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    NP = "np"
    TEST = "test"
    PREP = "prep"
    PROD = "prod"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APCHI_", env_file=".env", extra="ignore")

    environment: Environment = Environment.NP

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_database: str = "apchi"

    # None means "derive from environment"; an explicit value always wins, so the
    # level can be raised during a production incident without a rebuild.
    log_level: str | None = None
    log_json: bool | None = None

    @property
    def effective_log_level(self) -> str:
        if self.log_level is not None:
            return self.log_level.upper()
        return "DEBUG" if self.environment in (Environment.NP, Environment.TEST) else "INFO"

    @property
    def effective_log_json(self) -> bool:
        # JSON in-cluster, plain console locally. Same switch as the level.
        if self.log_json is not None:
            return self.log_json
        return self.environment in (Environment.PREP, Environment.PROD)


@lru_cache
def get_settings() -> Settings:
    return Settings()
