/**
 * Persistent Meteora DBC price worker.
 *
 * This process is intentionally long-lived.
 *
 * Startup:
 *
 *   Node starts once
 *       ↓
 *   Meteora SDK loads once
 *       ↓
 *   Solana connection is created once per RPC URL
 *       ↓
 *   Python sends price requests over stdin
 *       ↓
 *   Worker returns one JSON response per request
 *
 * This removes the expensive:
 *
 *   Python -> spawn Node -> load SDK -> RPC -> exit
 *
 * cycle from every position-price check.
 *
 * IMPORTANT:
 *
 * This worker is READ-ONLY.
 *
 * It:
 *   - never receives private keys
 *   - never signs transactions
 *   - never submits transactions
 *   - only reads Meteora pool state
 *
 * Transaction construction and confirmation remain separate.
 */

const readline = require('readline');

const { Connection } = require('@solana/web3.js');

const {
  DynamicBondingCurveClient,
  getPriceFromSqrtPrice,
} = require('@meteora-ag/dynamic-bonding-curve-sdk');


// ---------------------------------------------------------------------------
// Worker state
// ---------------------------------------------------------------------------

const connections = new Map();

const clients = new Map();


// ---------------------------------------------------------------------------
// Commitment
// ---------------------------------------------------------------------------

function normalizeCommitment(value) {
  if (
    value === 'processed' ||
    value === 'confirmed' ||
    value === 'finalized'
  ) {
    return value;
  }

  return 'processed';
}


// ---------------------------------------------------------------------------
// Connection/client cache
// ---------------------------------------------------------------------------

function getClient(
  rpcUrl,
  commitment
) {
  const key =
    `${rpcUrl}|${commitment}`;

  let client =
    clients.get(key);

  if (client) {
    return client;
  }


  let connection =
    connections.get(key);

  if (!connection) {

    connection =
      new Connection(
        rpcUrl,
        commitment
      );

    connections.set(
      key,
      connection
    );
  }


  client =
    DynamicBondingCurveClient.create(
      connection,
      commitment
    );

  clients.set(
    key,
    client
  );

  return client;
}


// ---------------------------------------------------------------------------
// Pool price
// ---------------------------------------------------------------------------

async function readPoolPrice(
  payload
) {
  const {
    baseMint,
    rpcUrl,
  } = payload;


  const commitment =
    normalizeCommitment(
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


  const client =
    getClient(
      rpcUrl,
      commitment
    );


  // -------------------------------------------------------------------------
  // Pool
  // -------------------------------------------------------------------------

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


  // -------------------------------------------------------------------------
  // Pool configuration
  // -------------------------------------------------------------------------

  const config =
    await client.state.getPoolConfig(
      pool.config
    );


  // -------------------------------------------------------------------------
  // Price
  // -------------------------------------------------------------------------

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


  // -------------------------------------------------------------------------
  // Supply / market cap
  // -------------------------------------------------------------------------

  const supplyTokens =
    Number(
      config.preMigrationTokenSupply.toString()
    ) /
    10 ** config.tokenDecimal;


  const marketCapSol =
    priceSolNumber *
    supplyTokens;


  // -------------------------------------------------------------------------
  // Reserves
  // -------------------------------------------------------------------------

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


  return {
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
  };
}


// ---------------------------------------------------------------------------
// JSON response
// ---------------------------------------------------------------------------

function writeResponse(
  requestId,
  result
) {
  process.stdout.write(
    JSON.stringify({
      requestId,
      ...result,
    }) + '\n'
  );
}


// ---------------------------------------------------------------------------
// Request handling
// ---------------------------------------------------------------------------

async function handleRequest(
  payload
) {
  const requestId =
    payload.requestId;


  try {

    const result =
      await readPoolPrice(
        payload
      );


    writeResponse(
      requestId,
      result
    );

  } catch (error) {

    writeResponse(
      requestId,
      {
        success: false,

        error: String(
          (
            error &&
            error.message
          ) ||
          error
        ),
      }
    );
  }
}


// ---------------------------------------------------------------------------
// Persistent stdin interface
// ---------------------------------------------------------------------------

const rl =
  readline.createInterface({
    input: process.stdin,
    crlfDelay: Infinity,
  });


rl.on(
  'line',
  async (line) => {

    const trimmed =
      line.trim();


    if (!trimmed) {
      return;
    }


    let payload;


    try {

      payload =
        JSON.parse(
          trimmed
        );

    } catch (error) {

      writeResponse(
        null,
        {
          success: false,
          error:
            'invalid_json_request',
        }
      );

      return;
    }


    await handleRequest(
      payload
    );
  }
);


// ---------------------------------------------------------------------------
// Shutdown
// ---------------------------------------------------------------------------

function shutdown() {
  process.exit(
    0
  );
}


process.on(
  'SIGTERM',
  shutdown
);

process.on(
  'SIGINT',
  shutdown
);
