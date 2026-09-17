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

    # Free external-event stack. Each provider is enabled by the presence of its key.
    ticketmaster_api_key: str = ""          # developer.ticketmaster.com (Discovery API, free)
    seatgeek_client_id: str = ""            # platform.seatgeek.com (free)
    seatgeek_client_secret: str = ""
    fl511_api_key: str = ""                 # fl511.com/developers (FDOT, free)
    fl511_api_url: str = "https://fl511.com/api/v2/get/event"
    events_radius_km: float = 15.0          # how far from a store a concert / game still counts
    traffic_radius_km: float = 5.0          # how far a road event still counts
    holiday_country: str = "US"
    holiday_subdivision: str = "FL"
    # Devices at each store POST /api/heartbeat with this token; blank disables the endpoint.
    heartbeat_token: str = ""
    heartbeat_gap_minutes: int = 5
    # Nominatim (OpenStreetMap) and the National Weather Service both require a
    # descriptive User-Agent with a contact. One setting serves both.
    geocoder_user_agent: str = "aurora-ai/0.2 (set GEOCODER_USER_AGENT to your contact email)"
    # Weather: Open-Meteo (no key) for hourly history + forecast, NWS (no key) for alerts.
    open_meteo_forecast_url: str = "https://api.open-meteo.com/v1/forecast"
    open_meteo_archive_url: str = "https://archive-api.open-meteo.com/v1/archive"
    nws_alerts_url: str = "https://api.weather.gov/alerts"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
