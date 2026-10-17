# Net Drift

The former Flow Tracker panel reads InsiderFinance's public QQQ or SPY option-flow feed through `/api/net-drift`. It does not use the dashboard's Yahoo option-chain snapshots.

For each returned trade, Net Drift estimates a bullish premium contribution:

- Call at/above ask: `+premium`; call at/below bid: `-premium`.
- Put at/above ask: `-premium`; put at/below bid: `+premium`.
- Midpoint or unknown side: excluded from signed drift and counted in the panel status.

The upper chart shows cumulative signed Call, Put, and combined Net Premium around the zero line, plus underlying spot sampled from the same trades. The lower chart shows cumulative directional contract volume (Call size minus Put size after trade-side sign). The 0DTE filter keeps trades whose expiry equals their trade date. The tooltip also shows combined net premium.

InsiderFinance's public API caps each response at 20 trades. The backend now pages through the public feed to retrieve the session's available rows, then shows `returned_count / session_count` and marks incomplete coverage. The public feed currently contains Ask-side trades only, so it cannot reproduce bid-side selling, the full market tape, or QuantDecay's proprietary Net Drift formula. Underlying spot is sampled at option-trade times, not from a continuous equity-price feed. Use the date picker or **Latest**, and select QQQ or SPY.

Sources: [InsiderFinance QQQ Flow](https://www.insiderfinance.io/ticker-flow/QQQ), [QuantDecay product description](https://quantdecay.com/).
