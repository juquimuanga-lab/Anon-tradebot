/**
 * Pump.fun unsigned BUY transaction builder.
 *
 * Architecture:
 *
 *     Python
 *        ↓
 *     this file
 *        ↓
 *     Pump.fun SDK
 *        ↓
 *     unsigned transaction
 *        ↓
 *     Python signs
 *        ↓
 *     solana_rpc.py broadcasts/confirms
 *
 * IMPORTANT:
 *
 * - No private key is ever passed to this file.
 * - This file never signs a transaction.
 * - This file never submits a transaction.
 * - ownerPubkey is only used as the transaction payer/user.
 *
 * The Pump.fun SDK is responsible for constructing the current bonding-curve
 * account list, including current fee-recipient requirements.
 */

const {
  Connection,
  PublicKey,
  Transaction,
  ComputeBudgetProgram,
} = require("@solana/web3.js");

const {
  PumpSdk,
  getBuyTokenAmountFromSolAmount,
} = require("@pump-fun/pump-sdk");

const BN = require("bn.js");
const bs58 = require("bs58");

const {
  TOKEN_PROGRAM_ID,
} = require("@solana/spl-token");


// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

// Helius High is appropriate for latency-sensitive launch transactions.
const PRIORITY_LEVEL = "High";

// Used only if Helius priority estimation is unavailable.
const FALLBACK_PRIORITY_FEE_MICROLAMPORTS = 10_000;

// Pump SDK slippage is expressed as a percentage.
//
// Example:
//     300 bps = 3%
//     500 bps = 5%
function slippageBpsToPercent(slippageBps) {
  const bps = Number(slippageBps);

  if (!Number.isFinite(bps) || bps < 0) {
    return 3;
  }

  return bps / 100;
}


// ---------------------------------------------------------------------------
// stdin
// ---------------------------------------------------------------------------

function readStdin() {
  return new Promise((resolve) => {
    let data = "";

    process.stdin.on("data", (chunk) => {
      data += chunk;
    });

    process.stdin.on("end", () => {
      resolve(data);
    });
  });
}


// ---------------------------------------------------------------------------
// Helius priority fee estimation
// ---------------------------------------------------------------------------

async function getPriorityFeeEstimate(
  connection,
  transaction
) {
  const serialized = transaction.serialize({
    requireAllSignatures: false,
    verifySignatures: false,
  });

  const serializedBase58 = bs58.encode(
    serialized
  );

  const response = await fetch(
    connection.rpcEndpoint,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: "anon-tradebot-pumpfun-priority-fee",
        method: "getPriorityFeeEstimate",
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
    }
  );

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

  const estimate =
    body?.result?.priorityFeeEstimate;

  if (
    estimate === undefined ||
    estimate === null ||
    !Number.isFinite(Number(estimate)) ||
    Number(estimate) < 0
  ) {
    throw new Error(
      `Invalid Helius priority fee estimate: ${JSON.stringify(body)}`
    );
  }

  return Math.ceil(
    Number(estimate)
  );
}


// ---------------------------------------------------------------------------
// Existing Compute Budget detection
// ---------------------------------------------------------------------------

function hasComputeUnitPriceInstruction(
  transaction
) {
  return transaction.instructions.some(
    (instruction) => {
      if (
        !instruction.programId.equals(
          ComputeBudgetProgram.programId
        )
      ) {
        return false;
      }

      if (
        !instruction.data ||
        instruction.data.length === 0
      ) {
        return false;
      }

      // Compute Budget instruction:
      //
      // 2 = SetComputeUnitLimit
      // 3 = SetComputeUnitPrice
      //
      return instruction.data[0] === 3;
    }
  );
}


// ---------------------------------------------------------------------------
// Validation helpers
// ---------------------------------------------------------------------------

function requirePositiveInteger(
  value,
  field
) {
  const parsed = Number(value);

  if (
    !Number.isSafeInteger(parsed) ||
    parsed <= 0
  ) {
    throw new Error(
      `${field}_must_be_positive_integer`
    );
  }

  return parsed;
}


// ---------------------------------------------------------------------------
// Main
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


  // -------------------------------------------------------------------------
  // Validate
  // -------------------------------------------------------------------------

  if (!rpcUrl) {
    throw new Error(
      "rpc_url_missing"
    );
  }

  if (!baseMint) {
    throw new Error(
      "base_mint_missing"
    );
  }

  if (!ownerPubkey) {
    throw new Error(
      "owner_pubkey_missing"
    );
  }

  if (action !== "buy") {
    throw new Error(
      "pumpfun_builder_only_supports_buy"
    );
  }


  const lamports = requirePositiveInteger(
    amountLamports,
    "amount_lamports"
  );


  // Validate public keys before making RPC requests.
  const mint = new PublicKey(
    baseMint
  );

  const user = new PublicKey(
    ownerPubkey
  );


  // -------------------------------------------------------------------------
  // Connection
  // -------------------------------------------------------------------------

  const connection = new Connection(
    rpcUrl,
    {
      commitment: "processed",
    }
  );


  // -------------------------------------------------------------------------
  // Pump SDK
  // -------------------------------------------------------------------------

  const sdk = new PumpSdk(
    connection
  );


  // -------------------------------------------------------------------------
  // Fetch current Pump.fun global state
  // -------------------------------------------------------------------------

  const global =
    await sdk.fetchGlobal();


  if (!global) {
    throw new Error(
      "pumpfun_global_state_not_found"
    );
  }


  // -------------------------------------------------------------------------
  // Fetch current Pump.fun fee configuration
  //
  // Newer Pump.fun deployments use fee configuration as part of the
  // bonding-curve quote calculation.
  // -------------------------------------------------------------------------

  let feeConfig = null;

  try {
    feeConfig =
      await sdk.fetchFeeConfig();
  } catch (error) {
    // Some SDK versions/builds do not require feeConfig for the instruction
    // builder itself. We therefore keep this optional.
    //
    // The quote function below will only receive it when available.
    console.error(
      `Pump.fun fee config unavailable: ${
        error?.message || error
      }`
    );
  }


  // -------------------------------------------------------------------------
  // Fetch live bonding-curve state
  //
  // This obtains:
  //
  // - bondingCurve
  // - bondingCurveAccountInfo
  // - associatedUserAccountInfo
  //
  // The SDK handles the current account layout.
  // -------------------------------------------------------------------------

  const buyState =
    await sdk.fetchBuyState(
      mint,
      user
    );


  if (!buyState) {
    throw new Error(
      "pumpfun_buy_state_not_found"
    );
  }


  const {
    bondingCurveAccountInfo,
    bondingCurve,
    associatedUserAccountInfo,
  } = buyState;


  if (!bondingCurveAccountInfo) {
    throw new Error(
      "pumpfun_bonding_curve_account_info_missing"
    );
  }

  if (!bondingCurve) {
    throw new Error(
      "pumpfun_bonding_curve_state_missing"
    );
  }


  // -------------------------------------------------------------------------
  // Do not attempt to buy a completed/migrated bonding curve.
  // -------------------------------------------------------------------------

  if (
    bondingCurve.complete === true
  ) {
    throw new Error(
      "pumpfun_bonding_curve_already_complete"
    );
  }


  // -------------------------------------------------------------------------
  // SOL amount
  //
  // This is the maximum SOL the user is allowing the transaction to spend.
  // -------------------------------------------------------------------------

  const solAmount = new BN(
    String(lamports)
  );


  // -------------------------------------------------------------------------
  // Calculate expected token amount
  //
  // Pump SDK calculates the bonding-curve output using the live global
  // state, fee configuration and bonding-curve state.
  //
  // We support both the current fee-aware signature and the older SDK
  // signature as a compatibility fallback.
  // -------------------------------------------------------------------------

  let tokenAmount;

  try {
    if (feeConfig) {
      tokenAmount =
        getBuyTokenAmountFromSolAmount({
          global,
          feeConfig,
          mintSupply:
            bondingCurve.tokenTotalSupply,
          bondingCurve,
          amount: solAmount,
        });
    } else {
      tokenAmount =
        getBuyTokenAmountFromSolAmount(
          global,
          bondingCurve,
          solAmount
        );
    }
  } catch (firstError) {
    try {
      tokenAmount =
        getBuyTokenAmountFromSolAmount({
          global,
          bondingCurve,
          amount: solAmount,
        });
    } catch (secondError) {
      throw new Error(
        "pumpfun_buy_quote_failed: " +
        `${secondError?.message || firstError}`
      );
    }
  }


  if (!tokenAmount) {
    throw new Error(
      "pumpfun_token_amount_calculation_failed"
    );
  }


  const tokenAmountBN =
    new BN(
      tokenAmount.toString()
    );


  if (
    tokenAmountBN.lte(
      new BN(0)
    )
  ) {
    throw new Error(
      "pumpfun_calculated_token_amount_zero"
    );
  }


  // -------------------------------------------------------------------------
  // Slippage
  // -------------------------------------------------------------------------

  const slippagePercent =
    slippageBpsToPercent(
      slippageBps
    );


  // -------------------------------------------------------------------------
  // Build Pump.fun buy instructions
  //
  // The official SDK constructs the current Pump.fun bonding-curve
  // instruction set and ATA handling.
  // -------------------------------------------------------------------------

  const instructions =
    await sdk.buyInstructions({
      global,
      bondingCurveAccountInfo,
      bondingCurve,
      associatedUserAccountInfo,
      mint,
      user,

      // Exact token quantity expected from the current curve.
      amount: tokenAmountBN,

      // Maximum SOL allowed to be spent.
      solAmount,

      // Pump SDK expects percentage, not basis points.
      slippage: slippagePercent,

      // Explicitly use the standard SPL Token program.
      tokenProgram: TOKEN_PROGRAM_ID,
    });


  if (
    !instructions ||
    !Array.isArray(instructions) ||
    instructions.length === 0
  ) {
    throw new Error(
      "pumpfun_buy_instructions_empty"
    );
  }


  // -------------------------------------------------------------------------
  // Build unsigned transaction
  // -------------------------------------------------------------------------

  const tx = new Transaction();

  tx.feePayer = user;

  tx.add(
    ...instructions
  );


  // -------------------------------------------------------------------------
  // Initial blockhash
  //
  // We need a blockhash before priority-fee estimation because Helius should
  // inspect the actual transaction as closely as possible.
  // -------------------------------------------------------------------------

  const initialBlockhash =
    await connection.getLatestBlockhash(
      "processed"
    );

  tx.recentBlockhash =
    initialBlockhash.blockhash;


  // -------------------------------------------------------------------------
  // Helius priority fee
  // -------------------------------------------------------------------------

  let priorityFeeMicroLamports =
    FALLBACK_PRIORITY_FEE_MICROLAMPORTS;

  let priorityFeeSource =
    "fallback";


  try {
    priorityFeeMicroLamports =
      await getPriorityFeeEstimate(
        connection,
        tx
      );

    priorityFeeSource =
      "helius-high";

  } catch (feeError) {

    console.error(
      "Pump.fun Helius priority fee estimation failed; " +
      `using fallback: ${
        feeError?.message || feeError
      }`
    );
  }


  // -------------------------------------------------------------------------
  // Add priority fee
  // -------------------------------------------------------------------------

  let priorityFeeInstructionAdded =
    false;


  if (
    !hasComputeUnitPriceInstruction(
      tx
    )
  ) {

    tx.add(
      ComputeBudgetProgram.setComputeUnitPrice({
        microLamports:
          priorityFeeMicroLamports,
      })
    );

    priorityFeeInstructionAdded =
      true;
  }


  // -------------------------------------------------------------------------
  // Refresh blockhash AFTER all instructions have been added.
  //
  // This is important because the transaction will be signed later by
  // Python. We want the returned transaction and expiry information to
  // correspond to the final transaction.
  // -------------------------------------------------------------------------

  const finalBlockhash =
    await connection.getLatestBlockhash(
      "processed"
    );

  tx.recentBlockhash =
    finalBlockhash.blockhash;


  // -------------------------------------------------------------------------
  // Serialize unsigned transaction
  // -------------------------------------------------------------------------

  const serialized =
    tx.serialize({
      requireAllSignatures: false,
      verifySignatures: false,
    });


  // -------------------------------------------------------------------------
  // Return to Python
  // -------------------------------------------------------------------------

  process.stdout.write(
    JSON.stringify({
      success: true,

      transaction_b64:
        serialized.toString(
          "base64"
        ),

      blockhash:
        finalBlockhash.blockhash,

      last_valid_block_height:
        finalBlockhash.lastValidBlockHeight,

      base_mint:
        mint.toBase58(),

      owner_pubkey:
        user.toBase58(),

      amount_lamports:
        lamports,

      expected_token_amount:
        tokenAmountBN.toString(),

      slippage_bps:
        Number(
          slippageBps || 300
        ),

      slippage_percent:
        slippagePercent,

      priority_fee_micro_lamports:
        priorityFeeMicroLamports,

      priority_fee_source:
        priorityFeeSource,

      priority_level:
        PRIORITY_LEVEL,

      priority_fee_instruction_added:
        priorityFeeInstructionAdded,

      instruction_count:
        instructions.length,

      bonding_curve:
        bondingCurveAccountInfo.pubkey
          ? bondingCurveAccountInfo.pubkey.toBase58()
          : null,

      action:
        "buy",

    }) + "\n"
  );
}


// ---------------------------------------------------------------------------
// Error handling
// ---------------------------------------------------------------------------

main().catch((err) => {

  process.stdout.write(
    JSON.stringify({
      success: false,

      error:
        String(
          (err && err.message) ||
          err
        ),
    }) + "\n"
  );

  // The Python wrapper treats the JSON response as the authoritative result.
  process.exit(0);
});
