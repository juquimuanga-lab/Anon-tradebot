// Read-only pool state reader for a Meteora DBC pool given its base mint.
// No private key or owner pubkey needed - this only reads on-chain state to
// compute price/liquidity/market-cap/migration status for scoring.
const { Connection } = require('@solana/web3.js');
const {
  DynamicBondingCurveClient,
  getPriceFromSqrtPrice,
} = require('@meteora-ag/dynamic-bonding-curve-sdk');

function readStdin() {
  return new Promise((resolve) => {
    let data = '';
    process.stdin.on('data', (chunk) => (data += chunk));
    process.stdin.on('end', () => resolve(data));
  });
}

async function main() {
  const raw = await readStdin();
  const { baseMint, rpcUrl } = JSON.parse(raw);

  const connection = new Connection(rpcUrl, 'confirmed');
  const client = DynamicBondingCurveClient.create(connection, 'confirmed');

  const poolAccount = await client.state.getPoolByBaseMint(baseMint);
  if (!poolAccount) {
    throw new Error('pool_not_found_for_mint');
  }
  const pool = poolAccount.account.poolState;
  const config = await client.state.getPoolConfig(pool.config);

  const priceSolPerToken = getPriceFromSqrtPrice(pool.sqrtPrice, config.tokenDecimal, 9);
  const supplyTokens = Number(config.preMigrationTokenSupply.toString()) / 10 ** config.tokenDecimal;
  const quoteReserveSol = Number(pool.quoteReserve.toString()) / 1e9;
  const migrationThresholdSol = Number(config.migrationQuoteThreshold.toString()) / 1e9;

  process.stdout.write(
    JSON.stringify({
      success: true,
      pool_address: poolAccount.publicKey.toBase58(),
      creator: pool.creator.toBase58(),
      token_decimals: config.tokenDecimal,
      price_sol_per_token: Number(priceSolPerToken.toString()),
      supply_tokens: supplyTokens,
      market_cap_sol: Number(priceSolPerToken.toString()) * supplyTokens,
      quote_reserve_sol: quoteReserveSol,
      migration_threshold_sol: migrationThresholdSol,
      is_migrated: Boolean(pool.isMigrated),
    }) + '\n'
  );
}

main().catch((err) => {
  process.stdout.write(
    JSON.stringify({ success: false, error: String((err && err.message) || err) }) + '\n'
  );
  process.exit(0);
});
