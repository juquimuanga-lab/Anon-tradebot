/**
 * Pump.fun unsigned SELL transaction builder.
 *
 * Python:
 *   - supplies the wallet public key
 *   - supplies the mint
 *   - supplies the exact raw token amount
 *   - signs the returned transaction
 *   - broadcasts/confirm it
 *
 * This file:
 *   - never receives a private key
 *   - never signs
 *   - never broadcasts
 *
 * The official Pump.fun SDK constructs the current bonding-curve sell
 * instruction set, including current fee-recipient accounts.
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

const {
  TOKEN_PROGRAM_ID,
  getMint,
} = require("@solana/spl-token");


// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const PRIORITY_LEVEL = "High";

const FALLBACK_PRIORITY_FEE_MICROLAMPORTS = 10_000;


// ---------------------------------------------------------------------------
// stdin
// ---------------------------------------------------------------------------

function readStdin() {
  return new Promise((resolve) => {
    let data = "";

    process.stdin.on(
      "data",
      (chunk) => {
        data += chunk;
      }
    );

    process.stdin.on(
      "end",
      () => {
        resolve(data);
      }
    );
  });
}


// ---------------------------------------------------------------------------
// Helpers
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


function slippageBpsToPercent(
  slippageBps
) {
  const bps = Number(
    slippageBps
  );

  if (
    !Number.isFinite(bps) ||
    bps < 0
  ) {
    return 3;
  }

  return bps / 100;
}


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

      return instruction.data[0] === 3;
    }
  );
}


// ---------------------------------------------------------------------------
// Helius priority fee
// ---------------------------------------------------------------------------

async function getPriorityFeeEstimate(
  connection,
  transaction
) {
  const serialized =
    transaction.serialize({
      requireAllSignatures: false,
      verifySignatures: false,
    });

  const serializedBase58 =
    bs58.encode(
      serialized
    );

  const response =
    await fetch(
      connection.rpcEndpoint,
      {
        method: "POST",

        headers: {
          "Content-Type":
            "application/json",
        },

        body: JSON.stringify({
          jsonrpc: "2.0",

          id:
            "anon-tradebot-pumpfun-sell-priority-fee",

          method:
            "getPriorityFeeEstimate",

          params: [
            {
              transaction:
                serializedBase58,

              options: {
                priorityLevel:
                  PRIORITY_LEVEL,

                recommended:
                  true,
              },
            },
          ],
        }),
      }
    );

  if (!response.ok) {
    throw new Error(
      "Helius priority fee request failed " +
      `with HTTP ${response.status}`
    );
  }

  const body =
    await response.json();

  if (body.error) {
    throw new Error(
      "Helius priority fee RPC error: " +
      JSON.stringify(
        body.error
      )
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
  const raw =
    await readStdin();

  const input =
    JSON.parse(raw);

  const {
    action,
    baseMint,
    ownerPubkey,
    amountTokensRaw,
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
  // Public keys
  // -------------------------------------------------------------------------

  const mint =
    new PublicKey(
      baseMint
    );

  const user =
    new PublicKey(
      ownerPubkey
    );


  // -------------------------------------------------------------------------
  // Exact raw token amount
  //
  // This is intentionally passed as a decimal string to BN.
  //
  // Never use JS floating point for the raw SPL token amount.
  // -------------------------------------------------------------------------

  const amount =
    new BN(
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
  // Connection
  // -------------------------------------------------------------------------

  const connection =
    new Connection(
      rpcUrl,
      {
        commitment:
          "processed",
      }
    );


  // -------------------------------------------------------------------------
  // Pump SDK
  // -------------------------------------------------------------------------

  const sdk =
    new PumpSdk(
      connection
    );


  // -------------------------------------------------------------------------
  // Global state
  // -------------------------------------------------------------------------

  const global =
    await sdk.fetchGlobal();

  if (!global) {
    throw new Error(
      "pumpfun_global_state_not_found"
    );
  }


  // -------------------------------------------------------------------------
  // Fee configuration
  // -------------------------------------------------------------------------

  let feeConfig =
    null;

  try {

    feeConfig =
      await sdk.fetchFeeConfig();

  } catch (error) {

    // Keep this non-fatal because the instruction builder itself can
    // obtain what it needs from the SDK state.
    console.error(
      "Pump.fun fee config unavailable: " +
      `${
        error?.message ||
        error
      }`
    );
  }


  // -------------------------------------------------------------------------
  // Fetch live SELL state
  //
  // This is intentionally fetchSellState(), not fetchBuyState().
  // The SDK can therefore resolve the exact account state required for
  // selling this wallet's token position.
  // -------------------------------------------------------------------------

  const sellState =
    await sdk.fetchSellState(
      mint,
      user
    );

  if (!sellState) {
    throw new Error(
      "pumpfun_sell_state_not_found"
    );
  }


  const {
    bondingCurveAccountInfo,
    bondingCurve,
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


  // -------------------------------------------------------------------------
  // Selling is only valid while the token is on the Pump.fun bonding curve.
  //
  // Once complete/migrated, the position must be handled by the graduated
  // market executor rather than this bonding-curve sell path.
  // -------------------------------------------------------------------------

  if (
    bondingCurve.complete === true
  ) {
    throw new Error(
      "pumpfun_bonding_curve_completed_sell_requires_graduated_executor"
    );
  }


  // -------------------------------------------------------------------------
  // Verify token decimals.
  //
  // This is mainly defensive validation. The actual sell amount is already
  // supplied as raw units.
  // -------------------------------------------------------------------------

  const mintInfo =
    await getMint(
      connection,
      mint,
      "processed",
      TOKEN_PROGRAM_ID
    );

  const decimals =
    Number(
      mintInfo.decimals
    );


  // -------------------------------------------------------------------------
  // Make sure the requested sell amount doesn't exceed the wallet's
  // current token balance.
  //
  // The SDK exposes getTokenBalance(), which reads the associated token
  // balance using the current wallet.
  // -------------------------------------------------------------------------

  const walletBalance =
    await sdk.getTokenBalance(
      mint,
      user,
      TOKEN_PROGRAM_ID
    );

  if (!walletBalance) {
    throw new Error(
      "pumpfun_token_balance_unavailable"
    );
  }

  if (
    amount.gt(
      new BN(
        walletBalance.toString()
      )
    )
  ) {
    throw new Error(
      "pumpfun_sell_amount_exceeds_wallet_balance"
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


  // -------------------------------------------------------------------------
  // Calculate expected SOL output.
  //
  // This gives the SDK the minimum SOL amount after applying slippage.
  // -------------------------------------------------------------------------

  let expectedSolAmount;

  try {

    if (feeConfig) {

      expectedSolAmount =
        getSellSolAmountFromTokenAmount({
          global,
          feeConfig,
          mintSupply:
            bondingCurve.tokenTotalSupply,
          bondingCurve,
          amount,
        });

    } else {

      expectedSolAmount =
        getSellSolAmountFromTokenAmount(
          global,
          bondingCurve,
          amount
        );
    }

  } catch (firstError) {

    try {

      expectedSolAmount =
        getSellSolAmountFromTokenAmount({
          global,
          bondingCurve,
          amount,
        });

    } catch (secondError) {

      throw new Error(
        "pumpfun_sell_quote_failed: " +
        `${
          secondError?.message ||
          firstError
        }`
      );
    }
  }


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
  //
  // Newer Pump.fun curves can expose this state on the bonding curve.
  // Default false when the field is unavailable.
  // -------------------------------------------------------------------------

  const mayhemMode =
    Boolean(
      bondingCurve.isMayhemMode ??
      bondingCurve.is_mayhem_mode ??
      false
    );


  // -------------------------------------------------------------------------
  // Build SELL instructions
  // -------------------------------------------------------------------------

  const instructions =
    await sdk.sellInstructions({
      global,

      bondingCurveAccountInfo,

      bondingCurve,

      mint,

      user,

      // Exact raw token amount.
      amount,

      // Minimum SOL expected after slippage.
      solAmount:
        expectedSolBN,

      slippage:
        slippagePercent,

      tokenProgram:
        TOKEN_PROGRAM_ID,

      mayhemMode,

      cashback:
        false,
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
  // Transaction
  // -------------------------------------------------------------------------

  const tx =
    new Transaction();

  tx.feePayer =
    user;

  tx.add(
    ...instructions
  );


  // -------------------------------------------------------------------------
  // Blockhash
  // -------------------------------------------------------------------------

  const initialBlockhash =
    await connection.getLatestBlockhash(
      "processed"
    );

  tx.recentBlockhash =
    initialBlockhash.blockhash;


  // -------------------------------------------------------------------------
  // Priority fee
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
  // Final blockhash AFTER all instructions
  // -------------------------------------------------------------------------

  const finalBlockhash =
    await connection.getLatestBlockhash(
      "processed"
    );

  tx.recentBlockhash =
    finalBlockhash.blockhash;


  // -------------------------------------------------------------------------
  // Serialize
  // -------------------------------------------------------------------------

  const serialized =
    tx.serialize({
      requireAllSignatures:
        false,

      verifySignatures:
        false,
    });


  // -------------------------------------------------------------------------
  // Return unsigned transaction
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

      token_decimals:
        decimals,

      expected_sol_lamports:
        expectedSolBN.toString(),

      expected_sol:
        Number(
          expectedSolBN.toString()
        ) / 1_000_000_000,

      slippage_bps:
        Number(
          slippageBps || 300
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
      }) + "\n"
    );

    process.exit(0);
  }
);
