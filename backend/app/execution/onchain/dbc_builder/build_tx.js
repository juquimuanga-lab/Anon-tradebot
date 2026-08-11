// Builds an UNSIGNED swap transaction against Meteora's public Dynamic
// Bonding Curve program.
//
// Priority fees:
// - Builds the transaction first without a priority fee.
// - Uses Helius getPriorityFeeEstimate against the exact transaction/accounts.
// - Requests the HIGH priority level for time-sensitive sniper trades.
// - Adds ComputeBudgetProgram.setComputeUnitPrice() to the transaction.
// - Refreshes the blockhash after the priority instruction is added.
//
// No private key ever touches this script.
// Signing happens in Python after this script returns the unsigned tx.

const { Connection, PublicKey, ComputeBudgetProgram } = require('@solana/web3.js');
const {
  DynamicBondingCurveClient,
  getCurrentPoint,
} = require('@meteora-ag/dynamic-bonding-curve-sdk');
const BN = require('bn.js');
const bs58 = require('bs58');


// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

// Helius High priority is appropriate for time-sensitive/sniper transactions.
// Helius documents High as the 75th-percentile priority level and recommends
// High/VeryHigh for time-sensitive transactions.
const PRIORITY_LEVEL = 'High';

// If Helius priority-fee estimation is temporarily unavailable, don't abandon
// the trade completely. Use a modest fallback priority fee instead.
//
// 10,000 micro-lamports/CU = 0.00001 lamports/CU.
//
// This is only a fallback. Under normal operation the Helius estimate is used.
const FALLBACK_PRIORITY_FEE_MICROLAMPORTS = 10_000;


// ---------------------------------------------------------------------------
// stdin helper
// ---------------------------------------------------------------------------

function readStdin() {
  return new Promise((resolve) => {
    let data = '';

    process.stdin.on('data', (chunk) => {
      data += chunk;
    });

    process.stdin.on('end', () => {
      resolve(data);
    });
  });
}


// ---------------------------------------------------------------------------
// Helius priority fee estimation
// ---------------------------------------------------------------------------

async function getPriorityFeeEstimate(connection, transaction) {
  // Helius supports estimating from the exact transaction. This is more
  // accurate than estimating from a generic account list because Helius can
  // inspect the actual instructions and accounts involved in the trade.
  //
  // The transaction is unsigned here, which is intentional. The Python
  // process will sign it after the priority-fee instruction is added.

  const serialized = transaction.serialize({
    requireAllSignatures: false,
    verifySignatures: false,
  });

  const serializedBase58 = bs58.encode(serialized);

  const response = await fetch(connection.rpcEndpoint, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 'anon-tradebot-priority-fee',
      method: 'getPriorityFeeEstimate',
      params: [
        {
          transaction: serializedBase58,
          options: {
            priorityLevel: PRIORITY_LEVEL,
            recommended: true,
          },
        },
      ],
    }),
  });

  if (!response.ok) {
    throw new Error(
      `Helius priority fee request failed with HTTP ${response.status}`
    );
  }

  const body = await response.json();

  if (body.error) {
    throw new Error(
      `Helius priority fee RPC error: ${JSON.stringify(body.error)}`
    );
  }

  const estimate = body?.result?.priorityFeeEstimate;

  if (
    estimate === undefined ||
    estimate === null ||
    !Number.isFinite(Number(estimate)) ||
    Number(estimate) < 0
  ) {
    throw new Error(
      `Helius returned an invalid priority fee estimate: ${JSON.stringify(body)}`
    );
  }

  return Math.ceil(Number(estimate));
}


// ---------------------------------------------------------------------------
// Detect existing Compute Budget priority-price instruction
// ---------------------------------------------------------------------------

function hasComputeUnitPriceInstruction(transaction) {
  const computeBudgetProgramId = ComputeBudgetProgram.programId.toBase58();

  return transaction.instructions.some((instruction) => {
    if (!instruction.programId.equals(ComputeBudgetProgram.programId)) {
      return false;
    }

    if (!instruction.data || instruction.data.length === 0) {
      return false;
    }

    // ComputeBudget instruction discriminator:
    //
    // 2 = SetComputeUnitLimit
    // 3 = SetComputeUnitPrice
    //
    // We only care about detecting an existing SetComputeUnitPrice.
    return instruction.data[0] === 3;
  });
}


// ---------------------------------------------------------------------------
// Main transaction builder
// ---------------------------------------------------------------------------

async function main() {
  const raw = await readStdin();

  const input = JSON.parse(raw);

  const {
    action,
    baseMint,
    ownerPubkey,
    amountLamports,
    slippageBps,
    rpcUrl,
  } = input;

  if (!rpcUrl) {
    throw new Error('rpc_url_missing');
  }

  if (!baseMint) {
    throw new Error('base_mint_missing');
  }

  if (!ownerPubkey) {
    throw new Error('owner_pubkey_missing');
  }

  if (!action || !['buy', 'sell'].includes(action)) {
    throw new Error(`invalid_action: ${action}`);
  }

  const connection = new Connection(rpcUrl, 'confirmed');

  const client = DynamicBondingCurveClient.create(
    connection,
    'confirmed'
  );


  // -------------------------------------------------------------------------
  // 1. Find Meteora DBC pool
  // -------------------------------------------------------------------------

  const poolAccount = await client.state.getPoolByBaseMint(baseMint);

  if (!poolAccount) {
    throw new Error('pool_not_found_for_mint');
  }

  const poolAddress = poolAccount.publicKey;
  const virtualPool = poolAccount.account;


  // -------------------------------------------------------------------------
  // 2. Load pool configuration
  // -------------------------------------------------------------------------

  const config = await client.state.getPoolConfig(
    virtualPool.poolState.config
  );

  if (!config) {
    throw new Error('pool_config_not_found');
  }


  // -------------------------------------------------------------------------
  // 3. Calculate swap quote
  // -------------------------------------------------------------------------

  const swapBaseForQuote = action === 'sell';

  const amountIn = new BN(String(amountLamports));

  const currentPoint = await getCurrentPoint(
    connection,
    config.activationType
  );

  const effectiveSlippageBps =
    slippageBps || 300;


  const quote = client.pool.swapQuote({
    virtualPool,
    config,
    swapBaseForQuote,
    amountIn,
    slippageBps: effectiveSlippageBps,
    hasReferral: false,
    eligibleForFirstSwapWithMinFee: false,
    currentPoint,
  });


  // -------------------------------------------------------------------------
  // 4. Calculate minimum acceptable output
  // -------------------------------------------------------------------------

  const outputAmount = new BN(
    quote.outputAmount.toString()
  );

  const slippageBpsBN = new BN(
    effectiveSlippageBps
  );

  const tenThousand = new BN(10000);

  const minimumAmountOut = outputAmount
    .mul(tenThousand.sub(slippageBpsBN))
    .div(tenThousand);


  // -------------------------------------------------------------------------
  // 5. Build Meteora swap transaction
  // -------------------------------------------------------------------------

  const owner = new PublicKey(ownerPubkey);

  const tx = await client.pool.swap({
    owner,
    pool: poolAddress,
    amountIn,
    minimumAmountOut,
    swapBaseForQuote,
    referralTokenAccount: null,
    payer: owner,
  });

  tx.feePayer = owner;


  // -------------------------------------------------------------------------
  // 6. Set a recent blockhash BEFORE asking Helius for the fee estimate
  // -------------------------------------------------------------------------

  const initialBlockhash = await connection.getLatestBlockhash(
    'confirmed'
  );

  tx.recentBlockhash = initialBlockhash.blockhash;


  // -------------------------------------------------------------------------
  // 7. Estimate a transaction-specific priority fee through Helius
  // -------------------------------------------------------------------------

  let priorityFeeMicroLamports =
    FALLBACK_PRIORITY_FEE_MICROLAMPORTS;

  let priorityFeeSource = 'fallback';

  try {
    priorityFeeMicroLamports =
      await getPriorityFeeEstimate(connection, tx);

    priorityFeeSource = 'helius-high';
  } catch (feeError) {
    // Do not prevent a potentially valid trade solely because the priority
    // fee endpoint temporarily failed. We retain a modest fallback fee.
    //
    // The actual error is returned in the response so Python/logging can
    // expose that the fallback was used.
    priorityFeeSource = 'fallback';

    console.error(
      `Helius priority fee estimation failed; using fallback: ${
        feeError?.message || feeError
      }`
    );
  }


  // -------------------------------------------------------------------------
  // 8. Add priority fee instruction
  // -------------------------------------------------------------------------

  let priorityFeeInstructionAdded = false;

  if (!hasComputeUnitPriceInstruction(tx)) {
    tx.add(
      ComputeBudgetProgram.setComputeUnitPrice({
        microLamports: priorityFeeMicroLamports,
      })
    );

    priorityFeeInstructionAdded = true;
  }


  // -------------------------------------------------------------------------
  // 9. Refresh blockhash AFTER modifying the transaction
  // -------------------------------------------------------------------------

  const finalBlockhash = await connection.getLatestBlockhash(
    'confirmed'
  );

  tx.recentBlockhash = finalBlockhash.blockhash;


  // -------------------------------------------------------------------------
  // 10. Serialize unsigned transaction
  // -------------------------------------------------------------------------

  const serialized = tx.serialize({
    requireAllSignatures: false,
    verifySignatures: false,
  });


  // -------------------------------------------------------------------------
  // 11. Return transaction to Python for signing
  // -------------------------------------------------------------------------

  process.stdout.write(
    JSON.stringify({
      success: true,

      transaction_b64: serialized.toString('base64'),

      blockhash: finalBlockhash.blockhash,

      last_valid_block_height:
        finalBlockhash.lastValidBlockHeight,

      quoted_output_amount:
        quote.outputAmount.toString(),

      minimum_amount_out:
        minimumAmountOut.toString(),

      pool_address:
        poolAddress.toBase58(),

      priority_fee_micro_lamports:
        priorityFeeMicroLamports,

      priority_fee_source:
        priorityFeeSource,

      priority_level:
        PRIORITY_LEVEL,

      priority_fee_instruction_added:
        priorityFeeInstructionAdded,

      action,

      base_mint:
        baseMint,
    }) + '\n'
  );
}


// ---------------------------------------------------------------------------
// Error handling
// ---------------------------------------------------------------------------

main().catch((err) => {
  process.stdout.write(
    JSON.stringify({
      success: false,
      error: String(
        (err && err.message) || err
      ),
    }) + '\n'
  );

  process.exit(0);
});
