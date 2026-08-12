/**
 * Pump.fun unsigned SELL transaction builder.
 *
 * Python:
 *   - supplies the wallet public key
 *   - supplies the mint
 *   - supplies the exact raw token amount
 *   - signs the returned transaction
 *   - broadcasts/confirms it
 *
 * This file:
 *   - never receives a private key
 *   - never signs
 *   - never broadcasts
 *
 * Pump SDK 1.36.x is used for the current Pump.fun bonding-curve
 * sell instruction set.
 */

const {
  Connection,
  PublicKey,
  Transaction,
  ComputeBudgetProgram,
} = require("@solana/web3.js");

const {
  PumpSdk,
  getSellSolAmountFromTokenAmount,
} = require("@pump-fun/pump-sdk");

const BN = require("bn.js");
const bs58 = require("bs58");


// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const PRIORITY_LEVEL = "High";

const FALLBACK_PRIORITY_FEE_MICROLAMPORTS = 10_000;


// ---------------------------------------------------------------------------
// stdin
// ---------------------------------------------------------------------------

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = "";

    process.stdin.on("data", (chunk) => {
      data += chunk;
    });

    process.stdin.on("end", () => {
      resolve(data);
    });

    process.stdin.on("error", reject);
  });
}


// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function slippageBpsToPercent(slippageBps) {
  const bps = Number(slippageBps);

  if (!Number.isFinite(bps) || bps < 0) {
    return 3;
  }

  return bps / 100;
}


function hasComputeUnitPriceInstruction(transaction) {
  return transaction.instructions.some((instruction) => {
    if (
      !instruction.programId.equals(
        ComputeBudgetProgram.programId
      )
    ) {
      return false;
    }

    if (!instruction.data || instruction.data.length === 0) {
      return false;
    }

    return instruction.data[0] === 3;
  });
}


// ---------------------------------------------------------------------------
// Helius priority fee
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

        id:
          "anon-tradebot-pumpfun-sell-priority-fee",

        method:
          "getPriorityFeeEstimate",

        params: [
          {
            transaction: serializedBase58,

            options: {
              priorityLevel:
                PRIORITY_LEVEL,

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
      "Helius priority fee RPC error: " +
      JSON.stringify(body.error)
    );
  }

  const estimate =
    body?.result?.priorityFeeEstimate;

  if (
    estimate === undefined ||
    estimate === null ||
    !Number.isFinite(
      Number(estimate)
    ) ||
    Number(estimate) < 0
  ) {
    throw new Error(
      "Invalid Helius priority fee estimate: " +
      JSON.stringify(body)
    );
  }

  return Math.ceil(
    Number(estimate)
  );
}


// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  const raw = await readStdin();

  if (!raw.trim()) {
    throw new Error(
      "empty_stdin_payload"
    );
  }

  const input = JSON.parse(raw);

  const {
    action,
    baseMint,
    ownerPubkey,
    amountTokensRaw,
    slippageBps,
    rpcUrl,
  } = input;


  // -------------------------------------------------------------------------
  // Validate input
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

  if (action !== "sell") {
    throw new Error(
      "pumpfun_sell_builder_requires_sell_action"
    );
  }

  if (
    amountTokensRaw === undefined ||
    amountTokensRaw === null
  ) {
    throw new Error(
      "amount_tokens_raw_missing"
    );
  }


  // -------------------------------------------------------------------------
  // Raw SPL token amount
  //
  // NEVER use JavaScript floating-point arithmetic for token base units.
  // -------------------------------------------------------------------------

  const amount = new BN(
    String(
      amountTokensRaw
    )
  );

  if (
    amount.lte(
      new BN(0)
    )
  ) {
    throw new Error(
      "amount_tokens_raw_must_be_positive"
    );
  }


  // -------------------------------------------------------------------------
  // Public keys
  // -------------------------------------------------------------------------

  const mint = new PublicKey(
    baseMint
  );

  const user = new PublicKey(
    ownerPubkey
  );


  // -------------------------------------------------------------------------
  // Connection / SDK
  // -------------------------------------------------------------------------

  const connection = new Connection(
    rpcUrl,
    {
      commitment: "processed",
    }
  );

  const sdk = new PumpSdk(
    connection
  );


  // -------------------------------------------------------------------------
  // Fetch current Pump.fun state
  //
  // fetchSellState() determines the correct token program for the mint.
  // We use that returned tokenProgram rather than hard-coding the legacy
  // SPL Token program.
  // -------------------------------------------------------------------------

  const [
    global,
    feeConfig,
    sellState,
  ] = await Promise.all([
    sdk.fetchGlobal(),

    sdk.fetchFeeConfig(),

    sdk.fetchSellState(
      mint,
      user
    ),
  ]);


  if (!global) {
    throw new Error(
      "pumpfun_global_state_not_found"
    );
  }

  if (!feeConfig) {
    throw new Error(
      "pumpfun_fee_config_not_found"
    );
  }

  if (!sellState) {
    throw new Error(
      "pumpfun_sell_state_not_found"
    );
  }


  const {
    bondingCurveAccountInfo,
    bondingCurve,
    tokenProgram,
  } = sellState;


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

  if (!tokenProgram) {
    throw new Error(
      "pumpfun_token_program_missing_from_sell_state"
    );
  }


  // -------------------------------------------------------------------------
  // Bonding-curve sell only
  // -------------------------------------------------------------------------

  if (
    bondingCurve.complete === true
  ) {
    throw new Error(
      "pumpfun_bonding_curve_completed_sell_requires_graduated_executor"
    );
  }


  // -------------------------------------------------------------------------
  // Verify current wallet token balance.
  //
  // Both values are raw token units.
  // -------------------------------------------------------------------------

  const walletBalance =
    await sdk.getTokenBalance(
      mint,
      user,
      tokenProgram
    );


  if (
    walletBalance === undefined ||
    walletBalance === null
  ) {
    throw new Error(
      "pumpfun_token_balance_unavailable"
    );
  }


  if (
    walletBalance.lte(
      new BN(0)
    )
  ) {
    throw new Error(
      "pumpfun_wallet_token_balance_zero"
    );
  }


  if (
    amount.gt(
      walletBalance
    )
  ) {
    throw new Error(
      "pumpfun_sell_amount_exceeds_wallet_balance"
    );
  }


  // -------------------------------------------------------------------------
  // Calculate expected SOL output.
  // -------------------------------------------------------------------------

  const expectedSolAmount =
    getSellSolAmountFromTokenAmount({
      global,

      feeConfig,

      mintSupply:
        bondingCurve.tokenTotalSupply,

      bondingCurve,

      amount,
    });


  if (!expectedSolAmount) {
    throw new Error(
      "pumpfun_sell_sol_amount_calculation_failed"
    );
  }


  const expectedSolBN =
    new BN(
      expectedSolAmount.toString()
    );


  if (
    expectedSolBN.lte(
      new BN(0)
    )
  ) {
    throw new Error(
      "pumpfun_expected_sell_sol_amount_zero"
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
  // Mayhem mode
  // -------------------------------------------------------------------------

  const mayhemMode = Boolean(
    bondingCurve.isMayhemMode ??
    bondingCurve.is_mayhem_mode ??
    false
  );


  // -------------------------------------------------------------------------
  // Build Pump.fun SELL instructions.
  //
  // Spreading sellState passes the SDK-detected tokenProgram and the
  // other current sell-state accounts into the instruction builder.
  // -------------------------------------------------------------------------

  const instructions =
    await sdk.sellInstructions({
      ...sellState,

      global,

      mint,

      user,

      amount,

      solAmount:
        expectedSolBN,

      slippage:
        slippagePercent,

      mayhemMode,

      cashback: false,
    });


  if (
    !instructions ||
    !Array.isArray(
      instructions
    ) ||
    instructions.length === 0
  ) {
    throw new Error(
      "pumpfun_sell_instructions_empty"
    );
  }


  // -------------------------------------------------------------------------
  // Build transaction
  // -------------------------------------------------------------------------

  const tx = new Transaction();

  tx.feePayer = user;

  tx.add(
    ...instructions
  );


  // -------------------------------------------------------------------------
  // Initial blockhash
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
      "Pump.fun SELL Helius priority fee estimation failed; " +
      `using fallback: ${
        feeError?.message ||
        feeError
      }`
    );
  }


  // -------------------------------------------------------------------------
  // Add priority fee if the SDK hasn't already done so.
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
  // Final fresh blockhash
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
  // Return unsigned transaction to Python
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

      amount_tokens_raw:
        amount.toString(),

      wallet_token_balance_raw:
        walletBalance.toString(),

      token_program:
        tokenProgram.toBase58(),

      expected_sol_lamports:
        expectedSolBN.toString(),

      expected_sol:
        Number(
          expectedSolBN.toString()
        ) / 1_000_000_000,

      slippage_bps:
        Number(
          slippageBps ?? 300
        ),

      slippage_percent:
        slippagePercent,

      mayhem_mode:
        mayhemMode,

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
        "sell",
    }) + "\n"
  );
}


// ---------------------------------------------------------------------------
// Error handling
// ---------------------------------------------------------------------------

main().catch(
  (err) => {

    process.stdout.write(
      JSON.stringify({
        success: false,

        error:
          String(
            (err &&
              err.message) ||
            err
          ),
      })
