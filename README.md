# CS2 Trade-Up Scanner

Calculates CS2 trade-up contracts and scans CSFloat, DMarket and Waxpeer for the cheapest listings that
guarantee a target output float. Outputs are valued at Skinport's recent sales median.

## Easiest way to use it

1. Install Python from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
2. Double-click **Start Tradeup.bat**. The first start sets everything up by itself.
3. Pick a number from the menu:
   - **Find profitable trade-ups for me:** answers a few questions, then scans all markets.
   - **Build my own trade-up:** type skin or collection names, get the cheapest buy links.
   - **Check a trade-up with skins I have:** enter your skins and floats, see odds and value.
   - **Look at a collection**, **Set up market API keys**, **How do trade-ups work?**
   - **Check that the markets are working:** one quick request to every market (same as `tradeup doctor`).

Every result starts with a plain-language summary and a verdict: *Looks profitable*, *Risky gamble*
or *Not worth it*. After that you can open all buy links in your browser with one question.

Output values use the Skinport sales median of the shortest window with at least 3 sales, and are
never valued above the cheapest listing currently on sale. Both rules are in `config.toml`.
The selling fee is CSFloat's 2% by default: set `sell_fee` and `sell_market` in `config.toml` to the
market you actually sell on (Skinport 8%, Steam 15%).

Each result also shows how much one trade-up swings around its average, the chance of getting back less
than half, and how many repeats it takes to be about 95% sure of coming out ahead. Right before buy
links are shown, every chosen listing is checked again; sold or repriced ones are replaced.

## Setup for the command line

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .[dev]
copy config.example.toml config.toml   # then paste your API keys
.\.venv\Scripts\tradeup update-data    # downloads the item catalog
```

Keys can also be set as environment variables: `CSFLOAT_API_KEY`, `WAXPEER_API_KEY`,
`DMARKET_PUBLIC_KEY`, `DMARKET_SECRET_KEY`. Any market without a key is skipped.

| Market | Used for | Key needed |
|---|---|---|
| CSFloat | Input listings with floats | Yes |
| DMarket | Input listings with floats | Yes (Ed25519 key pair) |
| Waxpeer | Input listings with floats, bulk lowest prices | Listings yes, bulk prices no |
| Skinport | Output sales medians, bulk lowest prices | No |

## Commands

```powershell
# Show a collection's skins, float caps and knife/glove pool
tradeup info "Chroma 2"

# Evaluate a contract whose floats you already know (works with no keys)
tradeup calc -i "AK-47 | Redline@0.16x10"
tradeup calc -i "AK-47 | Redline@0.16x3" -i "souvenir:AWP | BOOM@0.1x7"
tradeup calc -i "AWP | Hyper Beast@0.1x5" --stattrak        # Covert -> knife (5 inputs)

# Find the cheapest listings for a composition and target float
tradeup optimize -i "AK-47 | Redline=3" -i "col:Huntsman=7" -t MW
tradeup optimize -i "col:Chroma 2=10" --tier restricted -t 0.149999
tradeup optimize -i "col:Wildfire=2-5" -i "col:Breakout" --tier covert -t FT --objective profit

# Scan everything: coarse pass on bulk prices, deep pass on real listings for the best candidates
tradeup scan --coarse-only                     # no keys needed
tradeup scan --tier restricted --tier classified -t MW -t FT --mode both --top 15 --min-roi 0.05
tradeup scan --tier covert -t FT --budget 600  # knife/glove contracts under $600

# Scan every hour and get a Windows notification for each new contract at >= 5% expected ROI
tradeup watch --every 60 --tier restricted --tier classified -t MW -t FT --min-roi 0.05
tradeup watch --once ...                       # a single pass, for Windows Task Scheduler

# Check that every price source and market still answers in a format the program understands
tradeup doctor
```

`watch` remembers contracts it already alerted on (`data/alerts_seen.json`) and writes each alert with its
buy links to `data/alerts.log`. To run it without a terminal open, create a Task Scheduler task that runs
`.venv\Scripts\tradeup.exe watch --once ...` with "Start in" set to this folder.

Run `tradeup doctor` when results look thin. A market whose API changed doesn't raise an error, it just
stops returning listings; the doctor reports that as FAIL.

Input specs for `optimize`: `"Skin=3"` exactly 3, `"Skin"` any number, `"col:Name=7"` 7 from a
collection, `"col:Name=2-5"` between 2 and 5. Targets are `FN/MW/FT/WW/BS` or a maximum float.

## How the math works

- **Inputs:** 10 of the same rarity, or 5 Coverts for a knife/glove. StatTrak can't mix with
  normal or Souvenir inputs. Souvenirs can be inputs and produce normal outputs.
- **Odds:** each input gives `1 / (N x outcomes in its collection)` to every next-tier outcome of its
  own collection. StatTrak knife contracts can't produce gloves. Doppler phases share one outcome slot,
  valued with the phase weights in the config.
- **Float:** each input is normalized to its own range, the values are averaged, and the average is
  mapped onto each output's range: `out = min + avg x (max - min)`. Set `float_formula = "legacy"` to
  average raw floats instead.
- **Target:** for every possible output the allowed average is `(target - min) / (max - min)`, and
  the tightest one in the contract wins. A collection whose output can never reach the target is
  excluded. Wear targets stay `float_safety_margin` below the boundary, and results are re-checked
  in 32-bit float precision.
- **Optimizer:** OR-Tools CP-SAT chooses the exact listings: exactly N, group counts, float budget,
  lowest cost or highest expected profit.
- **Quick estimate (coarse scan):** each output is valued at the cheapest wear it can land in, and an
  input at the cheapest bulk listing that fits, with markups measured against real fills (buying 10
  costs more than 10x the single cheapest listing). On 18 live contracts the cost estimate's median
  error was 3%. The deep pass on real listings is what counts.

## Check the math against the game

The rules above (normalized float averaging, Doppler phases sharing a slot, Souvenir inputs giving normal
outputs) follow the current CS2 trade-up behaviour as documented by the community, but they haven't been
compared with a real contract here. To check: before you sign a contract, enter its inputs and floats with
`tradeup calc` (or menu option 3) and note the predicted float for the result you get. If the in-game
float differs from the prediction beyond the 6th decimal, the formula is off; set `float_formula` or
report it.

## Market notes

- **DMarket** is searched by its float buckets (`floatPart`, e.g. FT-0 = 0.15-0.18), so cheap high-float
  offers don't crowd out the low floats a contract needs.
- **Waxpeer** has no float filter: listings come cheapest first, up to 3 pages of 100 per wear, so very
  low floats in a popular wear can be missed there. Trade-held items are left out unless
  `include_trade_locked = true`.
- **Souvenir** searches are skipped for skins with no Souvenir listing in the Skinport or Waxpeer bulk
  data, which saves about half the requests. A Souvenir sold only on CSFloat or DMarket is missed.
