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
 * The Pump.fun SDK is responsible for constructing the current
 * bonding-curve account list, including current fee-recipient requirements.
 */

const {
  Connection,
  PublicKey,
  Transaction,
  ComputeBudgetProgram,
} = require("@solana/web3.js");

const pumpSdkModule = require("@pump-fun/pump-sdk");

let pumpSdkPackageVersion = "unknown";
try {
  pumpSdkPackageVersion =
    require("@pump-fun/pump-sdk/package.json").version || "unknown";
} catch (_) {
  // Some package exports block package.json access; keep unknown.
}

const {
  PumpSdk,
  OnlinePumpSdk,
  PUMP_SDK,
  getBuyTokenAmountFromSolAmount,
} = pumpSdkModule;

const BN = require("bn.js");
const bs58 = require("bs58");

const {
  TOKEN_PROGRAM_ID,
  TOKEN_2022_PROGRAM_ID,
} = require("@solana/spl-token");

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const PRIORITY_LEVEL = process.env.PUMPFUN_PRIORITY_LEVEL || "Medium";

const FALLBACK_PRIORITY_FEE_MICROLAMPORTS = 10_000;
const MAX_PRIORITY_FEE_MICROLAMPORTS = Number(process.env.PUMPFUN_PRIORITY_FEE_CAP_MICROLAMPORTS || 10_000);


// ---------------------------------------------------------------------------
// Slippage
// ---------------------------------------------------------------------------
//
// Pump SDK expects slippage as a percentage:
//
//     1   = 1%
//     3   = 3%
//     5   = 5%
//
// Our Python side supplies basis points:
//
//     300 bps = 3%
//

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


// ---------------------------------------------------------------------------
// Pump SDK compatibility
// ---------------------------------------------------------------------------
//
// Different installed versions/builds of @pump-fun/pump-sdk can expose
// their online RPC functionality differently.
//
// Some expose the RPC methods through PumpSdk.
// Newer SDK surfaces may expose OnlinePumpSdk.
//
// The previous deployment failed with:
//
//     sdk.fetchGlobal is not a function
//
// Therefore we deliberately detect the available implementation instead
// of assuming one class.
//
// ---------------------------------------------------------------------------

function createPumpSdk(
  connection
) {
  // The official SDK has two layers:
  //
  //   OnlinePumpSdk -> RPC/state fetching
  //   PUMP_SDK      -> offline instruction building
  //
  // Do NOT require one object to expose both sets of methods.

  let online;
  let onlineName;

  if (
    typeof OnlinePumpSdk ===
    "function"
  ) {
    try {
      online = new OnlinePumpSdk(
        connection
      );

      onlineName =
        "OnlinePumpSdk";

    } catch (error) {
      console.error(
        `Pump.fun OnlinePumpSdk initialization failed: ${
          error?.message || error
        }`
      );
    }
  }

  // Compatibility fallback for SDK builds that expose the online methods
  // directly through PumpSdk.
  if (
    !online &&
    typeof PumpSdk ===
    "function"
  ) {
    try {
      const candidate =
        new PumpSdk(
          connection
        );

      if (
        typeof candidate.fetchGlobal ===
          "function" &&
        typeof candidate.fetchBuyState ===
          "function"
      ) {
        online =
          candidate;

        onlineName =
          "PumpSdk-online-compatible";
      }

    } catch (error) {
      console.error(
        `Pump.fun PumpSdk online fallback initialization failed: ${
          error?.message || error
        }`
      );
    }
  }

  if (!online) {
    throw new Error(
      "pumpfun_sdk_incompatible: OnlinePumpSdk/fetchBuyState unavailable"
    );
  }

  // PUMP_SDK is the official offline instruction-builder singleton.
  // Some package builds may not export the singleton, so fall back to a
  // connection-less PumpSdk instance when available.
  let instructionSdk =
    PUMP_SDK;

  let instructionName =
    "PUMP_SDK";

  if (
    !instructionSdk &&
    typeof PumpSdk ===
      "function"
  ) {
    try {
      instructionSdk =
        new PumpSdk();

      instructionName =
        "PumpSdk-offline";

    } catch (error) {
      console.error(
        `Pump.fun offline PumpSdk initialization failed: ${
          error?.message || error
        }`
      );
    }
  }

  if (
    !instructionSdk ||
    typeof instructionSdk.buyInstructions !==
      "function"
  ) {
    throw new Error(
      "pumpfun_sdk_incompatible: offline PUMP_SDK/PumpSdk buyInstructions unavailable"
    );
  }

  if (
    typeof online.fetchGlobal !==
      "function"
  ) {
    throw new Error(
      "pumpfun_sdk_fetch_global_unavailable"
    );
  }

  if (
    typeof online.fetchBuyState !==
      "function"
  ) {
    throw new Error(
      "pumpfun_sdk_fetch_buy_state_unavailable"
    );
  }

  return {
    online,
    instructionSdk,
    onlineName,
    instructionName,
  };
}

// ---------------------------------------------------------------------------
// Resolve the token program from the actual mint account
// ---------------------------------------------------------------------------
//
// The BUY previously reached CreateIdempotent and failed with
// IncorrectProgramId. The mint account owner is authoritative, so resolve
// the token program directly from the mint before building ATA/buy
// instructions.
//

async function resolveActualTokenProgram(
  connection,
  mint
) {
  const mintAccount =
    await connection.getAccountInfo(
      mint,
      "processed"
    );

  if (!mintAccount) {
    throw new Error(
      "pumpfun_mint_account_not_found"
    );
  }

  const owner =
    mintAccount.owner;

  if (
    owner.equals(
      TOKEN_PROGRAM_ID
    )
  ) {
    return TOKEN_PROGRAM_ID;
  }

  if (
    owner.equals(
      TOKEN_2022_PROGRAM_ID
    )
  ) {
    return TOKEN_2022_PROGRAM_ID;
  }

  throw new Error(
    `pumpfun_unsupported_mint_program: ${owner.toBase58()}`
  );
}

// ---------------------------------------------------------------------------
// stdin
// ---------------------------------------------------------------------------

function readStdin() {
  return new Promise(
    (resolve) => {
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
    }
  );
}


// ---------------------------------------------------------------------------
// Helius priority fee estimation
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
            "anon-tradebot-pumpfun-priority-fee",

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

  if (
    !response.ok
  ) {
    throw new Error(
      `Helius priority fee request failed with HTTP ${response.status}`
    );
  }

  const body =
    await response.json();

  if (
    body.error
  ) {
    throw new Error(
      `Helius priority fee RPC error: ${JSON.stringify(
        body.error
      )}`
    );
  }

  const estimate =
    body?.result
      ?.priorityFeeEstimate;

  if (
    estimate ===
      undefined ||
    estimate === null ||
    !Number.isFinite(
      Number(estimate)
    ) ||
    Number(estimate) < 0
  ) {
    throw new Error(
      `Invalid Helius priority fee estimate: ${JSON.stringify(
        body
      )}`
    );
  }

  return Math.min(
    Math.ceil(Number(estimate)),
    Math.max(0, MAX_PRIORITY_FEE_MICROLAMPORTS)
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

      return (
        instruction.data[0] === 3
      );
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
  const parsed =
    Number(value);

  if (
    !Number.isSafeInteger(
      parsed
    ) ||
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
  const raw =
    await readStdin();

  let input;

  try {
    input =
      JSON.parse(raw);

  } catch (error) {
    throw new Error(
      `invalid_json_input: ${
        error?.message || error
      }`
    );
  }

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

  if (
    action !== "buy"
  ) {
    throw new Error(
      "pumpfun_builder_only_supports_buy"
    );
  }


  const lamports =
    requirePositiveInteger(
      amountLamports,
      "amount_lamports"
    );


  // -------------------------------------------------------------------------
  // Validate public keys
  // -------------------------------------------------------------------------

  let mint;
  let user;

  try {
    mint =
      new PublicKey(
        baseMint
      );

    user =
      new PublicKey(
        ownerPubkey
      );

  } catch (error) {

    throw new Error(
      `invalid_public_key: ${
        error?.message || error
      }`
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
  //
  // This is the critical compatibility fix.
  //
  // We no longer blindly execute:
  //
  //     new PumpSdk(connection)
  //
  // followed by:
  //
  //     sdk.fetchGlobal()
  //
  // Instead, we verify the installed implementation actually exposes the
  // online methods before using it.
  //

  const sdkInfo =
    createPumpSdk(
      connection
    );

  const online =
    sdkInfo.online;

  const instructionSdk =
    sdkInfo.instructionSdk;


  console.error(
    `Pump.fun SDK selected: ${sdkInfo.onlineName}; ` +
    `instruction=${sdkInfo.instructionName || "unknown"}; ` +
    `version=${pumpSdkPackageVersion}`
  );


  // -------------------------------------------------------------------------
  // Fetch current Pump.fun global state
  // -------------------------------------------------------------------------

  if (
    typeof online.fetchGlobal !==
    "function"
  ) {
    throw new Error(
      "pumpfun_sdk_fetch_global_unavailable"
    );
  }

  let global;
  try {
    global = await online.fetchGlobal();
  } catch (error) {
    throw new Error(
      `pumpfun_stage_fetchGlobal_failed: ${
        error?.message || error
      }`
    );
  }


  if (!global) {
    throw new Error(
      "pumpfun_global_state_not_found"
    );
  }


  // -------------------------------------------------------------------------
  // Fetch current Pump.fun fee configuration
  // -------------------------------------------------------------------------

  let feeConfig =
    null;

  if (
    typeof online.fetchFeeConfig ===
    "function"
  ) {
    try {

      feeConfig =
        await online.fetchFeeConfig();

    } catch (error) {

      console.error(
        "Pump.fun fee config unavailable; " +
        `continuing without it: ${
          error?.message || error
        }`
      );
    }
  }


  // -------------------------------------------------------------------------
  // Fetch live bonding-curve state
  // -------------------------------------------------------------------------

  if (
    typeof online.fetchBuyState !==
    "function"
  ) {
    throw new Error(
      "pumpfun_sdk_fetch_buy_state_unavailable"
    );
  }

  const actualTokenProgram =
  await resolveActualTokenProgram(
    connection,
    mint
  );

console.error(
  `Pump.fun mint token program: ${actualTokenProgram.toBase58()}`
);

const buyState =
  await online.fetchBuyState(
    mint,
    user,
    actualTokenProgram
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
  tokenProgram: sdkTokenProgram,
} = buyState;

const tokenProgram =
  actualTokenProgram;

if (
  sdkTokenProgram &&
  !sdkTokenProgram.equals(
    actualTokenProgram
  )
) {
  console.error(
    "Pump.fun token program mismatch; " +
    `SDK=${sdkTokenProgram.toBase58()} ` +
    `mintOwner=${actualTokenProgram.toBase58()}. ` +
    "Using mint owner."
  );
}


  if (
    !bondingCurveAccountInfo
  ) {
    throw new Error(
      "pumpfun_bonding_curve_account_info_missing"
    );
  }

  if (
    !bondingCurve
  ) {
    throw new Error(
      "pumpfun_bonding_curve_state_missing"
    );
  }


  // -------------------------------------------------------------------------
  // Do not buy completed/migrated curves
  // -------------------------------------------------------------------------

  if (
    bondingCurve.complete ===
    true
  ) {
    throw new Error(
      "pumpfun_bonding_curve_already_complete"
    );
  }


  // -------------------------------------------------------------------------
  // SOL amount
  // -------------------------------------------------------------------------

  const solAmount =
    new BN(
      String(lamports)
    );


  // -------------------------------------------------------------------------
  // Calculate expected token amount
  // -------------------------------------------------------------------------

  if (
    typeof getBuyTokenAmountFromSolAmount !==
    "function"
  ) {
    throw new Error(
      "pumpfun_sdk_buy_quote_function_unavailable"
    );
  }

  let tokenAmount;


  // Current fee-aware API
  if (
    feeConfig
  ) {

    try {

      tokenAmount =
        getBuyTokenAmountFromSolAmount({
          global,

          feeConfig,

          mintSupply:
            bondingCurve
              .tokenTotalSupply,

          bondingCurve,

          amount:
            solAmount,
        });

    } catch (firstError) {

      // Compatibility fallback for older SDK signatures.

      try {

        tokenAmount =
          getBuyTokenAmountFromSolAmount(
            global,
            bondingCurve,
            solAmount
          );

      } catch (secondError) {

        throw new Error(
          "pumpfun_buy_quote_failed: " +
          `${
            secondError?.message ||
            firstError
          }`
        );
      }
    }

  } else {

    // Older SDK / no fee config.

    try {

      tokenAmount =
        getBuyTokenAmountFromSolAmount(
          global,
          bondingCurve,
          solAmount
        );

    } catch (firstError) {

      try {

        tokenAmount =
          getBuyTokenAmountFromSolAmount({
            global,

            bondingCurve,

            amount:
              solAmount,
          });

      } catch (secondError) {

        throw new Error(
          "pumpfun_buy_quote_failed: " +
          `${
            secondError?.message ||
            firstError
          }`
        );
      }
    }
  }


  if (
    !tokenAmount
  ) {
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
  // -------------------------------------------------------------------------
  //
  // IMPORTANT:
  //
  // If fetchBuyState returned tokenProgram, use it.
  // This allows the SDK to correctly handle Token / Token-2022.
  //
  // We also pass the state returned by fetchBuyState rather than manually
  // reconstructing the state.
  //

  if (
    typeof instructionSdk.buyInstructions !==
    "function"
  ) {
    throw new Error(
      "pumpfun_sdk_buy_instructions_unavailable"
    );
  }


  const instructionParams = {
    global,

    bondingCurveAccountInfo,

    bondingCurve,

    associatedUserAccountInfo,

    mint,

    user,

    amount:
      tokenAmountBN,

    solAmount,

    slippage:
      slippagePercent,
  };


  // Never fall back to TOKEN_PROGRAM_ID here.
// The program was verified directly from the mint account.
instructionParams.tokenProgram =
  actualTokenProgram;

  let instructions;

  try {

    instructions =
      await instructionSdk.buyInstructions(
        instructionParams
      );

  } catch (error) {

    throw new Error(
      "pumpfun_buy_instructions_failed: " +
      `${
        error?.message ||
        error
      }`
    );
  }


  if (
    !instructions ||
    !Array.isArray(
      instructions
    ) ||
    instructions.length === 0
  ) {
    throw new Error(
      "pumpfun_buy_instructions_empty"
    );
  }


  // -------------------------------------------------------------------------
  // Build unsigned transaction
  // -------------------------------------------------------------------------

  const tx =
    new Transaction();

  tx.feePayer =
    user;

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
      "Pump.fun Helius priority fee estimation failed; " +
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
      ComputeBudgetProgram
        .setComputeUnitPrice({
          microLamports:
            priorityFeeMicroLamports,
        })
    );

    priorityFeeInstructionAdded =
      true;
  }


  // -------------------------------------------------------------------------
  // Refresh blockhash AFTER all instructions
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
      requireAllSignatures:
        false,

      verifySignatures:
        false,
    });


  // -------------------------------------------------------------------------
  // Return to Python
  // -------------------------------------------------------------------------

  process.stdout.write(
    JSON.stringify({
      success:
        true,

      transaction_b64:
        serialized.toString(
          "base64"
        ),

      blockhash:
        finalBlockhash.blockhash,

      last_valid_block_height:
        finalBlockhash
          .lastValidBlockHeight,

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
          slippageBps ||
          300
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
          ? bondingCurveAccountInfo
              .pubkey
              .toBase58()
          : null,

      token_program:
        tokenProgram
          ? tokenProgram.toBase58
            ? tokenProgram.toBase58()
            : String(
                tokenProgram
              )
          : TOKEN_PROGRAM_ID.toBase58(),

      pump_sdk_class:
        sdkInfo.onlineName,

      pump_sdk_instruction_class:
        sdkInfo.instructionName,

      action:
        "buy",

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
        success:
          false,

        error:
          String(
            (
              err &&
              err.message
            ) ||
            err
          ),
      }) + "\n"
    );

    // Python wrapper treats the JSON response as authoritative.
    process.exit(0);
  }
);
