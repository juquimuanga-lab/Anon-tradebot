/**
 * Pump.fun Sender Max wrapper.
 *
 * Runs the existing Pump.fun SDK builder unchanged, then adds the Helius
 * Sender tip instruction to the unsigned transaction before Python signs it.
 * This keeps the existing builder intact while making the resulting signed
 * transaction eligible for Helius Sender's low-latency delivery path.
 */

const {
  PublicKey,
  SystemProgram,
  Transaction,
} = require("@solana/web3.js");

const { spawn } = require("child_process");
const path = require("path");

const TIP_ACCOUNTS = [
  "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE",
  "D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ",
  "9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta",
  "5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn",
  "2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD",
  "2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ",
  "wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF",
  "3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT",
  "4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey",
  "4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or",
];

const DEFAULT_TIP_LAMPORTS = 1_000_000;
const MIN_TIP_LAMPORTS = 200_000;

function tipLamports() {
  const configured = Number(
    process.env.HELIUS_SENDER_TIP_LAMPORTS || DEFAULT_TIP_LAMPORTS
  );

  if (!Number.isSafeInteger(configured) || configured < MIN_TIP_LAMPORTS) {
    return DEFAULT_TIP_LAMPORTS;
  }

  return configured;
}

function chooseTipAccount() {
  const index = Math.floor(Math.random() * TIP_ACCOUNTS.length);
  return new PublicKey(TIP_ACCOUNTS[index]);
}

function runOriginalBuilder(input) {
  return new Promise((resolve, reject) => {
    const original = path.join(__dirname, "pumpfun_build_tx.js");
    const child = spawn("node", [original], {
      cwd: __dirname,
      stdio: ["pipe", "pipe", "pipe"],
    });

    let stdout = "";
    let stderr = "";

    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });

    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });

    child.on("error", reject);

    child.on("close", (code) => {
      const lines = stdout.trim().split(/\r?\n/).filter(Boolean);
      const lastLine = lines[lines.length - 1] || "";

      let result;
      try {
        result = JSON.parse(lastLine);
      } catch (error) {
        reject(
          new Error(
            `pumpfun_original_builder_invalid_json: ${
              error?.message || error
            }; stderr=${stderr.slice(-1200)}`
          )
        );
        return;
      }

      if (code !== 0 || !result.success) {
        reject(
          new Error(
            `pumpfun_original_builder_failed: ${
              result.error || `exit_code=${code}`
            }`
          )
        );
        return;
      }

      resolve({ result, stderr });
    });

    child.stdin.end(JSON.stringify(input));
  });
}

async function main() {
  let input = "";

  for await (const chunk of process.stdin) {
    input += chunk.toString();
  }

  let parsed;
  try {
    parsed = JSON.parse(input);
  } catch (error) {
    throw new Error(`invalid_json_input: ${error?.message || error}`);
  }

  const { result, stderr } = await runOriginalBuilder(parsed);

  if (!result.transaction_b64) {
    throw new Error("pumpfun_original_builder_missing_transaction");
  }

  const tx = Transaction.from(
    Buffer.from(result.transaction_b64, "base64")
  );

  const owner = new PublicKey(result.owner_pubkey || parsed.ownerPubkey);
  if (!tx.feePayer || !tx.feePayer.equals(owner)) {
    throw new Error("pumpfun_sender_fee_payer_mismatch");
  }

  const tipAccount = chooseTipAccount();
  const tip = tipLamports();

  tx.add(
    SystemProgram.transfer({
      fromPubkey: owner,
      toPubkey: tipAccount,
      lamports: tip,
    })
  );

  const serialized = tx.serialize({
    requireAllSignatures: false,
    verifySignatures: false,
  });

  const output = {
    ...result,
    transaction_b64: serialized.toString("base64"),
    sender_enabled: true,
    sender_tip_lamports: tip,
    sender_tip_account: tipAccount.toBase58(),
    sender_transport: "helius_sender_max",
    instruction_count: tx.instructions.length,
  };

  if (stderr) {
    console.error(stderr.trim());
  }

  process.stdout.write(`${JSON.stringify(output)}\n`);
}

main().catch((error) => {
  process.stdout.write(
    `${JSON.stringify({
      success: false,
      error: String(error?.message || error),
    })}\n`
  );
  process.exit(0);
});
