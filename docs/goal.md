# Screener Bot - Full Instructions

## Target URLs
- Listing page: https://www.screener.in/results/latest/
- Company page format: https://www.screener.in/company/{ID}/#quarters

---

## Step-by-Step Behavior

### STEP 1 - Scrape Company List
- Navigate to the listing page
- Wait for the results table to fully load using Playwright wait_for_selector
- Extract all company names and their URLs from the table

### STEP 2 - Visit Each Company Page
- Open each company URL in Playwright
- Wait for full page load
- Scroll to the section with id="quarters"

### STEP 3 - Extract Financial Data
From the quarterly results table inside #quarters, extract ONLY:
1. Sales (Revenue from Operations)
2. Operating Profit (EBITDA)
3. Net Profit (PAT)

For each metric, extract the two most recent quarter columns only.
Strip commas and currency symbols. Store as floats.

### STEP 4 - Calculate Percentage Change
```
percentage_change = ((current - previous) / abs(previous)) * 100
```
- If previous value is 0 or None, skip and log a warning
- Handle negative values correctly (net loss scenarios)

### STEP 5 - Alert Conditions
- percentage_change >= +20% -> POSITIVE ALERT (jump)
- percentage_change <= -20% -> NEGATIVE ALERT (fall)

### STEP 6 - Alert Output Format
Print immediately when detected (do not buffer to end of cycle):

```
ALERT DETECTED
Company     : Reliance Industries
Metric      : Net Profit
Previous Q  : Rs. 12,345 Cr
Current Q   : Rs. 15,200 Cr
Change      : +23.1%
Direction   : JUMP
Detected At : 2025-05-20 14:32:01
```
Use JUMP for jump, FALL for fall.

### STEP 7 - State Persistence
- After each cycle, save latest values to state.json:
  { "company_id": { "sales": X, "op_profit": Y, "net_profit": Z } }
- On next cycle, load state.json and compare against freshly scraped data
- This prevents duplicate alerts across cycles

### STEP 8 - 60 Second Loop
- After all companies are processed, run asyncio.sleep(60)
- Then restart from STEP 1 (re-fetch listing page each cycle)
- Log cycle start time, end time, and company count each cycle

---

## Console Output Standards
- Launch: "Screener Bot Started - Monitoring every 60 seconds"
- Cycle start: "Cycle #N started at {timestamp} - Found {X} companies"
- No alerts: "Cycle #N complete - No significant changes detected"

---

## Error Handling
- Wrap each company scrape in try/except
- Retry failed pages up to 2 times before skipping
- Log and skip if #quarters section is missing
- Log and skip if table structure cannot be parsed

---

## Anti-Bot Precautions
- Use headless=False during initial development
- Add random delay of 1 to 3 seconds between company page visits
- Set a realistic browser user-agent string
- Only re-visit the listing page once per 60 second cycle
