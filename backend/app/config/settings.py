"""Central application settings loaded from environment variables."""

from functools import lru_cache
from typing import List, Optional

from pydantic import Field, model_validator
from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
)


def _split_csv(value: str) -> List[str]:
    return [
        item.strip()
        for item in value.split(",")
        if item.strip()
    ]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Telegram
    # ------------------------------------------------------------------

    telegram_bot_token: str

    telegram_admin_ids_raw: str = Field(
        default="",
        validation_alias="TELEGRAM_ADMIN_IDS",
    )

    # ------------------------------------------------------------------
    # Anoncoin
    # ------------------------------------------------------------------

    anoncoin_base_url: str = (
        "https://api.anoncoin.it"
    )

    anoncoin_api_key: Optional[str] = None

    # Number of newest legacy launches requested per discovery poll.
    anoncoin_discovery_limit: int = 50

    anoncoin_trade_endpoint: Optional[str] = None

    # ------------------------------------------------------------------
    # Four.meme / BSC
    # ------------------------------------------------------------------

    bitquery_api_token: Optional[str] = Field(
        default=None,
        validation_alias="BITQUERY_API_TOKEN",
    )

    # Backward-compatible alias for deployments that call the Bitquery
    # credential an API key. V2 streaming still uses Bearer authentication.
    bitquery_api_key: Optional[str] = Field(
        default=None,
        validation_alias="BITQUERY_API_KEY",
    )

    bsc_rpc_url: Optional[str] = Field(
        default=None,
        validation_alias="BSC_RPC_URL",
    )

    fourmeme_trading_enabled: bool = Field(
        default=False,
        validation_alias="FOURMEME_TRADING_ENABLED",
    )

    fourmeme_exchange_address: str = (
        "0x5c952063c7fc8610ffdb798152d69f0b9550762b"
    )

    fourmeme_helper3_address: str = (
        "0xF251F83e40a78868FcfA3FA4599Dad6494E46034"
    )

    fourmeme_token_manager2_address: str = (
        "0x5c952063c7fc8610FFDB798152D69F0B9550762b"
    )

    fourmeme_scan_interval_seconds: float = 0.5
    fourmeme_max_event_queue: int = 1000
    fourmeme_default_slippage_bps: int = 500

    # ------------------------------------------------------------------
    # Solscan
    # ------------------------------------------------------------------

    solscan_base_url: str = (
        "https://pro-api.solscan.io/v2.0"
    )

    solscan_api_key: Optional[str] = None

    # ------------------------------------------------------------------
    # Robinhood Chain / Pons v2
    # ------------------------------------------------------------------

    robinhood_chain_id: int = Field(default=4663, validation_alias="ROBINHOOD_CHAIN_ID")
    robinhood_alchemy_api_key: Optional[str] = Field(default=None, validation_alias="ROBINHOOD_ALCHEMY_API_KEY")
    robinhood_rpc_url: Optional[str] = Field(default=None, validation_alias="ROBINHOOD_RPC_URL")
    robinhood_rpc_override_url: Optional[str] = Field(default=None, validation_alias="ROBINHOOD_ALCHEMY_RPC_URL")
    robinhood_pons_trading_enabled: bool = Field(default=False, validation_alias="ROBINHOOD_PONS_TRADING_ENABLED")
    pons_factory_address: str = Field(default="0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e", validation_alias="PONS_FACTORY_ADDRESS")
    pons_factory_start_block: int = Field(default=0, validation_alias="PONS_FACTORY_START_BLOCK")
    pons_scan_interval_seconds: float = Field(default=0.5, validation_alias="PONS_SCAN_INTERVAL_SECONDS")
    pons_buy_slippage_bps: int = Field(default=1000, validation_alias="PONS_BUY_SLIPPAGE_BPS")
    pons_sell_slippage_bps: int = Field(default=1000, validation_alias="PONS_SELL_SLIPPAGE_BPS")

    # ------------------------------------------------------------------
    # Helius
    # ------------------------------------------------------------------

    helius_base_url: str = (
        "https://mainnet.helius-rpc.com"
    )

    helius_api_key: Optional[str] = None
    # Optional Alchemy Solana RPC fallback. Keep the API key in the
    # deployment environment; never hardcode it in source.
    alchemy_api_key: Optional[str] = Field(
        default=None,
        validation_alias="ALCHEMY_API_KEY",
    )


    # ------------------------------------------------------------------
    # Anoncoin creator watchlist
    #
    # IMPORTANT:
    #
    # This remains ONLY for the existing Anoncoin/Meteora launch
    # detection path.
    #
    # Pump.fun is intentionally NOT added here.
    # ------------------------------------------------------------------

    creator_watchlist_raw: str = Field(
        default=(
            "7AbRGzM3NBvvUXi7j1Mga2SraTfjpPBMzGpyHcXSzV3v"
        ),
        validation_alias="CREATOR_WATCHLIST",
    )

    # ------------------------------------------------------------------
    # Pump.fun
    #
    # Pump.fun is a separate launch source. No mint-authority wallet is
    # trusted. Discovery is verified from the official Pump.fun program
    # and create/create_v2 instructions in the transaction.
    # ------------------------------------------------------------------

    # Official Pump.fun bonding-curve program.
    #
    # This is kept separately because the launch detector will use the
    # Pump.fun program path rather than feeding Pump.fun launches into
    # the Anoncoin/Meteora watcher.
    pumpfun_program_id: str = Field(
        default=(
            "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
        ),
        validation_alias="PUMPFUN_PROGRAM_ID",
    )


    # ------------------------------------------------------------------
    # Solana Tracker / Smart Money (Phase 1)
    #
    # Phase 1 is observational only. It NEVER becomes a required
    # qualification condition and does not change the existing trade
    # decision path.
    # ------------------------------------------------------------------

    solana_tracker_api_key: Optional[str] = Field(
        default=None,
        validation_alias="SOLANA_TRACKER_API_KEY",
    )

    smart_money_wallets_raw: str = Field(
        default=(
            "HmUt3Jn46j7c7ANdURmEyjSRj8i3Em6MhjQUi37PZ219,DdM1tyCdoEyoxYYmGMjdf5rRPcpmj3UzZTpE7ScuTf7d"
        ),
        validation_alias="SMART_MONEY_WALLETS",
    )

    smart_money_enabled: bool = Field(
        default=True,
        validation_alias="SMART_MONEY_ENABLED",
    )

    smart_money_min_buy_usd: float = Field(
        default=50.0,
        validation_alias="SMART_MONEY_MIN_BUY_USD",
    )

    smart_money_max_trades_per_token: int = Field(
        default=100,
        validation_alias="SMART_MONEY_MAX_TRADES_PER_TOKEN",
    )

    smart_money_trade_lookback_seconds: int = Field(
        default=180,
        validation_alias="SMART_MONEY_TRADE_LOOKBACK_SECONDS",
    )

    smart_money_wallet_cache_seconds: int = Field(
        default=3600,
        validation_alias="SMART_MONEY_WALLET_CACHE_SECONDS",
    )

    # ------------------------------------------------------------------
    # GO Guardian AI supervisor
    # ------------------------------------------------------------------
    guardian_enabled: bool = Field(default=True, validation_alias="GUARDIAN_ENABLED")
    guardian_tick_seconds: float = Field(default=5.0, validation_alias="GUARDIAN_TICK_SECONDS")
    guardian_window_seconds: int = Field(default=600, validation_alias="GUARDIAN_WINDOW_SECONDS")
    guardian_min_buy_attempts: int = Field(default=5, validation_alias="GUARDIAN_MIN_BUY_ATTEMPTS")
    guardian_pause_failure_rate_pct: float = Field(default=70.0, validation_alias="GUARDIAN_PAUSE_FAILURE_RATE_PCT")
    guardian_min_candidates_for_filter_warning: int = Field(default=20, validation_alias="GUARDIAN_MIN_CANDIDATES_FOR_FILTER_WARNING")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    database_url: str = (
        "sqlite+aiosqlite:///./data/bot.db"
    )

    # ------------------------------------------------------------------
    # Security
    # ------------------------------------------------------------------

    secret_encryption_key: str

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------

    trading_mode: str = "paper"

    paper_starting_balance_sol: float = 10.0

    # Live entry strategy. A candidate must score at least this much
    # after hard filters before it can reach the BUY path.
    qualify_score_threshold: float = 52.0

    # ------------------------------------------------------------------
    # Pump.fun anti-late-entry / anti-chase protection
    # ------------------------------------------------------------------
    # These deployment-level values are applied by ScannerService to every
    # Pump.fun rule before screening/revalidation. They mirror the safe
    # defaults defined in app/scoring/rules.py.
    late_entry_enabled: bool = True
    late_entry_max_age_seconds: float = 3.0
    late_entry_soft_market_cap_usd: float = 8000.0
    late_entry_hard_market_cap_usd: float = 15000.0
    late_entry_near_high_pct: float = 4.0
    late_entry_required_pullback_pct: float = 10.0
    late_entry_max_short_runup_pct: float = 25.0
    late_entry_max_runup_from_first_pct: float = 60.0

    # Fast Pump.fun polling is intentionally separate from the slower
    # Anoncoin API scan interval.
    pumpfun_scan_interval_seconds: float = 1.0

    # Risk management for live positions. These are deliberately tighter
    # than the legacy rule defaults and are applied by PositionManager.
    defensive_stop_loss_pct: float = 15.0
    defensive_stop_sell_pct: float = 50.0
    hard_stop_loss_pct: float = 25.0
    breakeven_trigger_pct: float = 10.0
    breakeven_lock_pct: float = -2.0
    profit_lock_trigger_pct: float = 20.0
    profit_lock_pct: float = 5.0
    strong_profit_trigger_pct: float = 40.0
    strong_profit_lock_pct: float = 10.0
    strong_runner_lock_pct: float = 35.0
    adaptive_trailing_min_pct: float = 15.0
    adaptive_trailing_mid_pct: float = 22.0
    adaptive_trailing_strong_pct: float = 30.0
    adaptive_trailing_max_pct: float = 12.0
    smart_money_score_bonus: float = 10.0
    smart_money_probe_score: float = 55.0

    # ------------------------------------------------------------------
    # Simulated feed
    # ------------------------------------------------------------------

    enable_mock_feed: bool = False

    # ------------------------------------------------------------------
    # On-chain wallet execution
    # ------------------------------------------------------------------

    # If SOLANA_RPC_URL is not explicitly provided,
    # automatically use Helius.
    solana_rpc_url: Optional[str] = None

    jupiter_base_url: str = (
        "https://lite-api.jup.ag/swap/v1"
    )

    jupiter_price_url: str = (
        "https://lite-api.jup.ag/price/v3"
    )

    default_slippage_bps: int = 200

    # Execution-friction controls. Priority fee is capped per transaction.
    pumpfun_priority_level: str = Field(default="Medium", validation_alias="PUMPFUN_PRIORITY_LEVEL")
    pumpfun_priority_fee_cap_micro_lamports: int = Field(default=10_000, validation_alias="PUMPFUN_PRIORITY_FEE_CAP_MICROLAMPORTS")
    pumpfun_buy_slippage_bps: int = Field(default=200, validation_alias="PUMPFUN_BUY_SLIPPAGE_BPS")
    pumpfun_sell_slippage_bps: int = Field(default=200, validation_alias="PUMPFUN_SELL_SLIPPAGE_BPS")

    # ------------------------------------------------------------------
    # Timers
    # ------------------------------------------------------------------

    scan_interval_seconds: int = 10

    position_check_interval_seconds: int = 1

    execution_timeout_seconds: int = 180

    daily_summary_hour_utc: int = 23

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    log_level: str = "INFO"

    # ------------------------------------------------------------------
    # Solana RPC configuration
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def configure_bsc_rpc(self):
        if not self.bsc_rpc_url:
            self.bsc_rpc_url = "https://bsc-dataseed.binance.org"
        return self

    @model_validator(mode="after")
    def configure_solana_rpc(self):
        """
        Use Helius as the primary Solana RPC when HELIUS_API_KEY is
        configured.

        An explicitly supplied SOLANA_RPC_URL always takes precedence.
        """

        if not self.solana_rpc_url:

            if self.helius_api_key:

                self.solana_rpc_url = (
                    "https://mainnet.helius-rpc.com/"
                    f"?api-key={self.helius_api_key}"
                )

            else:

                self.solana_rpc_url = (
                    "https://api.mainnet-beta.solana.com"
                )

        return self

    # ------------------------------------------------------------------
    # Alchemy Solana RPC
    # ------------------------------------------------------------------

    @property
    def alchemy_solana_rpc_url(self) -> Optional[str]:
        if not self.alchemy_api_key:
            return None
        return (
            "https://solana-mainnet.g.alchemy.com/v2/"
            f"{self.alchemy_api_key}"
        )

    @property
    def robinhood_alchemy_rpc_url(self) -> Optional[str]:
        if self.robinhood_rpc_override_url:
            return self.robinhood_rpc_override_url
        key = self.robinhood_alchemy_api_key or self.alchemy_api_key
        if key:
            return f"https://robinhood-mainnet.g.alchemy.com/v2/{key}"
        return None

    @property
    def alchemy_solana_ws_url(self) -> Optional[str]:
        if not self.alchemy_api_key:
            return None
        return (
            "wss://solana-mainnet.g.alchemy.com/v2/"
            f"{self.alchemy_api_key}"
        )

    # ------------------------------------------------------------------
    # Telegram admin IDs
    # ------------------------------------------------------------------

    @property
    def telegram_admin_ids(self) -> List[int]:
        return [
            int(x)
            for x in _split_csv(
                self.telegram_admin_ids_raw
            )
            if x.lstrip("-").isdigit()
        ]

    # ------------------------------------------------------------------
    # Anoncoin creator watchlist
    # ------------------------------------------------------------------

    @property
    def creator_watchlist(self) -> List[str]:
        return _split_csv(
            self.creator_watchlist_raw
        )


    # ------------------------------------------------------------------
    # Smart-money wallet list
    # ------------------------------------------------------------------

    @property
    def smart_money_wallets(self) -> List[str]:
        return _split_csv(
            self.smart_money_wallets_raw
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
