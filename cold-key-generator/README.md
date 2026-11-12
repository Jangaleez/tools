# Cold Key Generator

Makes **Bitcoin, Ethereum and Solana seed phrases** on a computer that is **never online**, using the official tools:

| Coin | Seed phrase made by | Addresses worked out by |
|---|---|---|
| Bitcoin | BIP39 reference implementation ([python-mnemonic](https://github.com/trezor/python-mnemonic) by Trezor, the reference listed in BIP39) | **Bitcoin Core** itself, from the signed bitcoincore.org release |
| Ethereum | BIP39 reference implementation | **eth-account**, the Ethereum Foundation's Python library |
| Solana | **solana-keygen**, the official Solana CLI (Anza) | solana-keygen |

The phrases work in normal wallets: Sparrow, Electrum and hardware wallets for Bitcoin; MetaMask, Rabby and Ledger for Ethereum; Phantom, Solflare and Backpack for Solana.

> Bitcoin Core itself doesn't use seed phrases. It makes wallet files. That's why the phrase comes from the BIP39 standard's reference code, while Bitcoin Core does all the elliptic-curve maths to produce the addresses.

## How it keeps you safe

**Before anything is generated**, every one of these checks must pass, or it stops:

- **No network.** No interface is up, there's no route to the internet, and test connections fail. If it finds a connection, it tries to switch all networking off (`nmcli`, `rfkill`) and checks again. It refuses to continue if it's still online, and it checks again before every phrase.
- **No screen sharing or recording.** It looks for remote-desktop, screen-sharing and recording programs, and refuses to run over SSH.
- **Bundle unchanged.** It checks every file against the manifest made on your PC, and shows a **bundle code** for you to compare with the one printed when the bundle was made.
- **Bitcoin Core is genuine.** The download must match the SHA-256 hash that **13 independent Bitcoin Core builders** signed for v32.1. `SHA256SUMS.asc` must also carry valid GPG signatures from **at least 3** builders whose key fingerprints are pinned in the code.
- **Solana CLI is genuine.** The download must match the hash GitHub recorded for the official `anza-xyz/agave` release. Anza doesn't publish signatures, so this is an integrity check rather than a signature check.
- **Python libraries are genuine.** They're pinned by SHA-256 hash and downloaded with `pip --require-hashes`.
- **Self-test.** Every tool must reproduce published test values: all 24 official BIP39 test vectors, the BIP84 and BIP86 test addresses through Bitcoin Core, the well-known Ethereum test addresses, and the Solana test address. The Solana value was also confirmed with an independent implementation written from the SLIP-0010 and RFC 8032 specifications.

**While generating:**

- Randomness comes from the computer's secure random generator. You can optionally mix in your own **dice rolls** (Bitcoin and Ethereum); mixing can only add strength, never remove it.
- An optional **passphrase** ("25th word") is supported.
- Secrets never appear in command-line arguments, so other programs can't read them from the process list. Bitcoin Core receives them on standard input, and solana-keygen through a hidden terminal prompt.
- **Nothing is written to disk and nothing goes to the clipboard.** Bitcoin Core runs with networking switched off and its data folder in RAM, and that folder is deleted afterwards.
- After you've written the words down, you must **type 3 random words back** from your paper copy before it moves on.
- The screen and the terminal's scrollback are cleared after each phrase.
- It shows the first addresses so you can check them later from a watch-only wallet, or by sending a small test amount.

## What you need

- **A Tails USB stick** (8 GB or more). Tails is a system that runs from USB and forgets everything when you shut down. Install it with the official guide at [tails.net/install](https://tails.net/install/).
- **A second USB stick** for the bundle (1 GB or more; the official Solana download is large).
- Paper and a pen.

## Step 1: make the bundle (on your normal, online PC)

You need Python 3.10 or newer (`winget install Python.Python.3.13` on Windows).

```
cd C:\Projects\tools\cold-key-generator
python prepare_bundle.py E:\coldkeys
```

Replace `E:` with your second USB stick. On Linux, run `python3 prepare_bundle.py /media/you/USB/coldkeys`.

It downloads Bitcoin Core, the Solana CLI and the Python libraries, checks each one, and prints a **bundle code** like `1A2B 3C4D 5E6F 7A8B`. **Write the code down.**

## Step 2: generate (on the offline computer)

1. Start the computer from the **Tails** USB stick.
2. On the Welcome Screen, open **Additional Settings** (the **+** button, or **Alt+A**), turn on **Offline Mode**, then click **Start Tails**.
3. Plug in the bundle USB stick, open the `coldkeys` folder, right-click an empty space and choose **Open in Terminal**.
4. Run:
   ```
   bash start.sh
   ```
5. Check that the **bundle code** matches the one you wrote down.
6. Choose a coin and follow the prompts. Write the words down on paper, then type back the 3 words it asks for.
7. When you're done, **shut Tails down**. That wipes the computer's memory.

## Good habits

- For large amounts, a **hardware wallet** (Ledger, Trezor, Coldcard) is still the safest choice. This tool is for paper or cold-storage backups and test wallets.
- Never type a seed phrase into a computer or phone that goes online, and never photograph it.
- Before moving real money, send a **small test amount** to the first address and make sure it arrives.
- Keep two copies of each phrase in separate safe places. If you used a passphrase, store it separately from the words.
- Only plug the bundle USB stick into the offline computer after it's been made, and don't reuse it for anything else.

## Updating versions

The Bitcoin Core version, its signed hash, the builder fingerprints, and the Solana release tag are pinned at the top of `bundle-files/coldkeys.py` and `prepare_bundle.py`. To move to a new Bitcoin Core release, update `BTC_VERSION` and `BTC_SHA256` from the signatures in [bitcoin-core/guix.sigs](https://github.com/bitcoin-core/guix.sigs), not from the download site alone.
