/**
 * Pump.fun unsigned SELL transaction builder.
 *
 * Architecture:
 *
 *     Python
 *        ↓
 *     this file
 *        ↓
 *     OnlinePumpSdk
 *        ↓
 *     current Pump.fun bonding-curve state
 *        ↓
 *     PumpSdk sell instructions
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
 * - This file never broadcasts a transaction.
 * - ownerPubkey is only used as the transaction payer/user.
 *
 * Pump.fun bonding-curve sells only.
 * Graduated tokens must be routed through the AMM executor.
 */

const {
  Connection,
  PublicKey,
  Transaction,
  ComputeBudgetProgram,
} = require("@solana/web3.js");

const {
  PumpSdk,
  OnlinePumpSdk,
  getSellSolAmountFromTokenAmount,
} = require("@pump-fun/pump-sdk");

const {
  getAssociatedTokenAddressSync,
  createAssociatedTokenAccountIdempotentInstruction,
  createTransferInstruction,
  TOKEN_PROGRAM_ID,
  TOKEN_2022_PROGRAM_ID,
  ASSOCIATED_TOKEN_PROGRAM_ID,
} = require("@solana/spl-token");

const BN = require("bn.js");
const bs58 = require("bs58");


// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const PRIORITY_LEVEL = "High";

const FALLBACK_PRIORITY_FEE_MICROLAMPORTS = 10_000;


// ---------------------------------------------------------------------------
// Slippage
// ---------------------------------------------------------------------------
//
// Python supplies basis points.
//
//     100 bps = 1%
//     300 bps = 3%
//     500 bps = 5%
//
// Pump SDK expects percentage:
//
//     1 = 1%
//     3 = 3%
//     5 = 5%
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
// stdin
// ---------------------------------------------------------------------------

function readStdin() {
  return new Promise(
    (resolve, reject) => {
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

      process.stdin.on(
        "error",
        reject
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
      requireAllSignatures:
        false,

      verifySignatures:
        false,
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
      "Helius priority fee RPC error: " +
      JSON.stringify(
        body.error
      )
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
      "Invalid Helius priority fee estimate: " +
      JSON.stringify(body)
    );
  }

  return Math.ceil(
    Number(estimate)
  );
}


// ---------------------------------------------------------------------------
// Compute Budget detection
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

      // Compute Budget:
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
// Positive integer validation
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
// Direct wallet token balance
// ---------------------------------------------------------------------------
//
// The installed Pump.fun SDK does not reliably expose getTokenBalance().
// Read the wallet's parsed token accounts directly from Solana RPC.
//
// This supports normal SPL Token and Token-2022 accounts.
// ---------------------------------------------------------------------------

async function getWalletTokenBalance(
  connection,
  user,
  mint
) {
  const response =
    await connection.getParsedTokenAccountsByOwner(
      user,
      {
        mint,
      },
      "processed"
    );

  let totalBalance =
    new BN(0);

  for (
    const account of response.value
  ) {
    const tokenAmount =
      account?.account?.data?.parsed
        ?.info?.tokenAmount?.amount;

    if (
      tokenAmount === undefined ||
      tokenAmount === null
    ) {
      continue;
    }

    totalBalance =
      totalBalance.add(
        new BN(
          String(tokenAmount)
        )
      );
  }

  return totalBalance;
}

async function prepareCanonicalSellAccount(
  connection,
  user,
  mint,
  tokenProgram,
  amount
) {
  const associatedTokenAddress =
    getAssociatedTokenAddressSync(
      mint,
      user,
      false,
      tokenProgram,
      ASSOCIATED_TOKEN_PROGRAM_ID
    );

  const associatedAccountInfo =
    await connection.getAccountInfo(
      associatedTokenAddress,
      "processed"
    );

  if (associatedAccountInfo) {
    return {
      associatedTokenAddress,
      setupInstructions: [],
      ataCreated: false,
      tokensMovedToAta: new BN(0),
    };
  }

  const accounts =
    await connection.getParsedTokenAccountsByOwner(
      user,
      { mint },
      "processed"
    );

  let remaining =
    new BN(amount.toString());

  const sourceAccounts = [];

  for (const account of accounts.value) {
    const info =
      account?.account?.data?.parsed?.info;

    const sourceOwner = info?.owner;
    const rawAmount =
      info?.tokenAmount?.amount;

    if (!rawAmount) {
      continue;
    }

    if (
      sourceOwner &&
      sourceOwner !== user.toBase58()
    ) {
      continue;
    }

    const source =
      account.pubkey;

    if (
      source.equals(
        associatedTokenAddress
      )
    ) {
      continue;
    }

    const balance =
      new BN(String(rawAmount));

    if (
      balance.lte(
        new BN(0)
      )
    ) {
      continue;
    }

    sourceAccounts.push({
      source,
      balance,
    });

    if (
      balance.gte(
        remaining
      )
    ) {
      break;
    }

    remaining =
      remaining.sub(
        balance
      );
  }

  if (
    remaining.gt(
      new BN(0)
    )
  ) {
    throw new Error(
      "pumpfun_canonical_ata_missing_and_source_token_account_insufficient"
    );
  }

  const setupInstructions = [];

  setupInstructions.push(
    createAssociatedTokenAccountIdempotentInstruction(
      user,
      associatedTokenAddress,
      user,
      mint,
      tokenProgram,
      ASSOCIATED_TOKEN_PROGRAM_ID
    )
  );

  let transferRemaining =
    new BN(amount.toString());

  for (
    const sourceAccount of
      sourceAccounts
  ) {
    if (
      transferRemaining.lte(
        new BN(0)
      )
    ) {
      break;
    }

    const transferAmount =
      sourceAccount.balance.gte(
        transferRemaining
      )
        ? transferRemaining
        : sourceAccount.balance;

    setupInstructions.push(
      createTransferInstruction(
        sourceAccount.source,
        associatedTokenAddress,
        user,
        BigInt(
          transferAmount.toString()
        ),
        [],
        tokenProgram
      )
    );

    transferRemaining =
      transferRemaining.sub(
        transferAmount
      );
  }

  if (
    transferRemaining.gt(
      new BN(0)
    )
  ) {
    throw new Error(
      "pumpfun_failed_to_prepare_tokens_for_canonical_ata"
    );
  }

  return {
    associatedTokenAddress,
    setupInstructions,
    ataCreated: true,
    tokensMovedToAta:
      new BN(
        amount.toString()
      ),
  };
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {

  const raw =
    await readStdin();

  if (
    !raw.trim()
  ) {
    throw new Error(
      "empty_stdin_payload"
    );
  }

  let input;

  try {

    input =
      JSON.parse(raw);

  } catch (error) {

    throw new Error(
      `invalid_json_input: ${
        error?.message ||
        error
      }`
    );
  }


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

  if (
    !rpcUrl
  ) {
    throw new Error(
      "rpc_url_missing"
    );
  }

  if (
    !baseMint
  ) {
    throw new Error(
      "base_mint_missing"
    );
  }

  if (
    !ownerPubkey
  ) {
    throw new Error(
      "owner_pubkey_missing"
    );
  }

  if (
    action !== "sell"
  ) {
    throw new Error(
      "pumpfun_sell_builder_requires_sell_action"
    );
  }

  if (
    amountTokensRaw ===
      undefined ||
    amountTokensRaw ===
      null
  ) {
    throw new Error(
      "amount_tokens_raw_missing"
    );
  }


  // -------------------------------------------------------------------------
  // Raw token amount
  // -------------------------------------------------------------------------
  //
  // NEVER use JavaScript floating point for SPL token base units.
  //

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
  // Public keys
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
        error?.message ||
        error
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
  // Online Pump SDK
  // -------------------------------------------------------------------------
  //
  // IMPORTANT:
  //
  // OnlinePumpSdk is used for RPC-backed state fetching.
  //
  // It provides:
  //
  //     fetchGlobal()
  //     fetchFeeConfig()
  //     fetchSellState()
  //     getTokenBalance()
  //
  // The instruction builder itself remains PumpSdk-compatible.
  //

  if (
    typeof OnlinePumpSdk !==
    "function"
  ) {
    throw new Error(
      "pumpfun_online_sdk_unavailable"
    );
  }

  if (
    typeof PumpSdk !==
    "function"
  ) {
    throw new Error(
      "pumpfun_sdk_unavailable"
    );
  }

  const onlineSdk =
    new OnlinePumpSdk(
      connection
    );


  // -------------------------------------------------------------------------
  // Verify required online methods
  // -------------------------------------------------------------------------

  const requiredOnlineMethods = [
  "fetchGlobal",
  "fetchFeeConfig",
  "fetchSellState",
];

  for (
    const method of
      requiredOnlineMethods
  ) {

    if (
      typeof onlineSdk[method] !==
      "function"
    ) {
      throw new Error(
        `pumpfun_online_sdk_missing_method:${method}`
      );
    }
  }


  // -------------------------------------------------------------------------
  // Fetch all required state
  // -------------------------------------------------------------------------
  //
  // Do these in parallel to reduce latency.
  //

  const [
    global,
    feeConfig,
    sellState,
  ] = await Promise.all([
    onlineSdk.fetchGlobal(),

    onlineSdk.fetchFeeConfig(),

    onlineSdk.fetchSellState(
      mint,
      user
    ),
  ]);


  if (
    !global
  ) {
    throw new Error(
      "pumpfun_global_state_not_found"
    );
  }

  if (
    !feeConfig
  ) {
    throw new Error(
      "pumpfun_fee_config_not_found"
    );
  }

  if (
    !sellState
  ) {
    throw new Error(
      "pumpfun_sell_state_not_found"
    );
  }


  const {
    bondingCurveAccountInfo,
    bondingCurve,
    tokenProgram,
  } = sellState;


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

  if (
    !tokenProgram
  ) {
    throw new Error(
      "pumpfun_token_program_missing_from_sell_state"
    );
  }


  // -------------------------------------------------------------------------
  // Bonding curve completion check
  // -------------------------------------------------------------------------
  //
  // A completed bonding curve has migrated to the AMM.
  //
  // Do NOT attempt to sell it through the bonding-curve instruction.
  //

  if (
    bondingCurve.complete ===
    true
  ) {
    throw new Error(
      "pumpfun_bonding_curve_completed_sell_requires_graduated_executor"
    );
  }


  // -------------------------------------------------------------------------
  // Wallet token balance
  // -------------------------------------------------------------------------

  const walletBalance =
  await getWalletTokenBalance(
    connection,
    user,
    mint
  );


  if (
    walletBalance ===
      undefined ||
    walletBalance ===
      null
  ) {
    throw new Error(
      "pumpfun_token_balance_unavailable"
    );
  }


  const walletBalanceBN =
    new BN(
      walletBalance.toString()
    );


  if (
    walletBalanceBN.lte(
      new BN(0)
    )
  ) {
    throw new Error(
      "pumpfun_wallet_token_balance_zero"
    );
  }


  // -------------------------------------------------------------------------
  // Never attempt to sell more than the wallet owns
  // -------------------------------------------------------------------------

  if (
    amount.gt(
      walletBalanceBN
    )
  ) {
    throw new Error(
      "pumpfun_sell_amount_exceeds_wallet_balance"
    );
  }


  // -------------------------------------------------------------------------
  // Calculate expected SOL received
  // -------------------------------------------------------------------------
  //
  // Current Pump SDK requires:
  //
  //     global
  //     feeConfig
  //     mintSupply
  //     bondingCurve
  //     amount
  //
  // This keeps the quote aligned with current Pump.fun fee configuration.
  //

  if (
    typeof getSellSolAmountFromTokenAmount !==
    "function"
  ) {
    throw new Error(
      "pumpfun_sell_quote_function_unavailable"
    );
  }


  let expectedSolAmount;

  try {

    expectedSolAmount =
      getSellSolAmountFromTokenAmount({
        global,

        feeConfig,

        mintSupply:
          bondingCurve
            .tokenTotalSupply,

        bondingCurve,

        amount,
      });

  } catch (error) {

    throw new Error(
      "pumpfun_sell_quote_failed: " +
      `${
        error?.message ||
        error
      }`
    );
  }


  if (
    !expectedSolAmount
  ) {
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

  const mayhemMode =
    Boolean(
      bondingCurve.isMayhemMode ??
      bondingCurve.is_mayhem_mode ??
      false
    );


  // -------------------------------------------------------------------------
  // Build sell instructions
  // -------------------------------------------------------------------------
  //
  // Use the online state returned by fetchSellState.
  //
  // This is important because it contains the current token program and
  // bonding curve account information.
  //

  const sellInstructionParams = {
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

    cashback:
      false,
  };


  const offlineSdk =
  new PumpSdk();

const sellAccountPreparation =
  await prepareCanonicalSellAccount(
    connection,
    user,
    mint,
    tokenProgram,
    amount
  );

let instructions;

try {
  instructions =
    await offlineSdk.sellInstructions(
      sellInstructionParams
    );

} catch (error) {
  throw new Error(
    "pumpfun_sell_instructions_failed: " +
    `${
      error?.message ||
      error
    }`
  );
}

if (
  sellAccountPreparation
    .setupInstructions.length >
  0
) {
  instructions = [
    ...sellAccountPreparation
      .setupInstructions,
    ...instructions,
  ];
}

  } catch (error) {

    throw new Error(
      "pumpfun_sell_instructions_failed: " +
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
    instructions.length ===
      0
  ) {
    throw new Error(
      "pumpfun_sell_instructions_empty"
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
      "Pump.fun SELL Helius priority fee estimation failed; " +
      `using fallback: ${
        feeError?.message ||
        feeError
      }`
    );
  }


  // -------------------------------------------------------------------------
  // Add priority fee if not already present
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
      requireAllSignatures:
        false,

      verifySignatures:
        false,
    });


  // -------------------------------------------------------------------------
  // Return transaction to Python
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

      amount_tokens_raw:
        amount.toString(),

      wallet_token_balance_raw:
        walletBalanceBN.toString(),

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
          slippageBps ??
          300
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
        bondingCurveAccountInfo
          .pubkey
          ? bondingCurveAccountInfo
              .pubkey
              .toBase58()
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

    // The Python wrapper treats the JSON response as the authoritative
    // result, so return exit code 0 with success=false.
    process.exit(0);
  }
);
