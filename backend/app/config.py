from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. PostgreSQL is the production target; SQLite is the
    zero-setup fallback for local development and the test suite."""

    database_url: str = "sqlite:///./aurora.db"
    cors_origins: str = "http://localhost:3000"
    # What the dashboard and the weekly report open on: "" = everything, "state:FL" = one state, or a store code.
    default_scope: str = ""

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

    # Spreadsheet sources (no OAuth: share links). A Google Sheet is read as CSV
    # from its share link (sheet must be "anyone with the link can view"); pick
    # the tab by name or by the gid in the tab's URL. A OneDrive / SharePoint
    # "anyone with the link" share URL is downloaded directly. A local path works too.
    market_sheet_id: str = ""              # Google Sheet id of the OMMU market dashboard
    market_sheet_tab: str = ""             # tab name or gid of the raw weekly rows
    market_self_operator: str = ""         # our operator name as the report spells it (optional; is_p13 column wins)
    deals_sheet_id: str = ""               # Google Sheet id of the competitor deals workbook
    deals_sheet_tab: str = ""              # deals library tab name or gid
    promotions_url: str = ""               # OneDrive share link, Google Sheet share link, https URL or local path to the promotions workbook
    promotions_sheet: str = ""             # worksheet name (blank = first)
    promotions_column_map: str = ""        # JSON {"name": "Promo", "start": "Start Date", ...} when headers are unusual
    market_centroid_lat: float = 28.1      # where statewide events are placed (Florida)
    market_centroid_lon: float = -81.6

    # The analyst (Claude over Aurora's read-only tools). ANTHROPIC_API_KEY in the
    # environment is enough; the setting exists for explicit injection.
    anthropic_api_key: str = ""
    analyst_model: str = "claude-opus-5"
    analyst_effort: str = "high"          # low | medium | high | xhigh | max
    analyst_max_iterations: int = 24      # model turns per question (each may call several tools)

    # Weekly owner report by email (off until SMTP_HOST and REPORT_TO are set).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    report_to: str = ""                    # comma-separated
    report_weekday: int = 1                # ISO: 1 = Monday
    report_store: str = ""                 # blank = all stores; a code for one store's report

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
