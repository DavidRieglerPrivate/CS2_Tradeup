# CS2 Trade-Up Scanner

Calculates CS2 trade-up contracts and scans CSFloat, DMarket and Waxpeer for the cheapest listings that
guarantee a target output float. Outputs are valued at Skinport's recent sales median.

## Easiest way to use it

1. Install Python from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
2. Double-click **Start Tradeup.bat**. The first start sets everything up by itself.
3. Pick a number from the menu:
   - **Find profitable trade-ups for me:** answers a few questions, then scans all markets.
   - **Build my own trade-up:** type skin or collection names, get the cheapest buy links.
   - **Check a trade-up with skins I have:** pick skins from your Steam inventory (floats are read for
     you) or type them in, see odds and value.
   - **Look at a collection**, **Set up market API keys** (and your Steam profile), **How do trade-ups work?**
   - **Check that the markets are working:** one quick request to every market (same as `tradeup doctor`).

Every result starts with a plain-language summary and a verdict: *Looks profitable*, *Risky gamble*
or *Not worth it*. After that you can open all buy links in your browser with one question.

Output values use the Skinport sales median of the shortest window with at least 3 sales, and are
never valued above the cheapest listing currently on sale. Both rules are in `config.toml`.
The selling fee is CSFloat's 2% by default: set `sell_fee` and `sell_market` in `config.toml` to the
market you actually sell on (Skinport 8%, Steam 15%).

Each result also shows how much one trade-up swings around its average, the chance of getting back less
than half, and how many repeats it takes to be about 95% sure of coming out ahead. The best 3 results are
re-checked as still for sale before the list is shown, and any other result is re-checked when you open it;
sold or repriced listings are replaced.

## Setup for the command line

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .[dev]
copy config.example.toml config.toml   # then paste your API keys
.\.venv\Scripts\tradeup update-data    # downloads the item catalog
```

Keys can also be set as environment variables: `CSFLOAT_API_KEY`, `WAXPEER_API_KEY`,
`DMARKET_PUBLIC_KEY`, `DMARKET_SECRET_KEY`, and the Steam profile as `STEAM_ID`. Any market without a key
is skipped.

| Market | Used for | Key needed |
|---|---|---|
| CSFloat | Input listings with floats | Yes |
| DMarket | Input listings with floats | Yes (Ed25519 key pair) |
| Waxpeer | Input listings with floats, bulk lowest prices | Listings yes, bulk prices no |
| Skinport | Output sales medians, bulk lowest prices | No |
| Steam | Your own skins with floats (public inventory) | No, just your profile link |

## Commands

```powershell
# Show a collection's skins, float caps and knife/glove pool
tradeup info "Chroma 2"

# Evaluate a contract whose floats you already know (works with no keys)
tradeup calc -i "AK-47 | Redline@0.16x10"
tradeup calc -i "AK-47 | Redline@0.16x3" -i "souvenir:AWP | BOOM@0.1x7"
tradeup calc -i "AWP | Hyper Beast@0.1x5" --stattrak        # Covert -> knife (5 inputs)
tradeup calc -i "StatTrak™ AK-47 | Redline (Field-Tested)@0.16x10"   # Steam market names work too
tradeup calc -i "ak redline @ 0,16 x 10"                     # so do short names and comma decimals

# Find the cheapest listings for a composition and target float
tradeup optimize -i "AK-47 | Redline=3" -i "col:Huntsman=7" -t MW
tradeup optimize -i "col:Chroma 2=10" --tier restricted -t 0.149999
tradeup optimize -i "col:Wildfire=2-5" -i "col:Breakout" --tier covert -t FT --objective profit

# Scan everything: estimate every idea from bulk prices, then check all of them on real listings
tradeup scan --coarse-only                     # estimates only, no keys needed
tradeup scan --tier restricted --tier classified -t MW -t FT --mode both --min-roi 0.05
tradeup scan --tier classified --time 0        # no time limit: every idea on every market
tradeup scan --tier classified --top 20        # only the 20 best estimates (the old behaviour)
tradeup scan --tier covert -t FT --budget 600  # knife/glove contracts under $600

# Scan every hour and get a Windows notification for each new contract at >= 5% expected ROI
tradeup watch --every 60 --time 15 --tier restricted --tier classified -t MW -t FT --min-roi 0.05
tradeup watch --once ...                       # a single pass, for Windows Task Scheduler

# Check that every price source and market still answers in a format the program understands
tradeup doctor
```

```powershell
# Skins you already own (Steam inventory): list them, or let scans and optimize use them
tradeup inventory                              # usable skins with floats and what each is worth
tradeup scan --tier classified -t FT --inventory
tradeup optimize -i "col:Norse=10" --tier restricted -t FT --inventory
```

`watch` remembers contracts it already alerted on (`data/alerts_seen.json`) and writes each alert with its
buy links to `data/alerts.log`. To run it without a terminal open, create a Task Scheduler task that runs
`.venv\Scripts\tradeup.exe watch --once ...` with "Start in" set to this folder.

Run `tradeup doctor` when results look thin. A market whose API changed doesn't raise an error, it just
stops returning listings; the doctor reports that as FAIL.

Input specs for `optimize`: `"Skin=3"` exactly 3, `"Skin"` any number, `"col:Name=7"` 7 from a
collection, `"col:Name=2-5"` between 2 and 5. A skin may also be part of a collection you list
(`"AK-47 | Redline=3" "col:Phoenix=7"`): each input counts for one of them. Counts that can't add up to a
full contract are reported before any market is asked. Targets are `FN/MW/FT/WW/BS` or a maximum float.
The chosen inputs are listed with your own skins first, then one block per skin, cheapest first.

## Using skins you already own

Set your Steam profile once (menu option 5, or `steam_id` in `config.toml`: a profile link, SteamID64 or
custom profile name). The inventory must be public: Steam > Profile > Edit Profile > Privacy Settings >
Inventory: Public. No Steam API key is needed: Steam's inventory page sends every skin's float along.
Skins inside storage units aren't part of it, so take them out first.

Owned skins are only used when you say so (the menu asks; on the command line `--inventory`). Then:

- **Scan** and **Build my own** treat each owned skin like a listing with its own float. The optimizer mixes
  them with market listings and uses one only where it beats buying. A scan with a time limit checks the
  ideas your skins can go into first.
- **Each owned skin costs what it would sell for** (Skinport sales median after the sell fee), because
  using it means giving that money up. Set `[inventory] owned_cost = "free"` to count them as $0, which makes
  every contract with your skins look better than it is.
- **The budget is cash only:** owned skins don't count against "maximum you want to spend". Results show how
  many of your skins they use, what those are counted at, and what you still have to buy.
- **Check a trade-up** can pick a whole contract from your inventory: type the row numbers (`1-10`, `2,4,6`).

The inventory is read at most every 10 minutes (Steam blocks frequent reads for a while). `tradeup doctor`
checks that it can still be read.

## How a scan covers every idea

1. **Estimate every idea.** An idea is one collection x rarity x normal/StatTrak x target wear. Each is
   estimated from the Skinport and Waxpeer bulk price lists (a few requests in total) and ranked by
   estimated return.
2. **Check every idea on real listings, most promising first.** For each input skin, CSFloat and DMarket
   are asked first. Waxpeer has no float filter and needs one search per wear, so it is then searched
   only for the wears where its bulk price list shows something that could matter. A wear is skipped
   when nothing is listed there, or when Waxpeer's cheapest listing in it costs more than the 10th
   cheapest listing (5th for Covert -> knife) of the same skin and wear already found elsewhere.
   A contract uses at most that many of one skin, so a pricier Waxpeer listing could only help
   through a lower float, and Waxpeer's cheapest-first results rarely bring that.
3. **Pick the exact skins** for every idea that was checked, plus the best mix of collections per
   rarity, StatTrak and target.

The scan stops asking markets at its time limit (menu: Quick 2 min, Normal 5, Thorough 15, Complete
no limit; `tradeup scan --time`, default 10 minutes). An idea counts as checked once all its input
skins were asked on CSFloat and DMarket; ideas not reached are left out of the results, and the
summary after the scan says how many ideas were checked, how many Waxpeer searches were made or skipped,
and which market ran out of requests.

**Listings are kept for 30 minutes** (`listing_cache_minutes`), so a second scan with the same settings
answers the ideas already checked from the cache and spends its time on the ones the first scan didn't
reach. This is also why a re-check happens before you buy: the best 3 results are re-checked
automatically, any other one when you open it. Cache files older than a day are deleted at the start
of every scan.

**CSFloat** allows 200 requests per window. The scan stops using it when only 30 are left
(`csfloat_reserve`), so re-checking chosen listings still works. Skins asked while CSFloat was
unavailable are asked again once the window resets, if that happens before the time limit; otherwise
they are checked with DMarket and Waxpeer listings only, and the summary says how many. Resets have
been seen 2 hours apart, so CSFloat covers only about 100-170 skins per window; a full Classified scan
needs about 350, so most of it runs on DMarket and Waxpeer.

**Rate limits.** Each market's requests are spaced by `[rate_limits]` and served by idea rank, so the
second and third result pages of a promising idea don't wait behind the first pages of every worse
one. A market that keeps answering "too many requests" (HTTP 429) even after the waits it asked for is
blocked for 2 minutes, then 4, 8 and so on up to 30, with one test request after each block.

Measured on 2026-10-16:
- Classified -> Covert, FN/MW/FT, normal and StatTrak (301 ideas, 351 skins): finished in 5.5 minutes,
  about 1,330 Waxpeer searches made and 240 skipped.
- All gun rarities (about 1,250 ideas, 3,200 skins), empty cache: about 260 ideas checked in the first
  3 minutes. DMarket (10 requests a second) sets how fast ideas get checked; Waxpeer's searches
  (3 a second) make a full pass take roughly half an hour, or several shorter scans in a row thanks to
  the listing cache.

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

- **CSFloat** allows 200 requests per window (resets have been seen 2 hours apart). Scans keep the last 30 for
  re-checks and carry on with the other markets, asking CSFloat again after the reset when time allows;
  `tradeup doctor` shows how many requests are left. Re-checking listings costs one CSFloat request per
  listing, so only the best `confirm_top` results (default 3) are re-checked right after a scan.

- **DMarket** is searched by its float buckets (`floatPart`, e.g. FT-0 = 0.15-0.18), so cheap high-float
  offers don't crowd out the low floats a contract needs.
- **Waxpeer** has no float filter: listings come cheapest first, up to 3 pages of 100 per wear, so very
  low floats in a popular wear can be missed there. Scans skip wear searches that can't beat what the
  other markets already offer (see above). Trade-held items are left out unless
  `include_trade_locked = true`.
- **Souvenir** searches are skipped for skins with no Souvenir listing in the Skinport or Waxpeer bulk
  data, which saves about half the requests. A Souvenir sold only on CSFloat or DMarket is missed.
