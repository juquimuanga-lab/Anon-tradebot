// Builds an UNSIGNED swap transaction against Meteora's public Dynamic
// Bonding Curve program (the program Anoncoin's own API references via
// `meteoraConfigKey` for pre-graduation pools). No private key ever touches
// this script - it only needs the trader's PUBLIC key to set the fee payer.
// Signing happens in Python (app/execution/onchain/meteora_dbc.py) so the
// secret key never leaves the Python process.
const { Connection, PublicKey } = require('@solana/web3.js');
const {
  DynamicBondingCurveClient,
  getCurrentPoint,
} = require('@meteora-ag/dynamic-bonding-curve-sdk');
const BN = require('bn.js');

function readStdin() {
  return new Promise((resolve) => {
    let data = '';
    process.stdin.on('data', (chunk) => (data += chunk));
    process.stdin.on('end', () => resolve(data));
  });
}

async function main() {
  const raw = await readStdin();
  const input = JSON.parse(raw);
  const { action, baseMint, ownerPubkey, amountLamports, slippageBps, rpcUrl } = input;

  const connection = new Connection(rpcUrl, 'confirmed');
  const client = DynamicBondingCurveClient.create(connection, 'confirmed');

  const poolAccount = await client.state.getPoolByBaseMint(baseMint);
  if (!poolAccount) {
    throw new Error('pool_not_found_for_mint');
  }
  const poolAddress = poolAccount.publicKey;
  const virtualPool = poolAccount.account;

  const config = await client.state.getPoolConfig(virtualPool.config);
  if (!config) {
    throw new Error('pool_config_not_found');
  }

  const swapBaseForQuote = action === 'sell';
  const amountIn = new BN(String(amountLamports));
  const currentPoint = await getCurrentPoint(connection, config.activationType);
  const effectiveSlippageBps = slippageBps || 300;

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

  const outputAmount = new BN(quote.outputAmount.toString());
  const slippageBpsBN = new BN(effectiveSlippageBps);
  const tenThousand = new BN(10000);
  const minimumAmountOut = outputAmount
    .mul(tenThousand.sub(slippageBpsBN))
    .div(tenThousand);

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
  const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash('confirmed');
  tx.recentBlockhash = blockhash;

  const serialized = tx.serialize({ requireAllSignatures: false, verifySignatures: false });

  process.stdout.write(
    JSON.stringify({
      success: true,
      transaction_b64: serialized.toString('base64'),
      blockhash,
      last_valid_block_height: lastValidBlockHeight,
      quoted_output_amount: quote.outputAmount.toString(),
      minimum_amount_out: minimumAmountOut.toString(),
      pool_address: poolAddress.toBase58(),
    }) + '\n'
  );
}

main().catch((err) => {
  process.stdout.write(
    JSON.stringify({ success: false, error: String((err && err.message) || err) }) + '\n'
  );
  process.exit(0);
});
