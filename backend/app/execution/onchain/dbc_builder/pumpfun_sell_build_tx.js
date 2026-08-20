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
 *
 * V7:
 * - Keeps the complete V6 transaction-building architecture intact.
 * - Uses the SDK's `cashback` flag from the actual bonding-curve state instead
 *   of hard-coding `cashback: false`.
 * - When the coin is cashback-enabled, the SDK receives `cashback: true`, so
 *   it places the Pump UserVolumeAccumulator in the correct remaining-account
 *   position before the trailing fee-recipient account.
 * - Initializes the user's Pump UserVolumeAccumulator first when required.
 * - Does NOT manually reorder or append the accumulator to the Sell instruction.
 *
 * This is intentionally a surgical V7 based on the full V6 file, rather than
 * a shortened rewrite, so the Python JSON contract, ATA recovery, Token-2022
 * handling, priority-fee handling, and error reporting remain unchanged.
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
  bondingCurvePda,
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

const PRIORITY_LEVEL = process.env.PUMPFUN_PRIORITY_LEVEL || "Medium";

const FALLBACK_PRIORITY_FEE_MICROLAMPORTS = 10_000;
const MAX_PRIORITY_FEE_MICROLAMPORTS = Number(process.env.PUMPFUN_PRIORITY_FEE_CAP_MICROLAMPORTS || 10_000);


// Pump.fun main bonding-curve program.
const PUMP_PROGRAM_ID = new PublicKey(
  "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
);


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

  return Math.min(
    Math.ceil(Number(estimate)),
    Math.max(0, MAX_PRIORITY_FEE_MICROLAMPORTS)
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


// ---------------------------------------------------------------------------
// Resolve the token program from the actual mint account
// ---------------------------------------------------------------------------
//
// fetchSellState() defaults to classic TOKEN_PROGRAM_ID when no
// tokenProgram is passed in, and pump.fun's current token creation path
// (createV2) mints under TOKEN_2022_PROGRAM_ID. Relying on that default
// derives the associated token account at the wrong address for any
// Token-2022 mint.
//
// The mint account's owner is authoritative, so resolve the token program
// directly from the mint before asking the SDK for sell state.
// ---------------------------------------------------------------------------

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
// FIX: Prepare Pump.fun bonding curve ATA
// ---------------------------------------------------------------------------
//
// Pump.fun's Sell instruction expects the bonding curve's associated token
// account to already be initialized.
//
// If that ATA does not exist, the Sell instruction fails:
//
//     AccountNotInitialized
//     Error Number: 3012
//     account: associatedbondingcurve
//
// We therefore derive the ATA from the ACTUAL bonding curve PDA and mint,
// check whether it exists, and create it idempotently before the Sell
// instruction when necessary.
// ---------------------------------------------------------------------------

async function prepareBondingCurveAta(
  connection,
  payer,
  bondingCurvePubkey,
  mint,
  tokenProgram
) {
  const associatedBondingCurve =
    getAssociatedTokenAddressSync(
      mint,
      bondingCurvePubkey,
      true,
      tokenProgram,
      ASSOCIATED_TOKEN_PROGRAM_ID
    );

  const accountInfo =
    await connection.getAccountInfo(
      associatedBondingCurve,
      "processed"
    );

  if (accountInfo) {
    return {
      associatedBondingCurve,
      setupInstructions: [],
      ataCreated: false,
    };
  }

  const createInstruction =
    createAssociatedTokenAccountIdempotentInstruction(
      payer,
      associatedBondingCurve,
      bondingCurvePubkey,
      mint,
      tokenProgram,
      ASSOCIATED_TOKEN_PROGRAM_ID
    );

  return {
    associatedBondingCurve,
    setupInstructions: [
      createInstruction,
    ],
    ataCreated: true,
  };
}


// ---------------------------------------------------------------------------
// Prepare canonical user token ATA
// ---------------------------------------------------------------------------

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
    new BN(
      amount.toString()
    );

  const sourceAccounts = [];

  for (
    const account of accounts.value
  ) {
    const info =
      account?.account?.data?.parsed?.info;

    const sourceOwner =
      info?.owner;

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
      new BN(
        String(rawAmount)
      );

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
    new BN(
      amount.toString()
    );

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

  let amount =
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
  // Resolve actual token program BEFORE fetching sell state.
  // -------------------------------------------------------------------------

  const actualTokenProgram =
    await resolveActualTokenProgram(
      connection,
      mint
    );

  console.error(
    `Pump.fun mint token program: ${actualTokenProgram.toBase58()}`
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

  const [
    global,
    feeConfig,
    sellState,
  ] = await Promise.all([
    onlineSdk.fetchGlobal(),

    onlineSdk.fetchFeeConfig(),

    onlineSdk.fetchSellState(
      mint,
      user,
      actualTokenProgram
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
    tokenProgram: sdkTokenProgram,
  } = sellState;

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
  // Bonding curve completion check
  // -------------------------------------------------------------------------

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
  // Never attempt to sell more than wallet owns
  // -------------------------------------------------------------------------

  let amountClamped = false;

  if (
    amount.gt(
      walletBalanceBN
    )
  ) {
    amount =
      walletBalanceBN;

    amountClamped =
      true;
  }


  // -------------------------------------------------------------------------
  // Calculate expected SOL received
  // -------------------------------------------------------------------------

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

  const sellInstructionParams = {
    ...sellState,

    // Always force the token program resolved from the mint.
    // This prevents the SDK from deriving/checking the wrong ATA.
    tokenProgram,

    global,

    mint,

    user,

    amount,

    solAmount:
      expectedSolBN,

    slippage:
      slippagePercent,

    mayhemMode,

    // IMPORTANT V7 FIX:
    // Pump.fun sellInstructions() uses this flag to place the Pump
    // UserVolumeAccumulator in the correct remaining-account position for
    // cashback-enabled coins. V6 hard-coded this to false, which can leave the
    // trailing fee-recipient account in the slot the on-chain program expects
    // to contain the cashback accumulator, producing error 6073.
    cashback:
      Boolean(
        bondingCurve.isCashbackCoin ??
        bondingCurve.is_cashback_coin ??
        false
      ),
  };


  const offlineSdk =
    new PumpSdk();


  // -------------------------------------------------------------------------
  // V7 FIX: Prepare the Pump UserVolumeAccumulator when cashback is enabled
  // -------------------------------------------------------------------------
  //
  // The Pump cashback account is:
  //
  //   PDA("user_volume_accumulator", user)
  //
  // under the Pump bonding-curve program.
  //
  // We do NOT manually append this PDA to the sell instruction. The SDK owns
  // the account ordering when `cashback: true` is supplied.
  // -------------------------------------------------------------------------

  const cashbackEnabled =
    Boolean(
      bondingCurve.isCashbackCoin ??
      bondingCurve.is_cashback_coin ??
      false
    );

  let userVolumeAccumulator;
  let userVolumeAccumulatorSetupInstructions = [];
  let userVolumeAccumulatorCreated = false;

  if (
    cashbackEnabled
  ) {
    [
      userVolumeAccumulator
    ] =
      PublicKey.findProgramAddressSync(
        [
          Buffer.from(
            "user_volume_accumulator"
          ),
          user.toBuffer(),
        ],
        PUMP_PROGRAM_ID
      );

    const userVolumeAccumulatorInfo =
      await connection.getAccountInfo(
        userVolumeAccumulator,
        "processed"
      );

    if (
      !userVolumeAccumulatorInfo
    ) {
      if (
        typeof offlineSdk.initUserVolumeAccumulator !==
        "function"
      ) {
        throw new Error(
          "pumpfun_init_user_volume_accumulator_method_unavailable"
        );
      }

      userVolumeAccumulatorSetupInstructions.push(
        await offlineSdk.initUserVolumeAccumulator({
          payer:
            user,
          user,
        })
      );

      userVolumeAccumulatorCreated =
        true;
    }
  }


  // -------------------------------------------------------------------------
  // FIX: Prepare bonding curve ATA BEFORE Pump.fun Sell
  // -------------------------------------------------------------------------

  const bondingCurveAddress =
    bondingCurvePda(
      mint
    );

  const bondingCurveAtaPreparation =
    await prepareBondingCurveAta(
      connection,
      user,
      bondingCurveAddress,
      mint,
      tokenProgram
    );


  // -------------------------------------------------------------------------
  // Prepare canonical user ATA
  // -------------------------------------------------------------------------

  const sellAccountPreparation =
    await prepareCanonicalSellAccount(
      connection,
      user,
      mint,
      tokenProgram,
      amount
    );


  // -------------------------------------------------------------------------
  // Build Pump.fun Sell instruction
  // -------------------------------------------------------------------------

  let instructions;
  let sellBuilderMode;

  try {

    const canonicalAtaExists =
      !sellAccountPreparation.ataCreated;

    if (
      canonicalAtaExists
    ) {

      if (
        typeof offlineSdk.sellInstructions !==
        "function"
      ) {
        throw new Error(
          "pumpfun_sell_instructions_method_unavailable"
        );
      }

      instructions =
        await offlineSdk.sellInstructions(
          sellInstructionParams
        );

      sellBuilderMode =
        "high-level";

    } else {

      if (
        typeof offlineSdk.getSellInstructionRaw !==
        "function"
      ) {
        throw new Error(
          "pumpfun_get_sell_instruction_raw_method_unavailable"
        );
      }

      instructions = [
        await offlineSdk.getSellInstructionRaw(
          sellInstructionParams
        ),
      ];

      sellBuilderMode =
        "raw-canonical-ata-recovery";
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
  // Prepend required account setup instructions
  // -------------------------------------------------------------------------

  const setupInstructions = [
    ...userVolumeAccumulatorSetupInstructions,

    ...bondingCurveAtaPreparation
      .setupInstructions,

    ...sellAccountPreparation
      .setupInstructions,
  ];


  if (
    setupInstructions.length >
    0
  ) {
    instructions = [
      ...setupInstructions,
      ...instructions,
    ];
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

      amount_clamped:
        amountClamped,

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
        bondingCurveAddress.toBase58(),

      associated_bonding_curve:
        bondingCurveAtaPreparation
          .associatedBondingCurve
          .toBase58(),

      bonding_curve_ata_created:
        bondingCurveAtaPreparation
          .ataCreated,

      cashback_enabled:
        cashbackEnabled,

      user_volume_accumulator:
        userVolumeAccumulator
          ? userVolumeAccumulator.toBase58()
          : null,

      user_volume_accumulator_created:
        userVolumeAccumulatorCreated,

      sell_builder_mode:
        sellBuilderMode,

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
  }
);

