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

    anoncoin_trade_endpoint: Optional[str] = None

    # ------------------------------------------------------------------
    # Solscan
    # ------------------------------------------------------------------

    solscan_base_url: str = (
        "https://pro-api.solscan.io/v2.0"
    )

    solscan_api_key: Optional[str] = None

    # ------------------------------------------------------------------
    # Helius
    # ------------------------------------------------------------------

    helius_base_url: str = (
        "https://mainnet.helius-rpc.com"
    )

    helius_api_key: Optional[str] = None

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
    # Pump.fun is treated as a separate launch source.
    #
    # This is the Pump.fun mint-authority identifier supplied for
    # detecting Pump.fun-created tokens.
    #
    # It must NOT be treated as an Anoncoin creator wallet.
    # ------------------------------------------------------------------

    pumpfun_mint_authority: str = Field(
        default=(
            "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"
        ),
        validation_alias="PUMPFUN_MINT_AUTHORITY",
    )

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
            "6cNjLym8bDZ5JFGFSDom2us27iF7EBHYUXdFCdC5zWhX"
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

    qualify_score_threshold: float = 50.0

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

    default_slippage_bps: int = 300

    # ------------------------------------------------------------------
    # Timers
    # ------------------------------------------------------------------

    scan_interval_seconds: int = 30

    position_check_interval_seconds: int = 5

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
