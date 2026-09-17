from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. PostgreSQL is the production target; SQLite is the
    zero-setup fallback for local development and the test suite."""

    database_url: str = "sqlite:///./aurora.db"
    cors_origins: str = "http://localhost:3000"

    # Headset MCP connector (optional). The remote MCP endpoint and a bearer token.
    # Leave blank to use recorded JSON envelopes (`headset-import-dir`) instead.
    headset_mcp_url: str = ""
    headset_mcp_token: str = ""
    # Where `headset-sync --record` writes the raw pulls. Git-ignored: it is your data.
    headset_data_dir: str = "./data/headset"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
