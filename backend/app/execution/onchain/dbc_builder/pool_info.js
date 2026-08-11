// Read-only pool state reader for a Meteora DBC pool given its base mint.
//
// No private key or owner pubkey is needed.
//
// This file is used for FAST market-price monitoring.
//
// IMPORTANT:
// - "processed" is used by default for low-latency price observations.
// - This file does NOT determine whether a transaction succeeded.
// - Transaction confirmation remains handled by solana_rpc.py.
// - A slow RPC must not keep the Python position monitor blocked for a
//   long period; the Python wrapper enforces the outer timeout.

const { Connection } = require('@solana/web3.js');
const {
  DynamicBondingCurveClient,
  getPriceFromSqrtPrice,
} = require('@meteora-ag/dynamic-bonding-curve-sdk');


function readStdin() {
  return new Promise((resolve, reject) => {
    let data = '';

    process.stdin.on(
      'data',
      (chunk) => {
        data += chunk;
      }
    );

    process.stdin.on(
      'end',
      () => {
        resolve(data);
      }
    );

    process.stdin.on(
      'error',
      reject
    );
  });
}


function normalizeCommitment(value) {
  // Only allow Solana commitment values we intentionally support here.

  if (
    value === 'processed' ||
    value === 'confirmed' ||
    value === 'finalized'
  ) {
    return value;
  }

  return 'processed';
}


async function main() {
  const raw = await readStdin();

  const payload = JSON.parse(raw);

  const {
    baseMint,
    rpcUrl,
  } = payload;

  const commitment = normalizeCommitment(
    payload.commitment
  );


  if (!baseMint) {
    throw new Error(
      'baseMint is required'
    );
  }

  if (!rpcUrl) {
    throw new Error(
      'rpcUrl is required'
    );
  }


  // This is a READ-ONLY price observation.
  //
  // processed gives the monitor the newest available bank state without
  // waiting for a voted confirmation.
  const connection = new Connection(
    rpcUrl,
    commitment
  );

  const client =
    DynamicBondingCurveClient.create(
      connection,
      commitment
    );


  // -----------------------------------------------------------------------
  // Pool state
  // -----------------------------------------------------------------------

  const poolAccount =
    await client.state.getPoolByBaseMint(
      baseMint
    );

  if (!poolAccount) {
    throw new Error(
      'pool_not_found_for_mint'
    );
  }


  const pool =
    poolAccount.account.poolState;


  // -----------------------------------------------------------------------
  // Pool configuration
  // -----------------------------------------------------------------------

  const config =
    await client.state.getPoolConfig(
      pool.config
    );


  // -----------------------------------------------------------------------
  // Price
  // -----------------------------------------------------------------------

  const priceSolPerToken =
    getPriceFromSqrtPrice(
      pool.sqrtPrice,
      config.tokenDecimal,
      9
    );

  const priceSolNumber =
    Number(
      priceSolPerToken.toString()
    );

  if (
    !Number.isFinite(
      priceSolNumber
    ) ||
    priceSolNumber <= 0
  ) {
    throw new Error(
      'invalid_pool_price'
    );
  }


  // -----------------------------------------------------------------------
  // Supply / market cap
  // -----------------------------------------------------------------------

  const supplyTokens =
    Number(
      config.preMigrationTokenSupply.toString()
    ) /
    10 ** config.tokenDecimal;


  const marketCapSol =
    priceSolNumber *
    supplyTokens;


  // -----------------------------------------------------------------------
  // Reserves / migration
  // -----------------------------------------------------------------------

  const quoteReserveSol =
    Number(
      pool.quoteReserve.toString()
    ) /
    1e9;


  const migrationThresholdSol =
    Number(
      config.migrationQuoteThreshold.toString()
    ) /
    1e9;


  // -----------------------------------------------------------------------
  // Return compact JSON to Python.
  // -----------------------------------------------------------------------

  process.stdout.write(
    JSON.stringify({
      success: true,

      pool_address:
        poolAccount.publicKey.toBase58(),

      creator:
        pool.creator.toBase58(),

      token_decimals:
        config.tokenDecimal,

      price_sol_per_token:
        priceSolNumber,

      supply_tokens:
        supplyTokens,

      market_cap_sol:
        marketCapSol,

      quote_reserve_sol:
        quoteReserveSol,

      migration_threshold_sol:
        migrationThresholdSol,

      is_migrated:
        Boolean(
          pool.isMigrated
        ),

      commitment:
        commitment,
    }) + '\n'
  );
}


main().catch(
  (err) => {
    process.stdout.write(
      JSON.stringify({
        success: false,
        error: String(
          (
            err &&
            err.message
          ) ||
          err
        ),
      }) + '\n'
    );

    process.exit(0);
  }
);
